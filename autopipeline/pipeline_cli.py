from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import platform
from pathlib import Path
import shutil
import sys

from pipeline.task import PipelineSpec, PipelineTask, run_pipeline_smoke, setup_pipeline_workspace
from tree.agent import CommandAgentAdapter
from tree.agent_backend import role_uses_app_server
from tree.roles import add_role_config_args, build_role_configs_from_args, role_config_for
from tree.search import TreeSearchEngine
from tree.types import SearchConfig
from tree.viewer import write_run_view
from .project import copy_workspace, initialize, write_json, validate_model_directory, model_asset_hashes
from .workloads import EXAMPLES


def build_parser():
    parser = argparse.ArgumentParser(prog="autopipeline pipeline", description="Create, validate, optimize, and export real inference workloads.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("examples", help="List included workloads and their resource requirements")
    init = commands.add_parser("init", help="Create a PyTorch workload project; no GPU or API call")
    init.add_argument("directory")
    init.add_argument("--source", help="Copy your directory containing reference.py and optional candidate.py")
    init.add_argument("--example", choices=list(EXAMPLES), default="residual-mlp")
    init.add_argument("--model-dir", help="Existing local encoder weights for --example hf-local; never downloads")
    init.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    init.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], default="float32")
    for name in ("check", "evaluate", "run"):
        p = commands.add_parser(name, help={"check": "Check configuration and resource readiness without invoking an agent",
                                           "evaluate": "Measure and verify a workload without an agent",
                                           "run": "Optimize a workload with an explicitly configured agent"}[name])
        p.add_argument("--spec", required=True)
        if name == "check":
            p.add_argument("--no-runtime", action="store_true", help="Validate files/config only; does not assert runtime readiness")
            p.add_argument("--json", action="store_true")
        else:
            p.add_argument("--run-dir", help="New output directory; existing directories are never overwritten")
            p.add_argument("--run-name")
            p.add_argument("--candidate", help="Evaluate/start from this candidate workspace against the fixed reference")
        if name in {"check", "run"}:
            p.add_argument("--executor-command")
            add_role_config_args(p)
        if name == "run":
            p.add_argument("--planner-command")
            p.add_argument("--reflector-command")
            p.add_argument("--planner-handoff-mode", choices=["idea", "variant", "detailed"], default="variant")
            p.add_argument("--beam-width", type=int, default=1)
            p.add_argument("--max-rounds", type=int, default=3)
            p.add_argument("--children-per-expansion", type=int, default=2)
            p.add_argument("--max-agent-calls", type=int, default=12)
            p.add_argument("--max-probe-calls", type=int, default=12)
            p.add_argument("--command-timeout-seconds", type=float, default=600)
            p.add_argument("--max-parallel-branches", type=int, choices=[1], default=1, help="Serial measurements on a single device")
            p.add_argument("--branch-resource-policy", choices=["conservative"], default="conservative")
    export = commands.add_parser("export", help="Export a verified candidate, fixed reference, and validation receipts")
    export.add_argument("run_dir")
    export.add_argument("--output-dir", required=True)
    info = commands.add_parser("info", help="Show a validated pipeline spec")
    info.add_argument("--spec", required=True)
    setup = commands.add_parser("setup", help="Copy a spec workspace to a new directory")
    setup.add_argument("--spec", required=True)
    setup.add_argument("--output-dir", required=True)
    smoke = commands.add_parser("smoke", help="Synthetic harness fixture only; not a model or performance experiment")
    smoke.add_argument("--run-dir", required=True)
    return parser


