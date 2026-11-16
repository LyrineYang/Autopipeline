"""Run a fixed workload through independent verification and optional tree search."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import platform
from pathlib import Path
import shutil
import sys
import time

from autopipeline.pipeline.spec import PipelineSpec
from autopipeline.pipeline.task import PipelineTask
from autopipeline.search.agent import CommandAgentAdapter
from autopipeline.search.agent_backend import role_uses_app_server
from autopipeline.search.roles import role_config_for
from autopipeline.search.run_artifacts import count_candidates, read_events, read_object
from autopipeline.search.engine import TreeSearchEngine
from autopipeline.search.types import SearchConfig

from .project import copy_workspace, model_asset_hashes, validate_model_directory, write_json


def readiness(
    spec: PipelineSpec, config: SearchConfig, *, no_runtime: bool = False, require_executor: bool = False,
) -> dict:
    errors, notes = [], []
    runtime = "not_checked" if no_runtime else "custom_evaluator"
    metadata = spec.metadata or {}
    if spec.is_native:
        from .evaluate import validate_config
        workload = json.loads((spec.workspace_source / "workload.json").read_text())
        validate_config(workload)
        if metadata.get("example") == "hf-local":
            model = json.loads((spec.workspace_source / "model.json").read_text())
            if not isinstance(model, dict) or not isinstance(model.get("model_dir"), str):
                raise ValueError("model.json must specify a local model_dir")
            validate_model_directory(Path(model["model_dir"]))
            if model_asset_hashes(Path(model["model_dir"])) != model.get("asset_hashes"):
                errors.append("Local model assets changed since initialization. Restore the recorded weights or initialize a new workload.")
            for key in ("batch_size", "sequence_length"):
                if type(model.get(key)) is not int or model[key] < 1:
                    raise ValueError(f"model.json {key} must be a positive integer")
            if not no_runtime and importlib.util.find_spec("transformers") is None:
                errors.append("Transformers is missing. Reinstall your AutoPipeline wheel with [cpu,models], or run python -m pip install '.[cpu,models]' from its source directory.")
            notes.append("Local pretrained weights are external read-only assets; preserve this model version when rechecking exports.")
        tolerances = metadata.get("numerical_tolerances", {})
        if any(workload[key] != tolerances.get(key) for key in ("atol", "rtol")):
            errors.append("workload.json and pipeline.json numerical tolerances must match")
        for file in ("reference.py", "candidate.py"):
            if not (spec.workspace_source / file).is_file():
                errors.append(f"Missing workload file: {file}")
        if not no_runtime:
            if importlib.util.find_spec("torch") is None:
                errors.append("PyTorch is missing. Reinstall your AutoPipeline wheel with [cpu] or [gpu], or run python -m pip install '.[cpu]' from its source directory. Use check --no-runtime for files/config only.")
                runtime = "unavailable"
            else:
                import torch
                runtime = f"torch {torch.__version__}; requested device: {workload['device']}"
                if workload["device"] == "cuda" and not torch.cuda.is_available():
                    errors.append("CUDA is unavailable; use an NVIDIA server with a matching PyTorch build. No CPU fallback is applied. To try CPU, create a new workload with: autopipeline pipeline init NEW_DIRECTORY --device cpu")
        notes.append("Profiler runs with each evaluation; CPU evidence does not establish CUDA performance.")
    executor = role_config_for(config.role_configs, "executor")
    command = executor.command or config.executor_command
    app_server = role_uses_app_server(executor, command)
    if require_executor and not command and not app_server:
        errors.append("No executor configured. Use --executor-backend codex-app-server or --executor-command. For evaluation only, use pipeline evaluate.")
    if app_server and shutil.which("codex") is None and not command:
        errors.append("codex is not on PATH. Install and authenticate Codex on the machine running this command.")
    if app_server:
        notes.append("Codex authentication is required on this machine; check does not contact the model service.")
    if require_executor:
        for key in ("beam_width", "max_rounds", "children_per_expansion", "max_agent_calls", "max_probe_calls", "command_timeout_seconds"):
            value = getattr(config, key)
            if value is None or not math.isfinite(value) or value <= 0:
                errors.append(f"--{key.replace('_', '-')} must be positive and finite")
    return {"ok": not errors, "configuration": "valid", "runtime": runtime,
            "executor": "codex-app-server" if app_server else "command" if command else "not_configured",
            "errors": errors, "notes": notes}


def _hash_workspace(directory: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Workspace contains symlinks; copy files and revalidate")
        if path.is_file() and "__pycache__" not in path.parts and ".autopipeline" not in path.parts and path.name != "metrics.json":
            hashes[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _editable_hashes(hashes: dict[str, str], spec: PipelineSpec) -> dict[str, str]:
    editable = [Path(p) for p in spec.editable_paths] or [Path(spec.target_path)]
    return {key: digest for key, digest in hashes.items()
            if key not in {"bottleneck_region.json", spec.metrics_path} and any(Path(key) == p or p in Path(key).parents for p in editable)}


def run_workload(
    spec: PipelineSpec,
    config: SearchConfig,
    *,
    run_dir: Path,
    optimize: bool,
    initial_candidate: Path | None = None,
    run_name: str | None = None,
    on_start: Callable[[Path, dict], None] | None = None,
) -> dict:
    if not optimize:
        config = SearchConfig(beam_width=1, max_rounds=0, children_per_expansion=1, max_agent_calls=0, max_probe_calls=0)
    config = replace(config, require_valid_baseline=True, enable_final_verify=True)
    check = readiness(spec, config, require_executor=optimize)
    if not check["ok"]:
        raise ValueError("\n".join(check["errors"]))
    run_dir = run_dir.resolve()
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}. Omit --run-dir to create a new timestamped run; existing results are never overwritten.")
    if run_dir == spec.workspace_source or spec.workspace_source in run_dir.parents:
        raise ValueError("Run directory must be outside workspace_source")
    initial = Path(initial_candidate).resolve() if initial_candidate else None
    if initial and (not initial.is_dir() or initial == run_dir or initial in run_dir.parents):
        raise ValueError("Candidate must be an existing workspace outside the run directory")
    if initial:
        _hash_workspace(initial)  # Reject symlinks before copytree can follow them.
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    start = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    write_json(result_path, {"status": "running", "started_at": started_at})
    try:
        if on_start is not None:
            on_start(run_dir, check)
        frozen = run_dir / "input" / "workload"
        copy_workspace(spec.workspace_source, frozen)
        data = json.loads(spec.path.read_text())
        data["workspace_source"] = "workload"
        # Public runs always verify the promoted artifact, including custom specs.
        data["final_verify_command"] = data.get("final_verify_command") or data["evaluate_command"]
        frozen_spec = run_dir / "input" / "pipeline.json"
        write_json(frozen_spec, data)
        original_hashes = _hash_workspace(frozen)
        write_json(run_dir / "input" / "hashes.json", original_hashes)
        write_json(run_dir / "environment.json", {
            "python": sys.version, "platform": platform.platform(),
            "packages": {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()
                         if dist.metadata["Name"]},
        })
        task = PipelineTask(
            repo_root=Path(__file__).resolve().parents[1],
            spec_path=frozen_spec,
            run_name=run_name,
            initial_candidate=initial,
            measurement_only_baseline=True,
        )
        agent = CommandAgentAdapter(task.repo_root, executor_command=config.executor_command,
                                    reflector_command=config.reflector_command, role_configs=config.role_configs,
                                    command_timeout_seconds=config.command_timeout_seconds)
        engine = TreeSearchEngine(task=task, agent=agent, config=config, run_dir=run_dir)
        result = engine.run()
        children = [node for node in engine.nodes.values() if node.parent_id is not None]
        verified = bool(result.promoted_path and result.final_verify and result.final_verify.get("accepted"))
        all_agent_failed = optimize and not any(
            node.execution_result and node.execution_result.status == "completed" for node in children
        )
        candidate_hashes = _hash_workspace(Path(result.promoted_path)) if result.promoted_path else {}
        status = "verified" if not optimize else "completed_no_improvement"
        if optimize and verified and engine.root_memory.best_score > engine.root_memory.baseline_score:
            baseline = next(node for node in engine.nodes.values() if node.parent_id is None)
            baseline_hashes = _hash_workspace(Path(baseline.sandbox_path))
            if _editable_hashes(candidate_hashes, task.spec) != _editable_hashes(baseline_hashes, task.spec):
                status = "completed_improved"
        reference_hashes = _hash_workspace(frozen)
        reference_changed = reference_hashes != original_hashes
        if reference_changed:
            verified = False
        if not verified or all_agent_failed:
            status = "failed"
        failure_reason = None
        if reference_changed:
            failure_reason = "Frozen reference changed during execution"
        elif all_agent_failed:
            failure_reason = "All executor attempts failed or no attempt could start"
        elif not verified:
            failure_reason = "Evaluation or final verification failed; inspect node verification logs"

        summary = {
            "schema_version": "run-receipt/v2",
            "status": status,
            "run_dir": str(run_dir),
            "verified": verified,
            "best_node_id": result.best_node_id,
            "candidate": result.promoted_path,
            "candidate_hashes": candidate_hashes,
            "reference_hashes": reference_hashes,
            "attempts": len(children),  # Legacy field counts proposed nodes.
            "search_counts": count_candidates(read_object(engine.state_path), read_events(engine.events_path)),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.perf_counter() - start, 6),
            "failure_reason": failure_reason,
            "final_verification": result.final_verify,
        }
        write_json(result_path, summary)
        return summary
    except (Exception, KeyboardInterrupt) as exc:
        write_json(result_path, {
            "schema_version": "run-receipt/v2",
            "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "run_dir": str(run_dir),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.perf_counter() - start, 6),
        })
        raise


def export_run(run_dir: Path, destination: Path) -> None:
    result = json.loads((run_dir / "result.json").read_text())
    if not isinstance(result, dict) or not result.get("verified") or result.get("status") not in {"verified", "completed_improved", "completed_no_improvement"}:
        raise ValueError("Export requires a successfully verified run")
    if not all(isinstance(result.get(key), dict) for key in ("candidate_hashes", "reference_hashes")):
        raise ValueError("Run receipt is missing file hashes; evaluate again before exporting")
    candidate = run_dir / "promoted_pipeline"
    reference = run_dir / "input" / "workload"
    if _hash_workspace(candidate) != result["candidate_hashes"] or _hash_workspace(reference) != result["reference_hashes"]:
        raise ValueError("Run files changed after verification. Evaluate them again before exporting.")
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}. Choose a new --output-dir; existing exports are never overwritten.")
    if run_dir == destination or run_dir in destination.parents:
        raise ValueError("Export destination must be outside the run directory")
    destination.mkdir(parents=True)
    copy_workspace(candidate, destination / "candidate")
    copy_workspace(reference, destination / "reference")
    spec = json.loads((run_dir / "input" / "pipeline.json").read_text())
    spec["workspace_source"] = "reference"
    write_json(destination / "pipeline.json", spec)
    write_json(destination / "receipt.json", result)
    shutil.copytree(run_dir / "final_verification", destination / "verification")
    if (candidate / ".autopipeline").is_dir():
        shutil.copytree(candidate / ".autopipeline", destination / "verification/candidate-profiler")
    shutil.copy2(run_dir / "environment.json", destination / "environment.json")
    (destination / "README.md").write_text(
        "# Verified candidate\n\nUse candidate/ as the implementation workspace.\n"
        "Reinstall the same workload dependencies on the target machine, then revalidate:\n\n"
        "```sh\nautopipeline pipeline evaluate --spec pipeline.json --candidate candidate --run-dir recheck\n```\n\n"
        "Measurements apply only to the recorded workload and machine. Custom evaluator commands may need their external dependencies.\n",
        encoding="utf-8")
