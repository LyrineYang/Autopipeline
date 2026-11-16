"""The workspace and evaluator contract loaded from pipeline.json."""
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .validation import validate_spec_data


@dataclass(slots=True)
class PipelineSpec:
    path: Path
    name: str
    objective: str
    workspace_source: Path
    target_path: str
    editable_paths: list[str]
    reference_paths: list[str]
    constraints: list[str]
    objective_signals: list[str]
    evaluate_command: list[str] | str
    metrics_path: str = "metrics.json"
    score_metric: str = "throughput"
    score_direction: str = "higher"
    final_verify_command: list[str] | str | None = None
    timeout_seconds: float | None = 300
    metadata: dict[str, Any] | None = None

    @property
    def is_native(self) -> bool:
        return (self.metadata or {}).get("runtime") == "pytorch"

    @classmethod
    def load(cls, path: Path) -> "PipelineSpec":
        spec_path = path.resolve()
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        validate_spec_data(data, spec_path)
        base_dir = spec_path.parent
        workspace_source = Path(str(data["workspace_source"]))
        if not workspace_source.is_absolute():
            workspace_source = (base_dir / workspace_source).resolve()
        return cls(
            path=spec_path,
            name=str(data.get("name") or spec_path.stem),
            objective=str(data.get("objective") or "Optimize pipeline objective metrics."),
            workspace_source=workspace_source,
            target_path=data.get("target_path", data.get("target", ".")),
            editable_paths=data.get("editable_paths", []),
            reference_paths=data.get("reference_paths", []),
            constraints=data.get("constraints", []),
            objective_signals=data.get("objective_signals", []),
            evaluate_command=data["evaluate_command"],
            metrics_path=str(data.get("metrics_path", "metrics.json")),
            score_metric=data.get("score_metric", "throughput"),
            score_direction=data.get("score_direction", "higher"),
            final_verify_command=data.get("final_verify_command"),
            timeout_seconds=data.get("timeout_seconds", 300),
            metadata=data.get("metadata", {}),
        )