def readiness(spec: PipelineSpec, *, args, require_executor=False) -> dict:
    errors, notes = [], []
    runtime = "not_checked" if getattr(args, "no_runtime", False) else "custom_evaluator"
    if (spec.metadata or {}).get("runtime") == "pytorch":
        from .evaluate import validate_config
        config = json.loads((spec.workspace_source / "workload.json").read_text())
        validate_config(config)
        if spec.metadata.get("example") == "hf-local":
            model = json.loads((spec.workspace_source / "model.json").read_text())
            if not isinstance(model, dict) or not isinstance(model.get("model_dir"), str):
                raise ValueError("model.json must specify a local model_dir")
            validate_model_directory(Path(model["model_dir"]))
            if model_asset_hashes(Path(model["model_dir"])) != model.get("asset_hashes"):
                errors.append("Local model assets changed since initialization. Restore the recorded weights or initialize a new workload.")
            for key in ("batch_size", "sequence_length"):
                if type(model.get(key)) is not int or model[key] < 1:
                    raise ValueError(f"model.json {key} must be a positive integer")
            if not getattr(args, "no_runtime", False) and importlib.util.find_spec("transformers") is None:
                errors.append("Transformers is missing. Install the models extra: python -m pip install '.[cpu,models]' from the checkout.")
            notes.append("Local pretrained weights are external read-only assets; preserve this model version when rechecking exports.")
        tolerances = spec.metadata.get("numerical_tolerances", {})
        if any(config[key] != tolerances.get(key) for key in ("atol", "rtol")):
            errors.append("workload.json and pipeline.json numerical tolerances must match")
        for file in ("reference.py", "candidate.py"):
            if not (spec.workspace_source / file).is_file():
                errors.append(f"Missing workload file: {file}")
        if not getattr(args, "no_runtime", False):
            if importlib.util.find_spec("torch") is None:
                errors.append("PyTorch is missing. Install auto-pipeline[cpu] or auto-pipeline[gpu] in this environment.")
                runtime = "unavailable"
            else:
                import torch
                runtime = f"torch {torch.__version__}; requested device: {config['device']}"
                if config["device"] == "cuda" and not torch.cuda.is_available():
                    errors.append("CUDA is unavailable; use an NVIDIA server with a matching PyTorch build. No CPU fallback is applied.")
        notes.append("Profiler runs with each evaluation; CPU evidence does not establish CUDA performance.")
    roles = build_role_configs_from_args(args)
    executor = role_config_for(roles, "executor")
    command = getattr(args, "executor_command", None)
    app_server = role_uses_app_server(executor, command)
    if require_executor and not command and not app_server:
        errors.append("No executor configured. Use --executor-backend codex-app-server or --executor-command. For evaluation only, use pipeline evaluate.")
    if app_server and shutil.which("codex") is None and not command:
        errors.append("codex is not on PATH. Install and authenticate Codex on the machine running this command.")
    if app_server:
        notes.append("Codex authentication is required on this machine; check does not contact the model service.")
    if require_executor:
        for key in ("beam_width", "max_rounds", "children_per_expansion", "max_agent_calls", "max_probe_calls", "command_timeout_seconds"):
            value = getattr(args, key)
            if not math.isfinite(value) or value <= 0:
                errors.append(f"--{key.replace('_', '-')} must be positive and finite")
    return {"ok": not errors, "configuration": "valid", "runtime": runtime,
            "executor": "codex-app-server" if app_server else "command" if command else "not_configured",
            "errors": errors, "notes": notes}


def _hash_workspace(directory: Path) -> dict:
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("Workspace contains symlinks; copy files and revalidate")
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
            and ".autopipeline" not in p.parts and p.name != "metrics.json"}


def _editable_hashes(directory: Path, spec: PipelineSpec) -> dict:
    editable = [Path(p) for p in spec.editable_paths] or [Path(spec.target_path)]
    return {key: digest for key, digest in _hash_workspace(directory).items()
            if key not in {"bottleneck_region.json", spec.metrics_path} and any(Path(key) == p or p in Path(key).parents for p in editable)}


