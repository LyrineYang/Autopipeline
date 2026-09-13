"""Validate user input before copying workspaces or starting paid agents."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any


def _relative(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must stay inside the workspace: {value}")
    return path


def validate_spec_data(data: Any, spec_path: Path) -> None:
    if not isinstance(data, dict):
        raise ValueError("Pipeline spec must be a JSON object")
    if not data.get("workspace_source"):
        raise ValueError("workspace_source is required")
    workspace = (spec_path.parent / str(data["workspace_source"])).resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace_source is not a directory: {workspace}")
    target = _relative(data.get("target_path", data.get("target", ".")), "target_path")
    if not (workspace / target).exists():
        raise ValueError(f"target_path does not exist: {target}")
    for field in ("editable_paths", "reference_paths", "constraints", "objective_signals"):
        if not isinstance(data.get(field, []), list) or any(not isinstance(v, str) for v in data.get(field, [])):
            raise ValueError(f"{field} must be a list of strings")
    editable = [_relative(p, "editable_paths") for p in data.get("editable_paths", [])] or [target]
    if not any(target == p or p in target.parents for p in editable):
        raise ValueError("target_path must be covered by editable_paths")
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Workspace symlinks are unsupported; copy the file instead: {path}")
    metrics = _relative(data.get("metrics_path", "metrics.json"), "metrics_path")
    if metrics == Path(".") or (workspace / metrics).is_dir():
        raise ValueError("metrics_path must name a file")
    if metrics == target or str(metrics) in data.get("reference_paths", []) or metrics.suffix in {".py", ".sh", ".so"}:
        raise ValueError("metrics_path must not overwrite candidate, reference, or executable source files")
    for field in ("evaluate_command", "final_verify_command"):
        value = data.get(field)
        if field == "final_verify_command" and value is None:
            continue
        if not ((isinstance(value, str) and value.strip()) or
                (isinstance(value, list) and value and all(isinstance(v, str) and v for v in value))):
            raise ValueError(f"{field} must be a non-empty command string or argument list")
    if data.get("score_direction", "higher") not in {"higher", "lower"}:
        raise ValueError("score_direction must be higher or lower")
    if not isinstance(data.get("score_metric", "throughput"), str) or not data.get("score_metric", "throughput"):
        raise ValueError("score_metric must be a non-empty string")
    timeout = data.get("timeout_seconds", 300)
    if timeout is not None and (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout_seconds must be finite and positive, or null")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    tolerances = metadata.get("numerical_tolerances", {})
    if not isinstance(tolerances, dict) or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in tolerances.values()):
        raise ValueError("numerical_tolerances must contain finite non-negative numbers")


def changed_protected_files(spec: Any, candidate: Path) -> list[str]:
    editable = [Path(p) for p in spec.editable_paths] or [Path(spec.target_path)]
    violations = []
    for source in spec.workspace_source.rglob("*"):
        relative = source.relative_to(spec.workspace_source)
        if not source.is_file() or "__pycache__" in relative.parts or str(relative) == spec.metrics_path:
            continue
        if any(relative == p or p in relative.parents for p in editable):
            continue
        other = candidate / relative
        if other.is_symlink() or not other.is_file() or source.read_bytes() != other.read_bytes():
            violations.append(str(relative))
    violations.extend(str(p.relative_to(candidate)) for p in candidate.rglob("*") if p.is_symlink())
    for file in candidate.rglob("*.py"):
        relative = file.relative_to(candidate)
        if not (spec.workspace_source / relative).exists() and not any(relative == p or p in relative.parents for p in editable):
            violations.append(str(relative))
    return sorted(set(violations))
