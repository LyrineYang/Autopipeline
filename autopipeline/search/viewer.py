from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import math
from pathlib import Path
from typing import Any

from .run_artifacts import count_candidates, format_counts, read_events, read_object, was_skipped_for_budget


METRIC_LABELS = {
    "correctness": ("Correctness", "", "gate"),
    "speedup_vs_pytorch": ("Speedup over PyTorch", "x", "objective"),
    "speedup": ("Speedup", "x", "objective"),
    "throughput_tflops": ("Throughput", "TFLOPS", "display"),
    "bandwidth_gb_s": ("Bandwidth", "GB/s", "display"),
    "forward_latency_ms": ("Forward latency", "ms", "objective"),
    "end_to_end_ms": ("End-to-end latency", "ms", "objective"),
    "latency_ms": ("Latency", "ms", "objective"),
    "tokens_per_second": ("Token throughput", "tok/s", "objective"),
    "cost_per_token": ("Cost per token", "", "objective"),
    "bench_time_seconds": ("Benchmark time", "s", "cost"),
    "ranking_score": ("Ranking score", "", "objective"),
    "score_value": ("Score", "", "objective"),
    "score": ("Score", "", "objective"),
}

LOWER_IS_BETTER = {"forward_latency_ms", "end_to_end_ms", "latency_ms", "cost_per_token", "bench_time_seconds"}
FAILURE_OUTCOMES = {"failed", "timeout", "rejected_compile", "rejected_runtime", "rejected_correctness"}
DISPLAY_PRIORITY = [
    "throughput_tflops",
    "speedup_vs_pytorch",
    "speedup",
    "forward_latency_ms",
    "end_to_end_ms",
    "throughput",
    "tokens_per_second",
    "score_value",
    "score",
]
PRIMARY_PRIORITY = [
    "speedup_vs_pytorch",
    "speedup",
    "forward_latency_ms",
    "end_to_end_ms",
    "throughput",
    "tokens_per_second",
    "cost_per_token",
    "score_value",
    "score",
]


@dataclass(slots=True)
class ViewPaths:
    output_dir: Path
    run_summary: Path
    branches: Path
    attempts: Path
    metrics_catalog: Path
    human_timeline: Path
    dashboard: Path
    iteration_effect: Path
    summary_markdown: Path


