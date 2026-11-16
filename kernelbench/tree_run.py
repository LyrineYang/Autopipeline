#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from kernelbench.bridge import (
    KernelBenchProblem,
    get_problem,
    load_from_file,
    load_from_huggingface,
    load_from_local_repo,
)
from kernelbench.task import (
    KernelBenchTask,
    default_kernelbench_tree_run_dir,
)
from autopipeline.search.agent import CommandAgentAdapter
from autopipeline.search.roles import add_role_config_args, build_role_configs_from_args
from autopipeline.search.engine import TreeSearchEngine
from autopipeline.search.types import SearchConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run tree search for one KernelBench problem or active KernelBench workspace.",
    )
    parser.add_argument("--level", type=int, default=None, help="KernelBench level.")
    parser.add_argument("--problem", type=int, default=None, help="KernelBench problem id.")
    parser.add_argument(
        "--source",
        choices=["cache", "hf", "local", "file", "active"],
        default="cache",
        help="Problem source. 'active' uses the current kernel.py and workspace/kb_active.",
    )
    parser.add_argument("--repo-path", default=None, help="Local KernelBench repo for --source local.")
    parser.add_argument("--file-path", default=None, help="Single problem file for --source file.")
    parser.add_argument("--backend", choices=["cuda", "triton"], default="cuda")
    parser.add_argument("--kernel-file", default=None, help="Active-mode kernel.py path.")
    parser.add_argument("--active-dir", default=None, help="Active-mode workspace/kb_active path.")
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--beam-width", type=int, default=1)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--children-per-expansion", type=int, default=1)
    parser.add_argument(
        "--bench-arg",
        action="append",
        default=[],
        help="Extra argument passed to kernelbench/bench_kb.py. Repeat for multiple args.",
    )
    parser.add_argument("--planner-command", default=None)
    parser.add_argument(
        "--planner-handoff-mode",
        choices=["idea", "variant", "detailed"],
        default="variant",
        help="How much implementation detail the planner may hand to the executor.",
    )
    parser.add_argument("--executor-command", default=None)
    parser.add_argument("--reflector-command", default=None)
    parser.add_argument("--command-timeout-seconds", type=float, default=600)
    parser.add_argument("--evaluation-timeout-seconds", type=float, default=300)
    parser.add_argument("--max-agent-calls", type=int, default=None)
    parser.add_argument("--max-probe-calls", type=int, default=None)
    parser.add_argument("--max-parallel-branches", type=int, default=1)
    parser.add_argument("--branch-resource-policy", default="conservative")
    add_role_config_args(parser)
    parser.add_argument("--final-verify", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    task = _build_task(args)
    run_name = args.run_name or task.run_name
    run_dir = (
        Path(args.run_dir).resolve()
        if args.run_dir
        else default_kernelbench_tree_run_dir(PROJECT_DIR, run_name)
    )
    role_configs = build_role_configs_from_args(args)
    agent = CommandAgentAdapter(
        PROJECT_DIR,
        executor_command=args.executor_command,
        reflector_command=args.reflector_command,
        command_timeout_seconds=args.command_timeout_seconds,
        role_configs=role_configs,
    )
    engine = TreeSearchEngine(
        task=task,
        agent=agent,
        config=SearchConfig(
            beam_width=args.beam_width,
            max_rounds=args.max_rounds,
            children_per_expansion=args.children_per_expansion,
            max_parallel_branches=args.max_parallel_branches,
            branch_resource_policy=args.branch_resource_policy,
            role_configs=role_configs,
            planner_command=args.planner_command,
            planner_handoff_mode=args.planner_handoff_mode,
            executor_command=args.executor_command,
            reflector_command=args.reflector_command,
            enable_final_verify=args.final_verify,
            command_timeout_seconds=args.command_timeout_seconds,
            max_agent_calls=args.max_agent_calls,
            max_probe_calls=args.max_probe_calls,
        ),
        run_dir=run_dir,
    )
    result = engine.run()
    print(f"run_dir: {result.run_dir}")
    print(f"best_node_id: {result.best_node_id}")
    print(f"promoted_path: {result.promoted_path}")
    print(f"frontier_node_ids: {json.dumps(result.frontier_node_ids)}")
    print(f"total_nodes: {result.total_nodes}")
    if result.final_verify is not None:
        print(f"final_verify: {result.final_verify}")


def _build_task(args: argparse.Namespace) -> KernelBenchTask:
    bench_args = list(args.bench_arg)
    if args.source == "active":
        return KernelBenchTask.from_active_workspace(
            repo_root=PROJECT_DIR,
            kernel_file=Path(args.kernel_file).resolve() if args.kernel_file else None,
            active_dir=Path(args.active_dir).resolve() if args.active_dir else None,
            bench_args=bench_args,
            run_name=args.run_name,
            timeout_seconds=args.evaluation_timeout_seconds,
        )

    problem = _load_problem(args)
    return KernelBenchTask.from_problem(
        repo_root=PROJECT_DIR,
        problem=problem,
        backend=args.backend,
        bench_args=bench_args,
        run_name=args.run_name,
        timeout_seconds=args.evaluation_timeout_seconds,
    )


def _load_problem(args: argparse.Namespace) -> KernelBenchProblem:
    if args.source == "file":
        if not args.file_path:
            raise SystemExit("--file-path is required with --source file")
        return load_from_file(
            args.file_path,
            level=args.level or 1,
            problem_id=args.problem or 0,
        )

    if args.level is None or args.problem is None:
        raise SystemExit("--level and --problem are required unless --source active or --source file")

    problem = get_problem(args.level, args.problem)
    if problem is not None or args.source == "cache":
        if problem is None:
            raise SystemExit(
                f"Problem L{args.level}_P{args.problem:03d} is not cached. "
                "Use --source hf, --source local, or kernelbench/bridge.py fetch first."
            )
        return problem

    if args.source == "hf":
        problems = load_from_huggingface(level=args.level, problem_id=args.problem)
    elif args.source == "local":
        if not args.repo_path:
            raise SystemExit("--repo-path is required with --source local")
        problems = load_from_local_repo(args.repo_path, level=args.level, problem_id=args.problem)
    else:
        raise SystemExit(f"Unsupported source: {args.source}")
    if not problems:
        raise SystemExit(f"Problem L{args.level}_P{args.problem:03d} was not found.")
    return problems[0]


if __name__ == "__main__":
    main()
