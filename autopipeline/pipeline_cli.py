"""Parse pipeline commands and present the workflow results."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys

from autopipeline.pipeline.spec import PipelineSpec
from autopipeline.pipeline.task import setup_pipeline_workspace
from autopipeline.search.roles import build_role_configs_from_args
from autopipeline.search.types import SearchConfig

from .pipeline_help import WORKLOAD_HELP, build_parser
from .presentation import command, evaluate_command, next_steps, print_outcome
from .project import initialize
from .report import write_product_view
from .workflow import export_run, readiness, run_workload
from .workloads import EXAMPLES


def _search_config(args) -> SearchConfig:
    run = args.command == "run"
    return SearchConfig(
        require_valid_baseline=True,
        max_rounds=args.max_rounds if run else 0, beam_width=args.beam_width if run else 1,
        children_per_expansion=args.children_per_expansion if run else 1,
        role_configs=build_role_configs_from_args(args), enable_final_verify=True,
        planner_command=getattr(args, "planner_command", None),
        executor_command=getattr(args, "executor_command", None),
        reflector_command=getattr(args, "reflector_command", None),
        planner_handoff_mode=getattr(args, "planner_handoff_mode", "variant"),
        command_timeout_seconds=getattr(args, "command_timeout_seconds", 600),
        max_agent_calls=args.max_agent_calls if run else 0,
        max_probe_calls=args.max_probe_calls if run else 0,
    )


def _run(args) -> int:
    spec = PipelineSpec.load(Path(args.spec))
    optimize = args.command == "run"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = Path(args.run_dir or spec.path.parent / "runs" / f"{stamp}-{args.command}").resolve()
    started = False

    def announce(directory: Path, check: dict) -> None:
        nonlocal started
        started = True
        print(f"运行目录：{run_dir}\n环境：{check['runtime']}", flush=True)
        print(f"候选可编辑文件：{', '.join(spec.editable_paths) or spec.target_path}；参考和评测配置保持固定。", flush=True)
        print("即将执行本地参考与候选代码；工作区复制不是操作系统安全沙箱。", flush=True)
        if optimize:
            print(f"Agent：{check['executor']}；可能联系已配置服务并消耗额度。\n"
                  f"树搜索：最多 {args.max_rounds} 轮，每个父分支尝试 {args.children_per_expansion} 个候选，"
                  f"每轮保留最多 {args.beam_width} 个。\n"
                  f"预算：最多 {args.max_agent_calls} 次 agent 调用、{args.max_probe_calls} 次主动探测；"
                  f"每次 agent 命令超时 {args.command_timeout_seconds:g} 秒。调用次数不是金额上限。\n"
                  "先评估基线，基线无效不会开始 agent 调用；优化不保证提速。", flush=True)
        else:
            print("本次不调用 agent；执行数值检查、计时与 profiler，再独立复验。", flush=True)
        print("查看当前记录：" + command("autopipeline", "report", directory), flush=True)

    try:
        summary = run_workload(
            spec, _search_config(args), run_dir=run_dir, optimize=optimize,
            initial_candidate=Path(args.candidate) if args.candidate else None,
            run_name=args.run_name, on_start=announce,
        )
    except (Exception, KeyboardInterrupt):
        if started:
            print("查看失败记录：" + command("autopipeline", "report", run_dir), file=sys.stderr)
        raise

    try:
        write_product_view(run_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        # Report rendering is not part of the evaluator's verdict.
        print(f"报告生成失败：{exc}\n验证记录已保留；重试：" + command("autopipeline", "report", run_dir), file=sys.stderr)
    print_outcome(summary)
    print(f"Status: {summary['status']}\nResult: {run_dir / 'result.json'}")
    if (run_dir / 'view' / 'summary.md').is_file():
        print(f"Report: {run_dir / 'view' / 'summary.md'}")
    if (run_dir / 'view' / 'dashboard.html').is_file():
        print(f"Dashboard: {run_dir / 'view' / 'dashboard.html'}")
    if summary["candidate"]:
        print(f"Candidate: {summary['candidate']}")
    print("\n".join(next_steps(run_dir, summary)))
    return 0 if summary["status"] != "failed" else 1


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
            report = readiness(PipelineSpec.load(Path(args.spec)), _search_config(args), no_runtime=args.no_runtime)
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
            return _run(args)
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
            from autopipeline.pipeline.smoke import run_pipeline_smoke
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