def build_run_view(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state = read_object(run_dir / "search_state.json")
    events = read_events(run_dir / "tree_events.jsonl")
    motifs = read_events(run_dir / "archive" / "motifs.jsonl")
    family_summary = read_object(run_dir / "archive" / "family_summary.json")

    nodes = _dict_value(state.get("nodes"))
    task = _dict_value(state.get("task_snapshot"))
    config = _dict_value(state.get("search_config"))
    budget = _dict_value(state.get("budget"))
    final_result = _dict_value(state.get("final_result"))

    metric_keys = _collect_metric_keys(nodes, events)
    primary_metric = _metric_spec(_select_metric(metric_keys, PRIMARY_PRIORITY, state, preferred_role="objective"))
    display_metric = _metric_spec(_select_metric(metric_keys, DISPLAY_PRIORITY, state, preferred_role="display"))
    metrics_catalog = _metrics_catalog(metric_keys, primary_metric, display_metric)
    attempts = _build_attempts(nodes, events, display_metric, primary_metric)
    branches = _build_branches(nodes, attempts, motifs)
    run_summary = _build_run_summary(
        run_dir=run_dir,
        state=state,
        task=task,
        config=config,
        budget=budget,
        final_result=final_result,
        attempts=attempts,
        branches=branches,
        search_counts=count_candidates(state, events),
        primary_metric=primary_metric,
        display_metric=display_metric,
    )
    run_summary["result"] = _run_result(run_dir, final_result, budget)
    human_timeline = _build_human_timeline(run_summary, attempts, branches)

    return {
        "run_summary": run_summary,
        "branches": branches,
        "attempts": attempts,
        "metrics_catalog": metrics_catalog,
        "human_timeline": human_timeline,
        "family_summary": family_summary,
    }


def _run_result(run_dir: Path, final_result: dict, budget: dict) -> dict:
    receipt = read_object(run_dir / "result.json")
    final = _dict_value(receipt.get("final_verification")) or _dict_value(final_result.get("final_verify"))
    report = read_object(run_dir / "final_verification" / "verification_report.json")
    evidence = report or _dict_value(final.get("verification"))
    correctness = _dict_value(evidence.get("correctness")).get("passed")
    numerical_error = _dict_value(evidence.get("numerical_error")).get("passed")
    if numerical_error is False:
        correctness = False

    reference = _dict_value(_dict_value(report.get("reference")).get("metrics"))
    candidate = _dict_value(_dict_value(report.get("candidate")).get("metrics")) or _dict_value(final.get("metrics"))
    if correctness is None and candidate.get("correctness") in {"PASS", "FAIL"}:
        correctness = candidate["correctness"] == "PASS"

    performance = _dict_value(evidence.get("performance"))
    native_forward = str(candidate.get("measurement_scope", "")).startswith("model forward")
    latency_key = next(
        (key for key in ("forward_latency_ms", "end_to_end_ms", "latency_ms")
         if _number(reference.get(key)) is not None or _number(candidate.get(key)) is not None),
        "end_to_end_ms",
    )
    reference_latency = _number(reference.get(latency_key))
    candidate_latency = _number(candidate.get(latency_key))
    if latency_key == "end_to_end_ms" or native_forward:
        # Native releases before a7 used end_to_end_ms for forward timing.
        if reference_latency is None:
            reference_latency = _number(performance.get("reference_end_to_end_ms"))
        if candidate_latency is None:
            candidate_latency = _number(performance.get("candidate_end_to_end_ms"))
    latency_label = "forward latency" if native_forward else _metric_spec(latency_key)["label"].lower()
    if reference_latency is None and candidate_latency is None:
        latency_label = "latency"

    usage = _dict_value(budget.get("agent_usage"))
    calls = _dict_value(receipt.get("search_counts")).get("agent_calls")
    return {
        "correctness_passed": correctness,
        "latency_label": latency_label,
        "reference_latency_ms": reference_latency,
        "candidate_latency_ms": candidate_latency,
        "duration_seconds": _number(receipt.get("duration_seconds")),
        "agent_calls": usage.get("agent_calls") if calls is None else calls,
        "total_tokens": usage.get("total_tokens"),
    }


def result_rows(result: dict) -> list[tuple[str, str]]:
    def with_unit(key: str, unit: str) -> str:
        value = result[key]
        return f"{_format_number(value)} {unit}" if value is not None else "Unavailable"

    correctness = {True: "Passed", False: "Failed"}.get(result["correctness_passed"], "Unavailable")
    latency_label = result["latency_label"]
    calls, tokens = result["agent_calls"], result["total_tokens"]
    return [
        ("Final correctness", correctness),
        (f"Reference {latency_label}", with_unit("reference_latency_ms", "ms")),
        (f"Candidate {latency_label}", with_unit("candidate_latency_ms", "ms")),
        ("Run time", with_unit("duration_seconds", "s")),
        ("Agent calls", f"{calls:,}" if calls is not None else "Unavailable"),
        ("Agent tokens recorded", f"{tokens:,}" if tokens is not None else "Unavailable"),
    ]


def write_run_view(run_dir: Path, output_dir: Path | None = None, *, outcome: dict[str, str] | None = None) -> ViewPaths:
    run_dir = Path(run_dir).resolve()
    output = Path(output_dir).resolve() if output_dir is not None else run_dir / "view"
    output.mkdir(parents=True, exist_ok=True)
    view = build_run_view(run_dir)
    if outcome is not None:
        # The calling product supplies its own verdict; the generic viewer only renders it.
        view["run_summary"]["outcome"] = outcome
    paths = ViewPaths(
        output_dir=output,
        run_summary=output / "run_summary.json",
        branches=output / "branches.jsonl",
        attempts=output / "attempts.jsonl",
        metrics_catalog=output / "metrics_catalog.json",
        human_timeline=output / "human_timeline.jsonl",
        dashboard=output / "dashboard.html",
        iteration_effect=output / "iteration_effect.svg",
        summary_markdown=output / "summary.md",
    )
    _write_json(paths.run_summary, view["run_summary"])
    _write_jsonl(paths.branches, view["branches"])
    _write_jsonl(paths.attempts, view["attempts"])
    _write_json(paths.metrics_catalog, view["metrics_catalog"])
    _write_jsonl(paths.human_timeline, view["human_timeline"])
    paths.iteration_effect.write_text(
        render_iteration_effect_svg(view["run_summary"], view["attempts"]),
        encoding="utf-8",
    )
    paths.dashboard.write_text(
        render_dashboard_html(view, iteration_effect_name=paths.iteration_effect.name),
        encoding="utf-8",
    )
    paths.summary_markdown.write_text(render_summary_markdown(view), encoding="utf-8")
    return paths


def render_iteration_effect_svg(run_summary: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
    points = [
        (item, value) for item in attempts
        if not item.get("display_imputed") and (value := _number(item.get("display_value"))) is not None
    ]
    if not points:
        return _empty_svg(1040, 320, "No measured attempts")

    width, height = 1040, 320
    left, right, top, bottom = 76, 1010, 25, 256
    first = int(points[0][0]["experiment_index"])
    last = max(first + 1, int(points[-1][0]["experiment_index"]))
    low = min(value for _, value in points)
    high = max(value for _, value in points)
    padding = (high - low) * 0.08 or max(abs(low) * 0.05, 0.001)
    low -= padding
    high += padding

    def x_scale(index: int) -> float:
        return left + (index - first) / (last - first) * (right - left)

    def y_scale(value: float) -> float:
        return top + (high - value) / (high - low) * (bottom - top)

    metric = run_summary["display_metric"]
    label = metric["label"] + (f" ({metric['unit']})" if metric["unit"] else "")
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
        f' viewBox="0 0 {width} {height}" role="img" aria-label="Iteration map">',
        '<style>text{font:13px system-ui,sans-serif;fill:#475569}'
        '.grid{stroke:#e2e8f0}.best{fill:none;stroke:#087e72;stroke-width:2.5}</style>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text transform="translate(19 {(top + bottom) / 2}) rotate(-90)" text-anchor="middle">{escape(label)}</text>',
    ]
    for tick in range(6):
        value = high - (high - low) * tick / 5
        y = y_scale(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end">{_format_number(value)}</text>')

    stride = math.ceil((last - first) / 8)
    for index in range(first, last + 1, stride):
        x = x_scale(index)
        parts.append(f'<text x="{x:.1f}" y="{bottom + 25}" text-anchor="middle">{index}</text>')
    parts.append(f'<text x="{width / 2}" y="{height - 8}" text-anchor="middle">Attempt</text>')

    best = None
    best_points = []
    dots = []
    for item, value in points:
        index = int(item["experiment_index"])
        x, y = x_scale(index), y_scale(value)
        outcome = item["outcome_key"]
        kept = item.get("accepted") and outcome in {"kept", "baseline"}
        if kept and _is_better(value, best, metric["direction"]):
            best = value
        if best is not None:
            best_points.append(f"{x:.1f},{y_scale(best):.1f}")
        color = {"baseline": "#4f46e5", "kept": "#087e72"}.get(outcome, "#b45309")
        if outcome in FAILURE_OUTCOMES:
            color = "#b91c1c"
        tooltip = f"{item['branch_id']} · {_outcome_label(outcome)} · {_format_metric_value(value, metric)}"
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}">'
            f'<title>{escape(tooltip)}</title></circle>'
        )
    if best_points:
        parts.append(f'<polyline points="{" ".join(best_points)}" class="best"/>')
    return "\n".join([*parts, *dots, "</svg>"])


