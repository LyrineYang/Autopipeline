from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
from pathlib import Path
from typing import Any

from .run_artifacts import count_candidates, format_counts, read_events, read_object, was_skipped_for_budget


METRIC_LABELS = {
    "correctness": ("正确性", "", "gate"),
    "speedup_vs_pytorch": ("相对 PyTorch 加速比", "x", "objective"),
    "speedup": ("加速比", "x", "objective"),
    "throughput_tflops": ("吞吐量", "TFLOPS", "display"),
    "bandwidth_gb_s": ("带宽", "GB/s", "display"),
    "forward_latency_ms": ("Forward latency", "ms", "objective"),
    "end_to_end_ms": ("端到端延迟", "ms", "objective"),
    "latency_ms": ("延迟", "ms", "objective"),
    "tokens_per_second": ("token 吞吐量", "tok/s", "objective"),
    "cost_per_token": ("每 token 成本", "", "objective"),
    "bench_time_seconds": ("基准测试耗时", "s", "cost"),
    "ranking_score": ("Ranking score", "", "objective"),
    "score_value": ("评分值", "", "objective"),
    "score": ("评分值", "", "objective"),
}

LOWER_IS_BETTER = {"forward_latency_ms", "end_to_end_ms", "latency_ms", "cost_per_token", "bench_time_seconds"}
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
    human_timeline = _build_human_timeline(run_summary, attempts, branches)

    return {
        "run_summary": run_summary,
        "branches": branches,
        "attempts": attempts,
        "metrics_catalog": metrics_catalog,
        "human_timeline": human_timeline,
        "family_summary": family_summary,
    }


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
    width = 1480
    height = 780
    margin_left = 92
    margin_right = 56
    margin_top = 138
    margin_bottom = 82
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    display_metric = _dict_value(run_summary.get("display_metric"))
    label = str(display_metric.get("label") or "评分值")
    unit = str(display_metric.get("unit") or "")
    direction = str(display_metric.get("direction") or "higher")

    points = [item for item in attempts if _number(item.get("display_value")) is not None]
    if not points:
        return _empty_svg(width, height, "没有可绘制的实验指标")

    values = [float(item["display_value"]) for item in points]
    baseline_value = _number(_dict_value(run_summary.get("baseline")).get("display_value"))
    if baseline_value is not None:
        values.append(float(baseline_value))
    y_min = min(values)
    y_max = max(values)
    if y_min == y_max:
        y_min -= 1.0
        y_max += 1.0
    padding = (y_max - y_min) * 0.08
    y_min = max(0.0, y_min - padding) if y_min >= 0 else y_min - padding
    y_max = y_max + padding

    x_min = min(int(item["experiment_index"]) for item in points)
    x_max = max(int(item["experiment_index"]) for item in points)
    if x_min == x_max:
        x_max = x_min + 1

    def x_scale(index: int) -> float:
        return margin_left + (index - x_min) / max(1, x_max - x_min) * plot_w

    def y_scale(value: float) -> float:
        return margin_top + (y_max - value) / max(1e-9, y_max - y_min) * plot_h

    family_segments = _family_segments(points)
    family_colors = [
        "#f3f7ff",
        "#faf5ff",
        "#fff8ea",
        "#fff1f2",
        "#effaf6",
        "#f8fafc",
    ]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;fill:#263043}",
        ".muted{fill:#64748b}.small{font-size:13px}.axis{stroke:#dbe3ef;stroke-width:1}.grid{stroke:#eef2f7;stroke-width:1}",
        ".best{fill:none;stroke:#19b58b;stroke-width:3}.baseline{stroke:#818cf8;stroke-width:2;stroke-dasharray:8 6}",
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width/2}" y="48" font-size="26" font-weight="700" text-anchor="middle">{escape(str(run_summary.get("title", "AutoKernel Run Dashboard")))}</text>',
        f'<text x="{width/2}" y="88" font-size="16" class="muted" text-anchor="middle">{escape(_summary_line(run_summary))}</text>',
    ]
    for idx, segment in enumerate(family_segments):
        start_x = x_scale(segment["start"]) - 8
        end_x = x_scale(segment["end"]) + 8
        color = family_colors[idx % len(family_colors)]
        parts.append(
            f'<rect x="{start_x:.1f}" y="{margin_top}" width="{max(1, end_x-start_x):.1f}" height="{plot_h}" fill="{color}" opacity="0.82"/>'
        )
        parts.append(
            f'<text x="{(start_x+end_x)/2:.1f}" y="{margin_top+28}" font-size="13" font-weight="700" fill="#64748b" text-anchor="middle">{escape(segment["family"])}</text>'
        )

    for i in range(6):
        y = margin_top + plot_h * i / 5
        value = y_max - (y_max - y_min) * i / 5
        parts.append(f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width-margin_right}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{margin_left-12}" y="{y+5:.1f}" text-anchor="end" font-size="13" class="muted">{_format_number(value)}</text>')

    for i in range(0, x_max - x_min + 1, max(1, (x_max - x_min) // 8 or 1)):
        x = x_scale(x_min + i)
        parts.append(f'<line x1="{x:.1f}" y1="{margin_top}" x2="{x:.1f}" y2="{margin_top+plot_h}" class="grid" opacity="0.55"/>')
        parts.append(f'<text x="{x:.1f}" y="{height-46}" text-anchor="middle" font-size="13" class="muted">{x_min+i}</text>')

    parts.append(f'<line x1="{margin_left}" y1="{margin_top+plot_h}" x2="{width-margin_right}" y2="{margin_top+plot_h}" class="axis"/>')
    parts.append(f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{margin_top+plot_h}" class="axis"/>')
    parts.append(f'<text x="{width/2}" y="{height-16}" text-anchor="middle" font-size="15" class="muted">实验 attempt #</text>')
    parts.append(
        f'<text transform="translate(30 {margin_top+plot_h/2}) rotate(-90)" text-anchor="middle" font-size="15" class="muted">{escape(label + (f" ({unit})" if unit else ""))}</text>'
    )

    if baseline_value is not None:
        y = y_scale(float(baseline_value))
        parts.append(f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width-margin_right}" y2="{y:.1f}" class="baseline"/>')
        parts.append(
            f'<text x="{width-margin_right-8}" y="{y-8:.1f}" text-anchor="end" font-size="13" fill="#6366f1">baseline {_format_number(float(baseline_value))}{escape(unit)}</text>'
        )

    best_points = []
    best_value = None
    for item in sorted(points, key=lambda entry: int(entry["experiment_index"])):
        value = float(item["display_value"])
        if _is_better(value, best_value, direction):
            best_value = value
        best_points.append((x_scale(int(item["experiment_index"])), y_scale(float(best_value))))
    path = " ".join(
        ("M" if idx == 0 else "L") + f"{x:.1f},{y:.1f}"
        for idx, (x, y) in enumerate(best_points)
    )
    parts.append(f'<path d="{path}" class="best"/>')

    for item in points:
        x = x_scale(int(item["experiment_index"]))
        y = y_scale(float(item["display_value"]))
        outcome = str(item.get("outcome_key", ""))
        if outcome in {"failed", "timeout", "rejected_compile", "rejected_runtime", "rejected_correctness"}:
            parts.append(f'<line x1="{x-6:.1f}" y1="{y-6:.1f}" x2="{x+6:.1f}" y2="{y+6:.1f}" stroke="#ef4444" stroke-width="3"/>')
            parts.append(f'<line x1="{x+6:.1f}" y1="{y-6:.1f}" x2="{x-6:.1f}" y2="{y+6:.1f}" stroke="#ef4444" stroke-width="3"/>')
        elif outcome == "kept":
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="#10b981" stroke="#047857" stroke-width="1.3"/>')
        elif outcome == "baseline":
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="#6366f1" stroke="#4338ca" stroke-width="1.3"/>')
        else:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#cbd5e1" opacity="0.9"/>')

    best = _dict_value(run_summary.get("best"))
    if best.get("display_value") is not None:
        best_x = x_scale(int(best.get("experiment_index") or points[-1]["experiment_index"]))
        best_y = y_scale(float(best["display_value"]))
        label_text = f'{_format_number(float(best["display_value"]))}{unit}'
        parts.append(
            f'<rect x="{best_x+10:.1f}" y="{best_y-24:.1f}" width="126" height="34" rx="6" fill="#ecfdf5" stroke="#10b981"/>'
        )
        parts.append(
            f'<text x="{best_x+18:.1f}" y="{best_y-3:.1f}" font-size="14" font-weight="700" fill="#047857">{escape(label_text)}</text>'
        )

    legend_x = width - margin_right - 224
    legend_y = height - margin_bottom - 88
    parts.extend(
        [
            f'<rect x="{legend_x}" y="{legend_y}" width="218" height="78" rx="6" fill="#ffffff" stroke="#dbe3ef" opacity="0.94"/>',
            f'<circle cx="{legend_x+18}" cy="{legend_y+18}" r="5" fill="#10b981"/><text x="{legend_x+32}" y="{legend_y+23}" class="small">变更被采纳</text>',
            f'<circle cx="{legend_x+18}" cy="{legend_y+40}" r="4" fill="#cbd5e1"/><text x="{legend_x+32}" y="{legend_y+45}" class="small">变更被回退 / 无收益</text>',
            f'<line x1="{legend_x+13}" y1="{legend_y+57}" x2="{legend_x+23}" y2="{legend_y+67}" stroke="#ef4444" stroke-width="3"/><line x1="{legend_x+23}" y1="{legend_y+57}" x2="{legend_x+13}" y2="{legend_y+67}" stroke="#ef4444" stroke-width="3"/><text x="{legend_x+32}" y="{legend_y+66}" class="small">失败 / 超时</text>',
        ]
    )
    parts.append("</svg>")
    return "\n".join(parts)


def render_dashboard_html(view: dict[str, Any], *, iteration_effect_name: str) -> str:
    summary = view["run_summary"]
    branches = view["branches"]
    attempts = view["attempts"]
    families = _family_cards(branches)
    cards = [
        ("实验次数", summary["counts"].get("实验次数", 0)),
        ("变更被采纳", summary["counts"].get("变更被采纳", 0)),
        ("变更被回退", summary["counts"].get("变更被回退", 0)),
        ("失败次数", summary["counts"].get("失败次数", 0)),
        (_dict_value(summary.get("display_metric")).get("label", "当前最好"), _best_display(summary)),
        ("推理成本", _cost_display(summary)),
    ]
    family_rows = "\n".join(
        "<tr>"
        f"<td>{escape(item['family'])}</td>"
        f"<td>{item['attempts']}</td>"
        f"<td>{item['kept']}</td>"
        f"<td>{item['failed']}</td>"
        f"<td>{_format_number(item['best_delta'])}</td>"
        "</tr>"
        for item in families
    )
    attempt_rows = "\n".join(
        "<tr>"
        f"<td>{item.get('experiment_index')}</td>"
        f"<td>{escape(str(item.get('branch_id') or ''))}</td>"
        f"<td>{escape(str(item.get('family') or ''))}</td>"
        f"<td>{escape(str(item.get('outcome') or ''))}</td>"
        f"<td>{escape(_format_number(item.get('display_value')))}</td>"
        f"<td>{escape(str(item.get('summary') or ''))}</td>"
        "</tr>"
        for item in attempts[-18:]
    )
    card_html = "\n".join(
        f'<section class="card"><div class="label">{escape(str(label))}</div><div class="value">{escape(str(value))}</div></section>'
        for label, value in cards
    )
    outcome = summary.get("outcome") or {}
    outcome_html = ""
    if outcome:
        outcome_html = '<section class="panel" aria-label="运行结果">' + "".join(
            f'<{tag} style="white-space:pre-wrap;overflow-wrap:anywhere">{escape(str(outcome.get(key) or ""))}</{tag}>'
            for key, tag in (("title", "h2"), ("detail", "p"), ("reason", "p"), ("next_step", "p"))
            if outcome.get(key)
        ) + "</section>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(str(summary.get("title", "Run Dashboard")))}</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#64748b; --line:#dbe3ef; --panel:#ffffff; --bg:#f6f8fb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ max-width:1500px; margin:0 auto; padding:28px 28px 40px; }}
    header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:18px; }}
    h1 {{ margin:0; font-size:28px; letter-spacing:0; }}
    .sub {{ color:var(--muted); margin-top:8px; font-size:15px; }}
    .cards {{ display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); gap:12px; margin:18px 0; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px; min-height:86px; }}
    .label {{ color:var(--muted); font-size:13px; margin-bottom:10px; }}
    .value {{ font-size:24px; font-weight:700; white-space:nowrap; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; margin-top:14px; }}
    .panel h2 {{ margin:0 0 12px; font-size:18px; }}
    .chart {{ width:100%; overflow:auto; }}
    .chart img {{ width:100%; min-width:980px; display:block; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th, td {{ padding:10px 8px; border-bottom:1px solid #edf1f7; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-weight:600; }}
    .grid {{ display:grid; grid-template-columns:1fr 1.4fr; gap:14px; }}
    @media (max-width: 1100px) {{ .cards {{ grid-template-columns:repeat(2, 1fr); }} .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>{escape(str(summary.get("title", "Run Dashboard")))}</h1>
      <div class="sub">{escape(_summary_line(summary))}</div>
    </div>
  </header>
  {outcome_html}
  <p class="sub">{escape(format_counts(summary["search_counts"]))}</p>
  <div class="cards">{card_html}</div>
  <section class="panel">
    <h2>迭代效果</h2>
    <div class="chart"><img src="{escape(iteration_effect_name)}" alt="iteration effect chart"></div>
  </section>
  <div class="grid">
    <section class="panel">
      <h2>优化方向效果</h2>
      <table><thead><tr><th>方向</th><th>尝试</th><th>采纳</th><th>失败</th><th>最好改善</th></tr></thead><tbody>{family_rows}</tbody></table>
    </section>
    <section class="panel">
      <h2>最近实验</h2>
      <table><thead><tr><th>#</th><th>节点</th><th>方向</th><th>结果</th><th>指标</th><th>摘要</th></tr></thead><tbody>{attempt_rows}</tbody></table>
    </section>
  </div>
</main>
</body>
</html>
"""


def render_summary_markdown(view: dict[str, Any]) -> str:
    summary = view["run_summary"]
    timeline = view["human_timeline"][:8]
    lines = [
        f"# {summary.get('title', 'Run Dashboard')}",
        "",
        _summary_line(summary),
        "",
        format_counts(summary["search_counts"]),
        "",
        "## 关键结果",
        "",
        f"- 实验次数：{summary['counts'].get('实验次数', 0)}",
        f"- 变更被采纳：{summary['counts'].get('变更被采纳', 0)}",
        f"- 变更被回退：{summary['counts'].get('变更被回退', 0)}",
        f"- 失败次数：{summary['counts'].get('失败次数', 0)}",
        f"- 当前最好：{_best_display(summary)}",
        f"- 成本：{_cost_display(summary)}",
        "",
        "## 时间线",
        "",
    ]
    outcome = summary.get("outcome") or {}
    if outcome:
        lines[2:2] = [str(outcome[key]) for key in ("title", "detail", "reason", "next_step") if outcome.get(key)] + [""]
    for item in timeline:
        lines.append(f"- {item['title']}：{item['summary']}")
    lines.append("")
    return "\n".join(lines)


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
            attempt["outcome"] = "基线"
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
        if attempt["outcome_key"] == "baseline":
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
            attempt["outcome_key"] = "failed"
            attempt["outcome"] = "失败"
        elif not accepted:
            attempt["outcome_key"] = _failure_outcome(attempt, node)
            attempt["outcome"] = _outcome_label(attempt["outcome_key"])
        elif parent_score is not None and score is not None and score > parent_score:
            attempt["outcome_key"] = "kept"
            attempt["outcome"] = "变更被采纳"
        else:
            attempt["outcome_key"] = "reverted"
            attempt["outcome"] = "变更被回退"
        display_value = _number(attempt.get("display_value"))
        if display_value is None and best_display is not None:
            display_value = float(best_display)
            attempt["display_value"] = display_value
            attempt["display_imputed"] = True
        if accepted and display_value is not None and _is_better(display_value, best_display, direction):
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
) -> dict[str, Any]:
    hypothesis = _dict_value(node.get("hypothesis"))
    evaluation = _dict_value(node.get("evaluation_result"))
    metrics = _dict_value(evaluation.get("metrics"))
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
        "outcome": "变更被回退",
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
    failed = [
        item
        for item in non_baseline
        if item.get("outcome_key") in {"failed", "timeout", "rejected_compile", "rejected_runtime", "rejected_correctness"}
    ]
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
            "title": "运行总览",
            "summary": _summary_line(run_summary),
            "severity": "info",
        }
    ]
    for item in attempts:
        if item.get("outcome_key") == "baseline":
            continue
        title = f"第 {item.get('experiment_index')} 次实验：{item.get('variant') or item.get('family')}"
        display = _format_metric_value(item.get("display_value"), _dict_value(item.get("display_metric")))
        timeline.append(
            {
                "kind": "attempt_result",
                "title": title,
                "summary": f"{item.get('outcome')}；{_dict_value(item.get('display_metric')).get('label', '指标')} {display}。",
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
        if payload.get("score") is not None:
            keys.add("score")
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
    correctness = str(_dict_value(attempt.get("metrics")).get("correctness", "")).upper()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "compile" in text and ("fail" in text or "error" in text):
        return "rejected_compile"
    if correctness and correctness != "PASS":
        return "rejected_correctness"
    if node.get("status") == "failed":
        return "failed"
    return "reverted"


def _outcome_label(key: str) -> str:
    return {
        "baseline": "基线",
        "kept": "变更被采纳",
        "reverted": "变更被回退",
        "failed": "失败",
        "timeout": "超时",
        "rejected_compile": "编译失败",
        "rejected_runtime": "运行失败",
        "rejected_correctness": "正确性失败",
    }.get(key, key)


def _family_segments(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not points:
        return []
    segments = []
    current = str(points[0].get("family") or "unspecified")
    start = int(points[0]["experiment_index"])
    end = start
    for item in points[1:]:
        family = str(item.get("family") or "unspecified")
        index = int(item["experiment_index"])
        if family != current:
            segments.append({"family": current, "start": start, "end": end})
            current = family
            start = index
        end = index
    segments.append({"family": current, "start": start, "end": end})
    return segments


def _family_cards(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for branch in branches:
        family = str(branch.get("family") or "unspecified")
        row = rows.setdefault(family, {"family": family, "attempts": 0, "kept": 0, "failed": 0, "best_delta": 0.0})
        row["attempts"] += int(branch.get("attempt_count") or 0)
        if str(branch.get("outcome")) in {"accepted_improved", "kept"}:
            row["kept"] += 1
        if str(branch.get("status")) == "failed":
            row["failed"] += 1
        score = _number(branch.get("final_score"))
        if score is not None:
            row["best_delta"] = max(float(row["best_delta"]), score)
    return sorted(rows.values(), key=lambda item: (-int(item["kept"]), -int(item["attempts"]), item["family"]))


def _dashboard_title(task: dict[str, Any]) -> str:
    task_type = str(task.get("task_type") or task.get("type") or "Run")
    if task_type == "autokernel":
        return "AutoKernel: Autonomous GPU Kernel Optimization"
    if task_type == "kernelbench":
        return "KernelBench Optimization Run"
    if task_type == "pipeline":
        return "Pipeline Optimization Run"
    return f"{task_type.title()} Optimization Run"


def _summary_line(run_summary: dict[str, Any]) -> str:
    counts = _dict_value(run_summary.get("counts"))
    baseline = _dict_value(run_summary.get("baseline"))
    best = _dict_value(run_summary.get("best"))
    display_metric = _dict_value(run_summary.get("display_metric"))
    unit = str(display_metric.get("unit") or "")
    label = str(display_metric.get("label") or "指标")
    base = _format_number(baseline.get("display_value"))
    final = _format_number(best.get("display_value"))
    return (
        f"{counts.get('实验次数', 0)} 次实验 | "
        f"{counts.get('变更被采纳', 0)} 次变更被采纳 | "
        f"{counts.get('变更被回退', 0)} 次变更被回退 | "
        f"{counts.get('失败次数', 0)} 次失败 | "
        f"{label} {base} -> {final}{unit}"
    )


def _best_display(summary: dict[str, Any]) -> str:
    display = _dict_value(summary.get("display_metric"))
    value = _dict_value(summary.get("best")).get("display_value")
    return _format_metric_value(value, display)


def _cost_display(summary: dict[str, Any]) -> str:
    cost = _dict_value(summary.get("cost"))
    api_calls = cost.get("API 调用量", 0)
    tokens = cost.get("令牌使用量", 0)
    money = cost.get("推理成本", 0)
    return f"{api_calls} API | {_compact_int(tokens)} tokens | ${_format_number(money)}"


def _format_metric_value(value: Any, metric: dict[str, Any]) -> str:
    unit = str(metric.get("unit") or "")
    return f"{_format_number(value)}{unit}"


def _format_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    if abs(number) >= 100:
        return f"{number:.1f}"
    if abs(number) >= 10:
        return f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _compact_int(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "0"
    if number >= 1_000_000:
        return f"{number/1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number/1_000:.1f}k"
    return str(int(number))


def _is_better(value: float | int | None, current: float | int | None, direction: str) -> bool:
    if value is None:
        return False
    if current is None:
        return True
    return float(value) < float(current) if direction == "lower" else float(value) > float(current)


def _branch_summary(node_id: str, hypothesis: dict[str, Any], final_attempt: dict[str, Any]) -> str:
    title = hypothesis.get("title") or hypothesis.get("id") or node_id
    outcome = final_attempt.get("outcome") or "未评测"
    value = _format_metric_value(final_attempt.get("display_value"), _dict_value(final_attempt.get("display_metric")))
    return f"{title}: {outcome}，指标 {value}"


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
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
