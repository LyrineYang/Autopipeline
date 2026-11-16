"""Candidate directions from measured workspace evidence."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from autopipeline.search.types import Hypothesis, RootMemory, SearchNode, TaskSnapshot

from .region import build_pipeline_region_contract, region_work_unit
from .spec import PipelineSpec


def seed_hypotheses(spec: PipelineSpec, parent: SearchNode, root: RootMemory, limit: int) -> list[Hypothesis]:
    if spec.is_native:
        metrics = parent.evaluation_result.metrics if parent.evaluation_result else {}
        return _native_hypotheses(spec, parent, root, limit, metrics)
    return _pipeline_branch_cards(
        spec=spec,
        observation={},
        diagnoses=["end_to_end_latency"],
        treatment_families=["pipeline_latency", "stage_balance", "overlap"],
        limit=limit,
    )


def _native_hypotheses(
    spec: PipelineSpec, parent: SearchNode, root: RootMemory, limit: int, metrics: dict[str, Any],
) -> list[Hypothesis]:
    suffix = f":{parent.id}:{root.rounds_completed + 1}"
    target = {
        "scope": "pipeline",
        "target": spec.target_path,
        "editable_path_hints": list(spec.editable_paths or [spec.target_path]),
        "family": "pipeline_latency",
        "verification_intent": ["forward_latency_ms", "correctness"],
        "disallowed_directions": _pipeline_disallowed_directions(),
    }
    directions = [
        Hypothesis(
            id="reduce-forward-latency" + suffix,
            title="Reduce forward overhead",
            core_hypothesis="Reduce temporary allocations, data movement, or dispatch in the model forward.",
            implementation_variant="reduce-forward-overhead",
            idea_id="forward-overhead",
            **target,
        ),
    ]
    if (metrics.get("profiler") or {}).get("status") == "completed":
        directions.append(Hypothesis(
            id="reduce-operator-cost" + suffix,
            title="Reduce the cost of profiled operators",
            core_hypothesis="Use the observed operator profile to choose equivalent fused or vectorized operations.",
            implementation_variant="equivalent-operator-substitution",
            idea_id="operator-cost",
            supporting_evidence=["The current candidate has an operator profile."],
            **target,
        ))
    return directions[:limit]


def _native_context(
    spec: PipelineSpec, parent: SearchNode, root: RootMemory, limit: int,
    metrics: dict[str, Any], upper_bounds: dict[str, int] | None,
) -> dict[str, Any]:
    cards = _native_hypotheses(spec, parent, root, limit, metrics)
    editable_paths = list(spec.editable_paths or [spec.target_path])
    observation = {
        key: metrics[key] for key in ("forward_latency_ms", "throughput", "peak_vram_mb", "correctness")
        if key in metrics
    }
    if "forward_latency_ms" not in observation and "end_to_end_ms" in metrics:
        observation["forward_latency_ms"] = metrics["end_to_end_ms"]
    return {
        "static_prior": {"fixed_workload": spec.name},
        "pipeline_spec": str(spec.path),
        "editable_paths": editable_paths,
        "pipeline_observation": observation,
        "profiler": metrics.get("profiler", {}),
        "region_graph": None,
        "branch_cards": [asdict(card) for card in cards],
        "planner_rules": {
            "allowed_scopes": ["pipeline"], "allowed_families": ["pipeline_latency"],
            "editable_paths": editable_paths,
        },
        "search_policy": {
            "recommended_scope": "pipeline",
            "branch_budget": {
                "children": len(cards),
                "beam_width": min((upper_bounds or {}).get("beam_width", limit), len(cards)),
            },
        },
    }


def build_planner_context(
    spec: PipelineSpec,
    snapshot: TaskSnapshot,
    parent: SearchNode,
    root: RootMemory,
    limit: int,
    *,
    run_dir: Path | None,
    upper_bounds: dict[str, int] | None,
) -> dict[str, Any]:
    parent_metrics = parent.evaluation_result.metrics if parent.evaluation_result else {}
    if spec.is_native:
        return _native_context(spec, parent, root, limit, parent_metrics, upper_bounds)

    observation = _pipeline_observation(parent_metrics)
    diagnoses = _pipeline_diagnoses(observation)
    treatment_families = _pipeline_treatment_families(diagnoses)
    branch_limit = max(1, int(limit or 1))
    branch_cards = _pipeline_branch_cards(
        spec=spec,
        observation=observation,
        diagnoses=diagnoses,
        treatment_families=treatment_families,
        limit=branch_limit,
    )
    symptoms = _pipeline_symptoms(observation)
    focus_target = str(observation.get("slowest_stage") or spec.target_path)
    region_graph = _pipeline_region_graph(spec, observation, symptoms)
    bottleneck_card = {
        "scope": "pipeline",
        "target": focus_target,
        "symptoms": symptoms,
        "diagnoses": diagnoses,
        "response_families": treatment_families,
        "avoid_families": [],
        "rationale": _pipeline_rationale(observation, diagnoses, treatment_families),
    }
    hardware_context = _pipeline_hardware_context(spec)
    artifact = run_dir / "regions" / f"{parent.id}.region.json" if run_dir is not None else None
    region_artifact = str(artifact) if artifact is not None else ""
    bottleneck_region = build_pipeline_region_contract(
        task_name=spec.name,
        workspace_source=spec.workspace_source,
        target_path=spec.target_path,
        editable_paths=spec.editable_paths,
        reference_paths=spec.reference_paths,
        evaluate_command=spec.evaluate_command,
        metrics_path=spec.metrics_path,
        score_metric=spec.score_metric,
        score_direction=spec.score_direction,
        objective_signals=snapshot.objective_signals,
        region_graph=region_graph,
        bottleneck_card=bottleneck_card,
        observation=observation,
        hardware_context=hardware_context,
        artifact_path=region_artifact,
    )
    branch_cards = [
        _with_region_work_unit(card, bottleneck_region.to_dict(), region_artifact)
        for card in branch_cards
    ]
    if artifact is not None:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({
            "node_id": parent.id,
            "region_graph": region_graph,
            "bottleneck_region": bottleneck_region.to_dict(),
            "bottleneck_card": bottleneck_card,
            "observation": observation,
            "branch_cards": [asdict(card) for card in branch_cards],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    branch_budget = {
        "children": min(branch_limit, len(branch_cards)) if branch_cards else branch_limit,
        "beam_width": min(int((upper_bounds or {}).get("beam_width", branch_limit)), max(1, len(branch_cards))),
    }
    branch_budget.update(max_tree_depth=1, max_local_iterations=1)
    return {
        "task_kind": "pipeline",
        "pipeline_spec": str(spec.path),
        "editable_paths": list(spec.editable_paths),
        "objective_signals": snapshot.objective_signals,
        "metrics_path": spec.metrics_path,
        "score_metric": spec.score_metric,
        "score_direction": spec.score_direction,
        "static_prior": {
            "task_kind": "pipeline",
            "fixed_workload": spec.name,
            "pipeline_spec": str(spec.path),
            "score_metric": spec.score_metric,
            "score_direction": spec.score_direction,
        },
        "hardware_context": hardware_context,
        "history_context": {
            "best_score": root.best_score,
            "best_node_id": root.best_node_id,
            "rounds_completed": root.rounds_completed,
            "positive_insights": root.positive_insights[-5:],
            "negative_insights": root.negative_insights[-5:],
        },
        "pipeline_observation": observation,
        "profiler": parent_metrics.get("profiler", {"status": "unavailable", "reason": "Evaluator did not report a profiler"}),
        "symptoms": symptoms,
        "region_graph": region_graph,
        "bottleneck_card": bottleneck_card,
        "bottleneck_region": bottleneck_region.to_dict(),
        "branch_cards": [asdict(card) for card in branch_cards],
        "artifact_paths": {
            "region": region_artifact,
        },
        "planner_rules": {
            "allowed_scopes": ["pipeline"],
            "allowed_families": treatment_families,
            "editable_paths": list(spec.editable_paths or [spec.target_path]),
        },
        "search_policy": {
            "recommended_scope": "pipeline",
            "scope_options": ["pipeline"],
            "exploration_bias": "diagnosis-guided",
            "branch_budget": branch_budget,
            "resource_hint": {
                "tier": "lean",
                "notes": [
                    "Evaluate each candidate in its own copied workspace.",
                    "Use declared workload device and profiler evidence; serialize GPU measurements.",
                ],
            },
            "stop_guidance": ["Finish the edit once the proposed change has been tried."],
        },
        "frozen_decisions": [
            "Use the diagnosed pipeline bottleneck as the branch identity source.",
            "Executor may edit only declared pipeline workspace files.",
            "Evaluator metrics decide keep or reject; agent self-report is diagnostic only.",
        ],
        "agent_role": "modify declared pipeline workspace files; do not reinterpret raw traces manually",
    }



def _pipeline_observation(metrics: dict[str, Any]) -> dict[str, Any]:
    stage_summary = metrics.get("stage_summary")
    stage_metrics = stage_summary if isinstance(stage_summary, dict) else {}
    observation: dict[str, Any] = {
        "source": "parent_evaluation" if metrics else "missing_parent_metrics",
    }
    for key in (
        "end_to_end_ms",
        "throughput",
        "stage_imbalance",
        "communication_ms",
        "sync_ms",
        "memcpy_ms",
        "overlap_ratio",
        "memory_headroom",
        "hardware_fit",
        "output_value",
        "score_metric",
        "score_value",
    ):
        if key in metrics:
            observation[key] = metrics[key]
    for key in ("slowest_stage", "slowest_stage_ms", "communication_ms", "sync_ms", "memcpy_ms", "overlap_ratio"):
        if key in stage_metrics and key not in observation:
            observation[key] = stage_metrics[key]
    return observation


def _pipeline_diagnoses(observation: dict[str, Any]) -> list[str]:
    diagnoses: list[str] = []
    end_to_end = _optional_float(observation.get("end_to_end_ms")) or 0.0
    stage_imbalance = _optional_float(observation.get("stage_imbalance")) or 0.0
    communication = _optional_float(observation.get("communication_ms")) or 0.0
    sync = _optional_float(observation.get("sync_ms")) or 0.0
    overlap = _optional_float(observation.get("overlap_ratio"))
    memory_headroom = _optional_float(observation.get("memory_headroom"))

    if stage_imbalance >= 0.20:
        diagnoses.append("stage_imbalance")
    if end_to_end > 0 and communication / end_to_end >= 0.08:
        diagnoses.append("communication_stall")
    if end_to_end > 0 and sync / end_to_end >= 0.04:
        diagnoses.append("synchronization_stall")
    if overlap is not None and overlap < 0.60 and (communication > 0 or sync > 0):
        diagnoses.append("pipeline_overlap_failure")
    if memory_headroom is not None and memory_headroom < 0.20:
        diagnoses.append("memory_pressure")
    if not diagnoses and end_to_end > 0:
        diagnoses.append("end_to_end_latency")
    return diagnoses


def _pipeline_treatment_families(diagnoses: list[str]) -> list[str]:
    families: list[str] = ["pipeline_latency"]
    if "stage_imbalance" in diagnoses:
        families.append("stage_balance")
    if any(item in diagnoses for item in ("communication_stall", "synchronization_stall", "pipeline_overlap_failure")):
        families.append("overlap")
    if "memory_pressure" in diagnoses:
        families.append("memory_headroom")
    return _dedupe_strings([family for family in families if family in _SUPPORTED_PIPELINE_FAMILIES])


def _pipeline_symptoms(observation: dict[str, Any]) -> list[str]:
    symptoms: list[str] = []
    if observation.get("end_to_end_ms") is not None:
        symptoms.append(f"end_to_end_ms={observation['end_to_end_ms']}")
    if observation.get("slowest_stage") is not None:
        symptoms.append(f"slowest_stage={observation['slowest_stage']}:{observation.get('slowest_stage_ms', 'unknown')}ms")
    for key in ("stage_imbalance", "communication_ms", "sync_ms", "overlap_ratio", "memory_headroom", "hardware_fit"):
        if observation.get(key) is not None:
            symptoms.append(f"{key}={observation[key]}")
    return symptoms


def _pipeline_branch_cards(
    *,
    spec: PipelineSpec,
    observation: dict[str, Any],
    diagnoses: list[str],
    treatment_families: list[str],
    limit: int,
) -> list[Hypothesis]:
    symptoms = _pipeline_symptoms(observation)
    slowest_stage = str(observation.get("slowest_stage") or "slowest stage")
    editable_paths = list(spec.editable_paths or [spec.target_path])
    cards: list[Hypothesis] = []
    for family in treatment_families:
        if family == "pipeline_latency":
            cards.append(
                Hypothesis(
                    id="reduce-end-to-end",
                    title="Reduce end-to-end latency",
                    core_hypothesis="Reduce the measured end-to-end latency while preserving the evaluator contract.",
                    idea_id="end-to-end-latency",
                    implementation_variant="combined-stage-overlap-reduction",
                    scope="pipeline",
                    target=spec.target_path,
                    constraints_summary="Edit only declared pipeline workspace files and keep the metrics contract intact.",
                    editable_path_hints=editable_paths,
                    family="pipeline_latency",
                    rationale=_pipeline_rationale(observation, diagnoses, treatment_families),
                    target_signals=[*symptoms[:3], "end_to_end_ms", "throughput"],
                    verification_intent=["end_to_end_ms", "throughput"],
                    supporting_evidence=symptoms,
                    disallowed_directions=_pipeline_disallowed_directions(),
                )
            )
        elif family == "stage_balance":
            cards.append(
                Hypothesis(
                    id="balance-stages",
                    title="Balance the slowest stage",
                    core_hypothesis=f"Reduce latency in {slowest_stage} so the critical path is less stage-bound.",
                    idea_id="stage-balance",
                    implementation_variant="slowest-stage-latency-reduction",
                    scope="pipeline",
                    target=slowest_stage,
                    constraints_summary="Keep the pipeline runner and metrics schema unchanged.",
                    editable_path_hints=editable_paths,
                    family="stage_balance",
                    rationale="Stage imbalance indicates one region dominates the fixed workload.",
                    target_signals=[*symptoms[:3], "stage_imbalance", "slowest_stage_ms"],
                    verification_intent=["stage_imbalance", "slowest_stage_ms", "end_to_end_ms"],
                    supporting_evidence=symptoms,
                    disallowed_directions=_pipeline_disallowed_directions(),
                )
            )
        elif family == "overlap":
            cards.append(
                Hypothesis(
                    id="improve-overlap",
                    title="Improve overlap around communication and sync",
                    core_hypothesis="Reduce exposed communication and synchronization time by increasing useful overlap.",
                    idea_id="pipeline-overlap",
                    implementation_variant="communication-sync-overlap",
                    scope="pipeline",
                    target="communication-sync-boundary",
                    constraints_summary="Do not replace the evaluator with a profiler-only signal.",
                    editable_path_hints=editable_paths,
                    family="overlap",
                    rationale="Communication or synchronization exposure plus low overlap suggests a scheduling/overlap treatment.",
                    target_signals=[*symptoms[:3], "overlap_ratio", "communication_ms", "sync_ms"],
                    verification_intent=["overlap_ratio", "communication_ms", "sync_ms", "end_to_end_ms"],
                    supporting_evidence=symptoms,
                    disallowed_directions=_pipeline_disallowed_directions(),
                )
            )
    return cards[:limit]


def _pipeline_region_graph(spec: PipelineSpec, observation: dict[str, Any], symptoms: list[str]) -> dict[str, Any]:
    primary = {
        "id": "pipeline-critical-path",
        "label": spec.target_path,
        "scope": "pipeline",
        "summary": "Fixed-workload objective defined by the pipeline spec.",
        "signals": symptoms,
        "adjacent": [],
    }
    regions = [primary]
    if observation.get("slowest_stage"):
        regions.append({
            "id": "slowest-stage",
            "label": str(observation["slowest_stage"]),
            "scope": "stage",
            "summary": "Stage with the largest reported latency.",
            "signals": [item for item in symptoms if item.startswith(("slowest_stage", "stage_imbalance"))],
            "adjacent": [primary["id"]],
        })
    comm_signals = [
        item for item in symptoms if item.startswith(("communication_ms", "sync_ms", "overlap_ratio"))
    ]
    if comm_signals:
        regions.append({
            "id": "communication-sync-boundary",
            "label": "communication/synchronization boundary",
            "scope": "pipeline",
            "summary": "Reported communication, synchronization, and overlap signals.",
            "signals": comm_signals,
            "adjacent": [primary["id"]],
        })
    primary["adjacent"] = [region["id"] for region in regions[1:]]
    return {"focus_region_id": primary["id"], "regions": regions}


def _pipeline_rationale(
    observation: dict[str, Any],
    diagnoses: list[str],
    treatment_families: list[str],
) -> str:
    if not observation:
        return "No baseline metrics are available yet; start with conservative pipeline families."
    return (
        "Baseline metrics show "
        f"{', '.join(_pipeline_symptoms(observation)[:4])}; "
        f"diagnosis={', '.join(diagnoses) or 'none'}; "
        f"treatments={', '.join(treatment_families) or 'none'}."
    )


def _pipeline_hardware_context(spec: PipelineSpec) -> dict[str, Any]:
    metadata = spec.metadata or {}
    hardware = metadata.get("hardware_context")
    if isinstance(hardware, dict):
        return {**hardware, "scope": "fixed"}
    return {
        "scope": "fixed",
        "source": "pipeline_spec_or_local_fixture",
        "note": "No production hardware profiler output is attached to this spec.",
    }


def _with_region_work_unit(
    hypothesis: Hypothesis,
    bottleneck_region: dict[str, Any],
    artifact_path: str,
) -> Hypothesis:
    hypothesis.work_unit = region_work_unit(
        bottleneck_region,
        family=hypothesis.family,
        artifact_path=artifact_path,
    )
    return hypothesis


def _pipeline_disallowed_directions() -> list[str]:
    return [
        "Do not claim production trace analysis without trace input.",
        "Do not introduce cross-kernel fusion or layout propagation unless the spec exposes editable files for it.",
        "Do not change evaluator correctness or metrics semantics.",
    ]


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


_SUPPORTED_PIPELINE_FAMILIES = {
    "pipeline_latency",
    "stage_balance",
    "overlap",
    "memory_headroom",
}


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