def render_dashboard_html(view: dict[str, Any], *, iteration_effect_name: str) -> str:
    summary = view["run_summary"]
    outcome = summary.get("outcome") or {}
    title = escape(str(summary["title"]))
    cards = "\n".join(
        f'<div class="card"><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>'
        for label, value in result_rows(summary["result"])
    )
    outcome_html = "".join(
        f'<{tag}>{escape(outcome[key])}</{tag}>'
        for key, tag in (("title", "h2"), ("detail", "p"), ("reason", "p"))
        if outcome.get(key)
    )
    next_step = f'<p class="next-step">{escape(outcome["next_step"])}</p>' if outcome.get("next_step") else ""
    details = ""
    if view["branches"] or any(item["local_iteration"] for item in view["attempts"]):
        counts = escape(format_counts(summary["search_counts"]))
        branch_rows = "\n".join(
            "<tr>"
            f"<td>{escape(branch['branch_id'])}</td><td>{escape(branch['parent_id'] or '—')}</td>"
            f"<td>{branch['round']}</td><td>{escape(branch['variant'] or branch['family'])}</td>"
            f"<td>{escape(branch['status'].title())}</td>"
            f"<td>{escape(_outcome_label(branch['outcome']))}</td>"
            "</tr>"
            for branch in view["branches"]
        )
        attempt_rows = "\n".join(
            "<tr>"
            f"<td>{item['experiment_index']}</td><td>{escape(item['branch_id'])}</td>"
            f"<td>{escape(item['outcome'])}</td>"
            f"<td>{'—' if item.get('display_imputed') else _format_number(item['display_value'])}</td>"
            f"<td>{escape(item['summary'])}</td>"
            "</tr>"
            for item in view["attempts"][-18:]
        )
        metric = escape(str(summary["display_metric"]["label"]))
        details = f"""<details id="branch-details" class="panel">
    <summary>Branch details</summary>
    <p class="muted">{counts}</p>
    <h3>Iteration map</h3>
    <p class="legend">
      <span class="baseline">● Baseline</span>
      <span class="kept">● Kept</span>
      <span class="reverted">● Reverted</span>
      <span class="failed">● Failed</span>
      <span class="kept">━ Best kept measurement</span>
    </p>
    <div class="scroll"><img class="chart" src="{escape(iteration_effect_name)}" alt="Search measurements by attempt"></div>
    <h3>Branches</h3>
    <div class="scroll"><table>
      <thead><tr><th>Branch</th><th>Parent</th><th>Round</th><th>Candidate</th><th>Status</th><th>Last attempt</th></tr></thead>
      <tbody>{branch_rows}</tbody>
    </table></div>
    <h3>Recent attempts</h3>
    <div class="scroll"><table>
      <thead><tr><th>Attempt</th><th>Branch</th><th>Outcome</th><th>{metric}</th><th>Evidence summary</th></tr></thead>
      <tbody>{attempt_rows}</tbody>
    </table></div>
  </details>"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme:light; --ink:#172033; --muted:#475569; --line:#dbe3ef; --bg:#f6f8fb; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1160px; margin:auto; padding:40px 24px; }}
    h1 {{ margin:0 0 16px; font-size:clamp(24px, 4vw, 34px); }}
    h2 {{ margin:0 0 8px; font-size:20px; }}
    h3 {{ margin:22px 0 8px; font-size:16px; }}
    p {{ margin:0 0 12px; overflow-wrap:anywhere; }}
    .muted, dt {{ color:var(--muted); }}
    .panel {{ background:white; border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:14px; }}
    .result {{ border-top:3px solid #087e72; }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(138px,1fr)); gap:10px; margin:14px 0 0; }}
    .card {{ background:var(--bg); border-radius:8px; padding:12px; }}
    dt {{ font-size:13px; }}
    dd {{ margin:3px 0 0; font-size:19px; font-weight:650; font-variant-numeric:tabular-nums; }}
    .next-step {{ margin:16px 0 0; color:var(--muted); white-space:pre-wrap; }}
    summary {{ cursor:pointer; font-weight:650; }}
    details > p {{ margin-top:10px; }}
    .legend {{ display:flex; gap:8px 20px; flex-wrap:wrap; font-size:13px; }}
    .baseline {{ color:#4f46e5; }} .kept {{ color:#087e72; }}
    .reverted {{ color:#92400e; }} .failed {{ color:#b91c1c; }}
    .scroll {{ overflow-x:auto; }}
    .chart {{ display:block; width:100%; min-width:620px; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th, td {{ padding:8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); }}
  </style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <section class="panel result" aria-label="Run result">
    {outcome_html}
    <dl class="cards">{cards}</dl>
    {next_step}
  </section>
  {details}
</main>
</body>
</html>
"""