def run_workload(args) -> int:
    spec = PipelineSpec.load(Path(args.spec))
    check = readiness(spec, args=args, require_executor=args.command == "run")
    if not check["ok"]:
        raise ValueError("\n".join(check["errors"]))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = Path(args.run_dir or spec.path.parent / "runs" / f"{stamp}-{args.command}").resolve()
    if run_dir == spec.workspace_source or spec.workspace_source in run_dir.parents:
        raise ValueError("Run directory must be outside workspace_source")
    initial = Path(args.candidate).resolve() if args.candidate else None
    if initial and (not initial.is_dir() or initial == run_dir or initial in run_dir.parents):
        raise ValueError("Candidate must be an existing workspace outside the run directory")
    if initial:
        _hash_workspace(initial)  # Reject symlinks before copytree can follow them.
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    write_json(result_path, {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    try:
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
        task = PipelineTask(repo_root=Path(__file__).resolve().parents[1], spec_path=frozen_spec, run_name=args.run_name)
        task.initial_candidate = initial
        task.measurement_only_baseline = True
        roles = build_role_configs_from_args(args)
        run = args.command == "run"
        config = SearchConfig(
            require_valid_baseline=True,
            max_rounds=args.max_rounds if run else 0, beam_width=args.beam_width if run else 1,
            children_per_expansion=args.children_per_expansion if run else 1,
            role_configs=roles, enable_final_verify=True,
            planner_command=getattr(args, "planner_command", None),
            executor_command=getattr(args, "executor_command", None),
            reflector_command=getattr(args, "reflector_command", None),
            planner_handoff_mode=getattr(args, "planner_handoff_mode", "variant"),
            command_timeout_seconds=getattr(args, "command_timeout_seconds", 600),
            max_agent_calls=args.max_agent_calls if run else 0,
            max_probe_calls=args.max_probe_calls if run else 0,
        )
        agent = CommandAgentAdapter(task.repo_root, executor_command=config.executor_command,
                                    reflector_command=config.reflector_command, role_configs=roles,
                                    command_timeout_seconds=config.command_timeout_seconds)
        engine = TreeSearchEngine(task=task, agent=agent, config=config, run_dir=run_dir)
        print(f"Running {args.command}; artifacts: {run_dir}", flush=True)
        result = engine.run()
        children = [node for node in engine.nodes.values() if node.parent_id is not None]
        successful_edits = [node for node in children if node.execution_result and node.execution_result.status == "completed"]
        verified = bool(result.promoted_path and result.final_verify and result.final_verify.get("accepted"))
        all_agent_failed = bool(run and (not children or not successful_edits))
        status = "verified" if not run else "completed_no_improvement"
        baseline = next(node for node in engine.nodes.values() if node.parent_id is None)
        candidate_changed = bool(result.promoted_path and _editable_hashes(Path(result.promoted_path), task.spec)
                                 != _editable_hashes(Path(baseline.sandbox_path), task.spec))
        if run and candidate_changed and engine.root_memory.best_score > engine.root_memory.baseline_score:
            status = "completed_improved"
        reference_changed = _hash_workspace(frozen) != original_hashes
        if reference_changed:
            verified = False
        if not verified or all_agent_failed:
            status = "failed"
        summary = {"status": status, "run_dir": str(run_dir), "verified": verified,
                   "best_node_id": result.best_node_id, "candidate": result.promoted_path,
                   "candidate_hashes": _hash_workspace(Path(result.promoted_path)) if result.promoted_path else {},
                   "reference_hashes": _hash_workspace(frozen),
                   "attempts": len(children), "finished_at": datetime.now(timezone.utc).isoformat(),
                   "failure_reason": "Frozen reference changed during execution" if reference_changed else
                                      "All executor attempts failed or no attempt could start" if all_agent_failed else
                                      None if verified else "Evaluation or final verification failed; inspect node verification logs",
                   "final_verification": result.final_verify}
        write_json(result_path, summary)
        write_run_view(run_dir)
        print(f"Status: {status}\nResult: {result_path}\nReport: {run_dir / 'view' / 'summary.md'}")
        if result.promoted_path:
            print(f"Candidate: {result.promoted_path}")
        return 0 if status != "failed" else 1
    except BaseException as exc:
        write_json(result_path, {"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                                 "error": f"{type(exc).__name__}: {exc}", "run_dir": str(run_dir)})
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
        raise FileExistsError(f"Destination already exists: {destination}")
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


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "examples":
            for name, description in EXAMPLES.items():
                print(f"{name}: {description}")
        elif args.command == "init":
            spec = initialize(Path(args.directory), source=Path(args.source) if args.source else None, device=args.device, dtype=args.dtype,
                              example=args.example, model_dir=Path(args.model_dir) if args.model_dir else None)
            print(f"Created: {spec}\nNext: autopipeline pipeline check --spec {spec}")
        elif args.command == "check":
            report = readiness(PipelineSpec.load(Path(args.spec)), args=args)
            print(json.dumps(report, indent=2) if args.json else "\n".join(
                [f"Configuration: valid; runtime: {report['runtime']}; executor: {report['executor']}"] + report["errors"] + report["notes"]))
            return 0 if report["ok"] else 1
        elif args.command in {"evaluate", "run"}:
            return run_workload(args)
        elif args.command == "export":
            export_run(Path(args.run_dir).resolve(), Path(args.output_dir).resolve())
            print(f"Exported: {Path(args.output_dir).resolve()}")
        elif args.command == "info":
            print(json.dumps(asdict(PipelineSpec.load(Path(args.spec))), default=str, indent=2))
        elif args.command == "setup":
            print(json.dumps(setup_pipeline_workspace(Path(args.spec), Path(args.output_dir)), indent=2))
        elif args.command == "smoke":
            if Path(args.run_dir).exists():
                raise FileExistsError("Smoke output already exists")
            print("Synthetic harness fixture; no model execution or GPU performance claim.")
            print(run_pipeline_smoke(repo_root=Path.cwd(), run_dir=Path(args.run_dir).resolve()))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Partial run artifacts are retained; start a new run directory to retry.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, (OSError, ValueError)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
