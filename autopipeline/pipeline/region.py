from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BottleneckRegionContract:
    schema_version: str
    id: str
    label: str
    scope: str
    summary: str
    inputs: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    reference: dict[str, Any] = field(default_factory=dict)
    candidate_surface: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    profile_evidence: dict[str, Any] = field(default_factory=dict)
    artifact_paths: dict[str, str] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)
    response_families: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_pipeline_region_contract(
    *,
    task_name: str,
    workspace_source: Path,
    target_path: str,
    editable_paths: list[str],
    reference_paths: list[str],
    evaluate_command: list[str] | str,
    metrics_path: str,
    score_metric: str,
    score_direction: str,
    objective_signals: list[str],
    region_graph: dict[str, Any],
    bottleneck_card: dict[str, Any],
    observation: dict[str, Any],
    hardware_context: dict[str, Any],
    artifact_path: str = "",
) -> BottleneckRegionContract:
    focus = _focus_region(region_graph)
    scope = str(focus.get("scope") or bottleneck_card.get("scope") or "pipeline")
    label = str(focus.get("label") or bottleneck_card.get("target") or target_path)
    signals = [str(item) for item in focus.get("signals", []) or bottleneck_card.get("symptoms", [])]
    diagnoses = [str(item) for item in bottleneck_card.get("diagnoses", [])]
    response_families = [str(item) for item in bottleneck_card.get("response_families", [])]
    contract = BottleneckRegionContract(
        schema_version="bottleneck-region/v1",
        id=str(focus.get("id") or region_graph.get("focus_region_id") or "pipeline-critical-path"),
        label=label,
        scope=scope,
        summary=str(focus.get("summary") or bottleneck_card.get("rationale") or ""),
        inputs=[
            {
                "name": "workspace",
                "kind": "workspace",
                "path": str(workspace_source),
            },
            {
                "name": "candidate_target",
                "kind": "file",
                "path": target_path,
            },
            {
                "name": "baseline_evidence",
                "kind": "metrics",
                "source": str(observation.get("source", "unknown")),
                "metrics": observation,
            },
        ],
        outputs=[
            {
                "name": "metrics",
                "kind": "json",
                "path": metrics_path,
                "signals": list(objective_signals),
            },
            {
                "name": "semantic_output",
                "kind": "metric",
                "key": "output_value",
            },
        ],
        reference={
            "workspace_source": str(workspace_source),
            "paths": list(reference_paths),
            "evaluate_command": evaluate_command,
            "metrics_path": metrics_path,
        },
        candidate_surface={
            "workspace_env": "PIPELINE_CANDIDATE_WORKSPACE",
            "target_path": target_path,
            "editable_paths": list(editable_paths or [target_path]),
            "modification_scope": scope,
        },
        evaluation={
            "entrypoint": evaluate_command,
            "metrics_path": metrics_path,
            "score_metric": score_metric,
            "score_direction": score_direction,
            "objective_signals": list(objective_signals),
        },
        profile_evidence={
            "source": observation.get("source", "baseline_evaluation"),
            "task": task_name,
            "hardware": hardware_context,
            "signals": signals,
            "observation": observation,
            "diagnoses": diagnoses,
        },
        artifact_paths={"region": artifact_path} if artifact_path else {},
        signals=signals,
        diagnoses=diagnoses,
        response_families=response_families,
    )
    return contract


def region_work_unit(region: BottleneckRegionContract | dict[str, Any], *, family: str, artifact_path: str = "") -> dict[str, Any]:
    payload = region.to_dict() if isinstance(region, BottleneckRegionContract) else dict(region)
    if artifact_path:
        payload["artifact_paths"] = {**payload.get("artifact_paths", {}), "region": artifact_path}
    return {
        "type": "bottleneck_region",
        "region_id": str(payload.get("id", "")),
        "scope": str(payload.get("scope", "")),
        "family": family,
        "region": payload,
        "artifact_path": artifact_path or payload.get("artifact_paths", {}).get("region", ""),
    }


def _focus_region(region_graph: dict[str, Any]) -> dict[str, Any]:
    regions = region_graph.get("regions")
    if not isinstance(regions, list) or not regions:
        return {}
    focus_id = str(region_graph.get("focus_region_id", ""))
    for item in regions:
        if isinstance(item, dict) and str(item.get("id")) == focus_id:
            return item
    first = regions[0]
    return first if isinstance(first, dict) else {}