def render_summary_markdown(view: dict[str, Any]) -> str:
    summary = view["run_summary"]
    outcome = summary.get("outcome") or {}
    lines = [f"# {summary['title']}", ""]
    for key in ("title", "detail", "reason"):
        if outcome.get(key):
            lines.extend([outcome[key], ""])
    lines.extend(["## Final verification and usage", ""])
    lines.extend(f"- **{label}** {value}" for label, value in result_rows(summary["result"]))
    if outcome.get("next_step"):
        lines.extend(["", outcome["next_step"]])
    if view["branches"] or any(item["local_iteration"] for item in view["attempts"]):
        lines.extend(["", "[Branch details and iteration map](dashboard.html#branch-details)"])
    return "\n".join(lines) + "\n"


def _build_attempts(
    nodes: dict[str, Any],
    events: list[dict[str, Any]],
    display_metric: dict[str, Any],
    primary_metric: dict[str, Any],
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    experiment_index = 0

    for event in events:
        event_type = event.get("event")
        payload = _dict_value(event.get("payload"))
        if event_type == "baseline_completed":
            node_id = str(payload.get("node_id") or "node-0001")
            node = _dict_value(nodes.get(node_id))
            attempt = _attempt_from_node(
                node_id=node_id,
                node=node,
                experiment_index=0,
                local_iteration=0,
                display_metric=display_metric,
                primary_metric=primary_metric,
                fallback_score=payload.get("score"),
                fallback_accepted=payload.get("accepted"),
                fallback_summary=payload.get("summary"),
                event_timestamp=str(event.get("timestamp") or ""),
            )
            attempt["outcome_key"] = "baseline"
            attempt["outcome"] = _outcome_label("baseline")
            attempts.append(attempt)
            seen.add((node_id, 0))
            continue
        if event_type != "evaluation_completed":
            continue
        node_id = str(payload.get("node_id") or "")
        if not node_id:
            continue
        local_iteration = int(payload.get("local_iteration") or 1)
        key = (node_id, local_iteration)
        if key in seen:
            continue
        seen.add(key)
        experiment_index += 1
        node = _dict_value(nodes.get(node_id))
        attempts.append(
            _attempt_from_node(
                node_id=node_id,
                node=node,
                experiment_index=experiment_index,
                local_iteration=local_iteration,
                display_metric=display_metric,
                primary_metric=primary_metric,
                fallback_score=payload.get("score"),
                fallback_accepted=payload.get("accepted"),
                fallback_summary=payload.get("summary"),
                event_timestamp=str(event.get("timestamp") or ""),
                # Earlier local-loop events did not store individual measurements.
                event_metrics=_dict_value(payload.get("metrics")) if "local_iteration" in payload else None,
                event_kept=payload.get("kept"),
            )
        )

    for node_id, node in sorted(nodes.items()):
        if str(_dict_value(node).get("hypothesis", {}).get("id")) == "baseline":
            continue
        if any(item.get("branch_id") == node_id for item in attempts):
            continue
        experiment_index += 1
        attempts.append(
            _attempt_from_node(
                node_id=node_id,
                node=_dict_value(node),
                experiment_index=experiment_index,
                local_iteration=1,
                display_metric=display_metric,
                primary_metric=primary_metric,
                fallback_score=None,
                fallback_accepted=None,
                fallback_summary=None,
                event_timestamp="",
            )
        )

    parent_scores = {
        node_id: _number(_dict_value(_dict_value(node).get("evaluation_result")).get("score"))
        for node_id, node in nodes.items()
    }
    best_display = None
    direction = str(display_metric.get("direction") or "higher")
    for attempt in sorted(attempts, key=lambda item: int(item.get("experiment_index", 0))):
        kept = attempt.pop("event_kept")
        if attempt["outcome_key"] == "baseline":
            if attempt["accepted"]:
                best_display = attempt.get("display_value")
            attempt["best_so_far"] = best_display
            continue
        node = _dict_value(nodes.get(str(attempt.get("branch_id"))))
        parent_id = node.get("parent_id")
        parent_score = parent_scores.get(str(parent_id)) if parent_id is not None else None
        accepted = bool(attempt.get("accepted"))
        score = _number(attempt.get("score"))
        status = str(node.get("status") or "")
        if not attempt.get("evaluated") and was_skipped_for_budget(node):
            attempt["outcome_key"] = "skipped_budget"
            attempt["outcome"] = "Skipped by budget"
            continue
        if status == "queued":
            attempt["outcome_key"] = "pending"
            attempt["outcome"] = "Proposed"
            continue
        if status == "failed" and not attempt.get("evaluated"):
            outcome = "failed"
        elif not accepted:
            outcome = _failure_outcome(attempt, node)
        else:
            if kept is None:
                kept = parent_score is not None and score is not None and score > parent_score
            outcome = "kept" if kept else "reverted"
        attempt["outcome_key"] = outcome
        attempt["outcome"] = _outcome_label(outcome)
        display_value = _number(attempt.get("display_value"))
        if outcome == "kept" and display_value is not None and _is_better(display_value, best_display, direction):
            best_display = display_value
            attempt["refreshes_best"] = True
        attempt["best_so_far"] = best_display

    return sorted(attempts, key=lambda item: int(item.get("experiment_index", 0)))


def _attempt_from_node(
    *,
    node_id: str,
    node: dict[str, Any],
    experiment_index: int,
    local_iteration: int,
    display_metric: dict[str, Any],
    primary_metric: dict[str, Any],
    fallback_score: Any,
    fallback_accepted: Any,
    fallback_summary: Any,
    event_timestamp: str,
    event_metrics: dict | None = None,
    event_kept: bool | None = None,
) -> dict[str, Any]:
    hypothesis = _dict_value(node.get("hypothesis"))
    evaluation = _dict_value(node.get("evaluation_result"))
    metrics = _dict_value(evaluation.get("metrics")) if event_metrics is None else event_metrics
    score = _number(fallback_score if fallback_score is not None else evaluation.get("score"))
    accepted = fallback_accepted if fallback_accepted is not None else evaluation.get("accepted")
    execution = _dict_value(node.get("execution_result"))
    summary = str(fallback_summary or evaluation.get("summary") or node.get("error") or execution.get("summary") or "")
    display_key = str(display_metric.get("key") or "score")
    primary_key = str(primary_metric.get("key") or "score")
    display_value = _number(metrics.get(display_key))
    if display_value is None and display_key in {"score", "score_value"}:
        display_value = score
    primary_value = _number(metrics.get(primary_key))
    if primary_value is None and primary_key in {"score", "score_value"}:
        primary_value = score
    return {
        "experiment_index": experiment_index,
        "granularity": "attempt" if local_iteration else "baseline",
        "branch_id": node_id,
        "parent_id": node.get("parent_id"),
        "local_iteration": local_iteration,
        "family": hypothesis.get("family") or "unspecified",
        "idea_id": hypothesis.get("idea_id") or hypothesis.get("id") or "",
        "variant": hypothesis.get("implementation_variant") or hypothesis.get("title") or "",
        "scope": hypothesis.get("scope") or "",
        "target": hypothesis.get("target") or "",
        "accepted": bool(accepted),
        "evaluated": bool(evaluation) or fallback_accepted is not None,
        "event_kept": event_kept,
        "score": score,
        "display_metric": display_metric,
        "display_value": display_value,
        "primary_metric": primary_metric,
        "primary_value": primary_value,
        "metrics": metrics,
        "summary": summary,
        "status": node.get("status") or "",
        "error": node.get("error") or "",
        "timestamp": event_timestamp,
        "outcome_key": "reverted",
        "outcome": _outcome_label("reverted"),
        "refreshes_best": False,
        "evidence_paths": list(_dict_value(evaluation.get("raw_log_paths")).values()),
    }


def _build_branches(
    nodes: dict[str, Any],
    attempts: list[dict[str, Any]],
    motifs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    motif_by_node = {str(item.get("node_id")): item for item in motifs if item.get("node_id")}
    attempts_by_node: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_node.setdefault(str(attempt.get("branch_id")), []).append(attempt)
    branches = []
    for node_id, raw_node in sorted(nodes.items()):
        node = _dict_value(raw_node)
        hypothesis = _dict_value(node.get("hypothesis"))
        if hypothesis.get("id") == "baseline":
            continue
        node_attempts = attempts_by_node.get(node_id, [])
        motif = _dict_value(motif_by_node.get(node_id))
        final_attempt = node_attempts[-1] if node_attempts else {}
        branches.append(
            {
                "branch_id": node_id,
                "parent_id": node.get("parent_id"),
                "round": int(node.get("depth") or 0),
                "family": hypothesis.get("family") or "unspecified",
                "idea_id": hypothesis.get("idea_id") or hypothesis.get("id") or "",
                "variant": hypothesis.get("implementation_variant") or hypothesis.get("title") or "",
                "target": hypothesis.get("target") or "",
                "status": node.get("status") or "",
                "outcome": final_attempt.get("outcome_key") or motif.get("outcome") or "",
                "attempt_count": sum(item["outcome_key"] not in {"skipped_budget", "pending"} for item in node_attempts),
                "final_score": final_attempt.get("score"),
                "final_display_value": final_attempt.get("display_value"),
                "human_summary": _branch_summary(node_id, hypothesis, final_attempt),
            }
        )
    return branches


def _build_run_summary(
    *,
    run_dir: Path,
    state: dict[str, Any],
    task: dict[str, Any],
    config: dict[str, Any],
    budget: dict[str, Any],
    final_result: dict[str, Any],
    attempts: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    search_counts: dict[str, int | None],
    primary_metric: dict[str, Any],
    display_metric: dict[str, Any],
) -> dict[str, Any]:
    non_baseline = [item for item in attempts if item.get("outcome_key") not in {"baseline", "skipped_budget", "pending"}]
    kept = [item for item in non_baseline if item.get("outcome_key") == "kept"]
    failed = [item for item in non_baseline if item.get("outcome_key") in FAILURE_OUTCOMES]
    reverted = [item for item in non_baseline if item not in kept and item not in failed]
    baseline = next((item for item in attempts if item.get("outcome_key") == "baseline"), None)
    memory = _dict_value(state.get("root_memory"))
    best_node_id = final_result.get("best_node_id") or memory.get("best_node_id")
    selected = _dict_value(_dict_value(state.get("nodes")).get(best_node_id))
    selected_evaluation = _dict_value(selected.get("evaluation_result"))
    selected_value = _number(_dict_value(selected_evaluation.get("metrics")).get(display_metric["key"]))
    selected_score = _number(selected_evaluation.get("score"))
    if selected_value is None and display_metric["key"] in {"score", "score_value", "ranking_score"}:
        selected_value = selected_score
    selected_step = next(
        (item for item in attempts if item["branch_id"] == best_node_id and item["score"] == selected_score), None,
    )
    usage = _dict_value(budget.get("agent_usage"))
    title = _dashboard_title(task)
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "title": title,
        "task_type": task.get("task_type") or task.get("type") or "",
        "objective": task.get("objective") or "",
        "target_path": task.get("target_path") or "",
        "config": {
            "beam_width": config.get("beam_width"),
            "max_rounds": config.get("max_rounds"),
            "children_per_expansion": config.get("children_per_expansion"),
            "max_parallel_branches": config.get("max_parallel_branches"),
        },
        "primary_metric": primary_metric,
        "display_metric": display_metric,
        "baseline": {
            "node_id": baseline.get("branch_id") if baseline else None,
            "score": baseline.get("score") if baseline else state.get("root_memory", {}).get("baseline_score"),
            "display_value": baseline.get("display_value") if baseline else None,
        },
        "best": {
            "node_id": best_node_id,
            "experiment_index": selected_step["experiment_index"] if selected_step else None,
            "score": selected_score if selected_score is not None else memory.get("best_score"),
            "display_value": selected_value,
        },
        "search_counts": search_counts,
        "counts": {
            "实验次数": len(non_baseline),
            "变更被采纳": len(kept),
            "变更被回退": len(reverted),
            "失败次数": len(failed),
            "分支数": len(branches),
        },
        "cost": {
            "API 调用量": usage.get("api_calls", 0),
            "令牌使用量": usage.get("total_tokens", 0),
            "推理成本": usage.get("cost_usd", 0.0),
            "agent 耗时": usage.get("agent_seconds", 0.0),
        },
    }


def _build_human_timeline(
    run_summary: dict[str, Any],
    attempts: list[dict[str, Any]],
    branches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    timeline = [
        {
            "kind": "run_summary",
            "title": "Search overview",
            "summary": _summary_line(run_summary),
            "severity": "info",
        }
    ]
    for item in attempts:
        if item.get("outcome_key") == "baseline":
            continue
        title = f"Attempt {item.get('experiment_index')} · {item.get('variant') or item.get('family')}"
        display = _format_metric_value(item.get("display_value"), _dict_value(item.get("display_metric")))
        timeline.append(
            {
                "kind": "attempt_result",
                "title": title,
                "summary": f"{item.get('outcome')} · {_dict_value(item.get('display_metric')).get('label', 'Metric')} {display}",
                "severity": "success" if item.get("outcome_key") == "kept" else "error" if item.get("outcome_key") in {"failed", "timeout"} else "warning",
                "branch_id": item.get("branch_id"),
                "experiment_index": item.get("experiment_index"),
            }
        )
    return timeline


def _collect_metric_keys(nodes: dict[str, Any], events: list[dict[str, Any]]) -> set[str]:
    keys = {"score"}
    for node in nodes.values():
        evaluation = _dict_value(_dict_value(node).get("evaluation_result"))
        metrics = _dict_value(evaluation.get("metrics"))
        keys.update(metrics.keys())
        if evaluation.get("score") is not None:
            keys.add("score")
    for event in events:
        payload = _dict_value(event.get("payload"))
        keys.update(_dict_value(payload.get("metrics")))
    return {str(key) for key in keys if key}


def _select_metric(
    metric_keys: set[str],
    priority: list[str],
    state: dict[str, Any],
    *,
    preferred_role: str,
) -> str:
    configured = _dict_value(_dict_value(state.get("task_snapshot")).get("metadata")).get("score_metric")
    if configured and configured in metric_keys:
        return str(configured)
    for key in priority:
        if key in metric_keys:
            return key
    return "score"


def _metric_spec(key: str) -> dict[str, Any]:
    label, unit, role = METRIC_LABELS.get(key, (key, "", "display"))
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "direction": "lower" if key in LOWER_IS_BETTER else "higher",
        "role": role,
    }


def _metrics_catalog(
    metric_keys: set[str],
    primary_metric: dict[str, Any],
    display_metric: dict[str, Any],
) -> dict[str, Any]:
    catalog = {key: _metric_spec(key) for key in sorted(metric_keys)}
    catalog[primary_metric["key"]]["role"] = "objective"
    catalog[display_metric["key"]]["role"] = "display"
    return catalog


def _failure_outcome(attempt: dict[str, Any], node: dict[str, Any]) -> str:
    text = " ".join(str(value or "") for value in [attempt.get("summary"), attempt.get("error"), node.get("error")]).lower()
    metrics = _dict_value(attempt.get("metrics"))
    correctness = str(metrics.get("correctness", "")).upper()
    failure = str(metrics.get("verification_failure_reason") or "")
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "compile" in text and ("fail" in text or "error" in text):
        return "rejected_compile"
    if failure in {"candidate_failed", "reference_failed"}:
        return "rejected_runtime"
    if ((correctness and correctness != "PASS")
            or failure.startswith(("correctness_", "numerical_")) or failure == "invalid_error_metric"):
        return "rejected_correctness"
    if node.get("status") == "failed":
        return "failed"
    return "reverted"


def _outcome_label(key: str) -> str:
    return {
        "baseline": "Baseline",
        "kept": "Kept",
        "reverted": "Reverted",
        "failed": "Failed",
        "timeout": "Timed out",
        "rejected_compile": "Compilation failed",
        "rejected_runtime": "Execution failed",
        "rejected_correctness": "Correctness failed",
        "skipped_budget": "Skipped by budget",
        "pending": "Proposed",
    }.get(key, key)


def _dashboard_title(task: dict[str, Any]) -> str:
    task_type = str(task.get("task_type") or task.get("type") or "Run")
    if task_type == "autokernel":
        return "AutoKernel: Autonomous GPU Kernel Optimization"
    if task_type == "kernelbench":
        return "KernelBench Optimization Run"
    if task_type in {"pipeline", "Run"}:
        return "AutoPipeline run report"
    return f"{task_type.title()} Optimization Run"


def _summary_line(run_summary: dict[str, Any]) -> str:
    counts = _dict_value(run_summary.get("counts"))
    baseline = _dict_value(run_summary.get("baseline"))
    best = _dict_value(run_summary.get("best"))
    display_metric = _dict_value(run_summary.get("display_metric"))
    label = str(display_metric.get("label") or "Metric")
    base = _format_metric_value(baseline.get("display_value"), display_metric)
    final = _format_metric_value(best.get("display_value"), display_metric)
    return (
        f"{counts.get('实验次数', 0)} attempts · {counts.get('变更被采纳', 0)} kept · "
        f"{counts.get('变更被回退', 0)} reverted · {counts.get('失败次数', 0)} failed · "
        f"{label} {base} → {final}"
    )


def _format_metric_value(value: Any, metric: dict[str, Any]) -> str:
    unit = str(metric.get("unit") or "")
    return f"{_format_number(value)} {unit}".strip() if value is not None else "Unavailable"


def _format_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    if abs(number) >= 100:
        return f"{number:.1f}"
    if abs(number) >= 10:
        return f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _is_better(value: float | int | None, current: float | int | None, direction: str) -> bool:
    if value is None:
        return False
    if current is None:
        return True
    return float(value) < float(current) if direction == "lower" else float(value) > float(current)


def _branch_summary(node_id: str, hypothesis: dict[str, Any], final_attempt: dict[str, Any]) -> str:
    title = hypothesis.get("title") or hypothesis.get("id") or node_id
    outcome = final_attempt.get("outcome") or "Awaiting evaluation"
    value = _format_metric_value(final_attempt.get("display_value"), _dict_value(final_attempt.get("display_metric")))
    return f"{title} · {outcome} · {value}"


def _empty_svg(width: int, height: int, message: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-family="sans-serif" font-size="20">{escape(message)}</text>'
        "</svg>"
    )


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
