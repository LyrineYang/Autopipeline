from __future__ import annotations

from typing import Any

from .types import RootMemory, SearchNode, TaskSnapshot


def build_verifier_summary(
    *,
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    recent_probe_results: list[dict[str, Any]],
    parent: SearchNode | None = None,
    node: SearchNode | None = None,
    task: Any | None = None,
    sensing_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed = [item for item in recent_probe_results if item.get("status") == "completed"]
    failed = [item for item in recent_probe_results if item.get("status") == "failed"]
    rejected = [item for item in recent_probe_results if item.get("status") == "rejected"]
    objective_evaluation = _objective_evaluation_from_sensing(sensing_summary)

    evidence = _summaries(completed, limit=4)
    regressions = _summaries(failed + rejected, limit=4)
    uncertainty: list[str] = []

    verdict = "need_more_evidence"
    status = "healthy"

    if node is not None and node.execution_result is not None and node.execution_result.status.lower() == "failed":
        verdict = "reject"
        status = "blocked"
        regressions.insert(0, node.execution_result.summary)
    elif node is not None and node.evaluation_result is not None:
        if node.evaluation_result.accepted:
            verdict = "keep"
            evidence.insert(0, node.evaluation_result.summary)
        else:
            verdict = "reject"
            regressions.insert(0, node.evaluation_result.summary)
        metric_evidence, metric_regressions = _metric_lines(
            node=node,
            objective_signals=task_snapshot.objective_signals,
        )
        evidence.extend(metric_evidence)
        regressions.extend(metric_regressions)
        if objective_evaluation:
            evidence.extend(_string_list(objective_evaluation.get("evidence")))
            regressions.extend(_string_list(objective_evaluation.get("regressions")))
            uncertainty.extend(_string_list(objective_evaluation.get("uncertainty")))
            objective_verdict = str(objective_evaluation.get("verdict", "need_more_evidence"))
            if objective_verdict == "reject":
                verdict = "reject"
                status = "warning" if status == "healthy" else status
            elif verdict != "reject":
                verdict = objective_verdict
    elif failed and not completed:
        verdict = "need_more_evidence"
        status = "blocked"
        uncertainty.append("Recent diagnostics failed before producing usable evidence.")
    elif not completed:
        uncertainty.append("No completed diagnostic evidence is available yet.")

    if failed or rejected:
        status = "warning" if status == "healthy" else status
        uncertainty.append("Some diagnostic requests failed or were rejected.")

    if node is None or node.evaluation_result is None:
        uncertainty.append("No accepted optimization result has been verified yet.")

    if parent is not None and parent.evaluation_result is not None:
        evidence.append(f"Parent score: {parent.evaluation_result.score:.3f}")
    if root.best_score is not None:
        evidence.append(f"Best known score: {root.best_score:.3f}")

    customizer = getattr(task, "augment_verifier_summary", None)
    if callable(customizer):
        custom = customizer(
            task_snapshot=task_snapshot,
            root=root,
            recent_probe_results=recent_probe_results,
            parent=parent,
            node=node,
        )
        if isinstance(custom, dict):
            verdict = str(custom.get("verdict", verdict))
            status = str(custom.get("status", status))
            evidence.extend(_string_list(custom.get("evidence")))
            regressions.extend(_string_list(custom.get("regressions")))
            uncertainty.extend(_string_list(custom.get("uncertainty")))

    return {
        "status": status,
        "verdict": verdict,
        "objective": task_snapshot.objective,
        "evidence": _dedupe(evidence)[:5],
        "regressions": _dedupe(regressions)[:5],
        "uncertainty": _dedupe(uncertainty)[:5],
        "objective_evaluation": objective_evaluation,
        "probe_health": {
            "probe_count": len(recent_probe_results),
            "completed_count": len(completed),
            "failed_count": len(failed),
            "rejected_count": len(rejected),
        },
    }


def _summaries(items: list[dict[str, Any]], *, limit: int) -> list[str]:
    summaries = [str(item.get("summary", "")).strip() for item in items if item.get("summary")]
    return [item for item in summaries if item][:limit]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _metric_lines(*, node: SearchNode, objective_signals: list[str]) -> tuple[list[str], list[str]]:
    if node.evaluation_result is None:
        return [], []
    metrics = node.evaluation_result.metrics
    evidence: list[str] = []
    regressions: list[str] = []
    available_signals = set(metrics.keys())
    stage_summary = metrics.get("stage_summary")
    stage_metrics = stage_summary if isinstance(stage_summary, dict) else {}
    available_signals.update(str(key) for key in stage_metrics.keys())

    def emit_signal(signal: str) -> None:
        if signal in metrics:
            evidence.append(f"{signal}={metrics[signal]}")
        elif signal in stage_metrics:
            evidence.append(f"stage_summary.{signal}={stage_metrics[signal]}")

    for signal in objective_signals:
        if signal:
            emit_signal(signal)

    scalar_keys = [
        "score_metric",
        "score_value",
        "step_time_ms",
        "end_to_end_ms",
        "throughput",
        "tokens_per_second",
        "cost_per_token",
        "gpu_utilization",
        "hardware_fit",
    ]
    for key in scalar_keys:
        value = metrics.get(key)
        if value is None:
            continue
        emit_signal(key)
    if stage_metrics:
        for key in ("slowest_stage", "slowest_stage_ms", "communication_ms", "overlap_ratio"):
            if key in stage_metrics:
                emit_signal(key)
    missing = [signal for signal in objective_signals if signal and signal not in available_signals]
    if missing:
        regressions.append(
            f"Missing objective signals in evaluation metrics: {', '.join(missing[:4])}"
        )
    return _dedupe(evidence), regressions


def _objective_evaluation_from_sensing(sensing_summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sensing_summary, dict):
        return None
    objective = sensing_summary.get("objective_evaluation")
    if isinstance(objective, dict):
        return objective
    pipeline = sensing_summary.get("pipeline_evidence")
    if isinstance(pipeline, dict):
        return pipeline
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
