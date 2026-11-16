from __future__ import annotations

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

from pipeline.task import PipelineSpec, PipelineTask, setup_pipeline_workspace
from tree.agent import CommandAgentAdapter
from tree.agent_backend import role_uses_app_server
from tree.roles import build_role_configs_from_args, role_config_for
from tree.search import TreeSearchEngine
from tree.types import SearchConfig
from .project import copy_workspace, initialize, write_json, validate_model_directory, model_asset_hashes
from .workloads import EXAMPLES
from .pipeline_help import WORKLOAD_HELP, build_parser
from .presentation import command, evaluate_command, print_outcome, next_steps
from .report import write_product_view


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
                errors.append("Transformers is missing. Reinstall your AutoPipeline wheel with [cpu,models], or run python -m pip install '.[cpu,models]' from its source directory.")
            notes.append("Local pretrained weights are external read-only assets; preserve this model version when rechecking exports.")
        tolerances = spec.metadata.get("numerical_tolerances", {})
        if any(config[key] != tolerances.get(key) for key in ("atol", "rtol")):
            errors.append("workload.json and pipeline.json numerical tolerances must match")
        for file in ("reference.py", "candidate.py"):
            if not (spec.workspace_source / file).is_file():
                errors.append(f"Missing workload file: {file}")
        if not getattr(args, "no_runtime", False):
            if importlib.util.find_spec("torch") is None:
                errors.append("PyTorch is missing. Reinstall your AutoPipeline wheel with [cpu] or [gpu], or run python -m pip install '.[cpu]' from its source directory. Use check --no-runtime for files/config only.")
                runtime = "unavailable"
            else:
                import torch
                runtime = f"torch {torch.__version__}; requested device: {config['device']}"
                if config["device"] == "cuda" and not torch.cuda.is_available():
                    errors.append("CUDA is unavailable; use an NVIDIA server with a matching PyTorch build. No CPU fallback is applied. To try CPU, create a new workload with: autopipeline pipeline init NEW_DIRECTORY --device cpu")
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
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}. Omit --run-dir to create a new timestamped run; existing results are never overwritten.")
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
        print(f"运行目录：{run_dir}\n环境：{check['runtime']}", flush=True)
        print(f"候选可编辑文件：{', '.join(spec.editable_paths) or spec.target_path}；参考和评测配置保持固定。", flush=True)
        print("即将执行本地参考与候选代码；工作区复制不是操作系统安全沙箱。", flush=True)
        if args.command == "run":
            print(f"Agent：{check['executor']}；可能联系已配置服务并消耗额度。\n"
                  f"预算：最多 {args.max_agent_calls} 次 agent 调用、{args.max_probe_calls} 次主动探测；"
                  f"每次 agent 命令超时 {args.command_timeout_seconds:g} 秒。调用次数不是金额上限。\n"
                  "先评估基线，基线无效不会开始 agent 调用；优化不保证提速。", flush=True)
        else:
            print("本次不调用 agent；执行数值检查、计时与 profiler，再独立复验。", flush=True)
        print("查看当前记录：" + command("autopipeline", "report", run_dir), flush=True)
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
        try:
            write_product_view(run_dir)
        except (OSError, ValueError, RuntimeError) as exc:
            # Report rendering is not part of the evaluator's verdict.
            print(f"报告生成失败：{exc}\n验证记录已保留；重试：" + command("autopipeline", "report", run_dir), file=sys.stderr)
        print_outcome(summary)
        print(f"Status: {status}\nResult: {result_path}")
        if (run_dir / 'view' / 'summary.md').is_file():
            print(f"Report: {run_dir / 'view' / 'summary.md'}")
        if (run_dir / 'view' / 'dashboard.html').is_file():
            print(f"Dashboard: {run_dir / 'view' / 'dashboard.html'}")
        if result.promoted_path:
            print(f"Candidate: {result.promoted_path}")
        print("\n".join(next_steps(run_dir, summary)))
        return 0 if status != "failed" else 1
    except BaseException as exc:
        write_json(result_path, {"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                                 "error": f"{type(exc).__name__}: {exc}", "run_dir": str(run_dir)})
        print("查看失败记录：" + command("autopipeline", "report", run_dir), file=sys.stderr)
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


def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list in (["--help"], ["-h"]):
        print(WORKLOAD_HELP)
        return 0
    if args_list == ["--help-all"]:
        build_parser().print_help()
        return 0
    parser = build_parser(show_all="--help-all" in args_list)
    args = parser.parse_args(args_list)
    try:
        if args.command == "examples":
            for name, description in EXAMPLES.items():
                print(f"{name}: {description}")
        elif args.command == "init":
            spec = initialize(Path(args.directory), source=Path(args.source) if args.source else None, device=args.device, dtype=args.dtype,
                              example=args.example, model_dir=Path(args.model_dir) if args.model_dir else None)
            print(f"Created: {spec}\n设备：{args.device}；数值类型：{args.dtype}。未执行模型、下载权重或调用 agent。")
            print("下一步，检查设备和配置：\n" + command("autopipeline", "pipeline", "check", "--spec", spec))
            print(f"使用说明：{spec.parent / 'README.md'}")
        elif args.command == "check":
            report = readiness(PipelineSpec.load(Path(args.spec)), args=args)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(f"检查{'通过' if report['ok'] else '未通过'}；环境：{report['runtime']}")
                for error in report['errors']:
                    print(f"需要处理：{error}")
                print("未执行模型或联系 agent 服务；检查通过不等于评估通过。")
                if report['executor'] != 'not_configured':
                    print(f"Agent：{report['executor']}；未验证登录或额度。")
                if report['ok']:
                    if args.no_runtime:
                        print("下一步，检查实际设备：\n" + command("autopipeline", "pipeline", "check", "--spec", Path(args.spec).resolve()))
                    else:
                        print("下一步，评估模型（不调用 agent）：\n" + evaluate_command(Path(args.spec).resolve()))
            return 0 if report["ok"] else 1
        elif args.command in {"evaluate", "run"}:
            return run_workload(args)
        elif args.command == "export":
            export_run(Path(args.run_dir).resolve(), Path(args.output_dir).resolve())
            destination = Path(args.output_dir).resolve()
            print(f"Exported: {destination}\n导出包含候选、固定参考和验证记录；未部署服务或复制外部权重。")
            print("下一步，在依赖和模型资源就绪后重新评估（不调用 agent）：\n" +
                  evaluate_command(destination / "pipeline.json", destination / "candidate"))
        elif args.command == "info":
            print(json.dumps(asdict(PipelineSpec.load(Path(args.spec))), default=str, indent=2))
        elif args.command == "setup":
            print(json.dumps(setup_pipeline_workspace(Path(args.spec), Path(args.output_dir)), indent=2))
        elif args.command == "smoke":
            from pipeline.smoke import run_pipeline_smoke
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
