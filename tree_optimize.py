#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from autopipeline.search.agent import CommandAgentAdapter
from kernel_tools.task import AutoKernelTask
from autopipeline.search.roles import add_role_config_args, build_role_configs_from_args
from autopipeline.search.engine import TreeSearchEngine
from autopipeline.search.types import SearchConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run beam-style tree search for a single AutoKernel candidate.",
    )
    parser.add_argument("--kernel-file", required=True, help="Source kernel file to optimize.")
    parser.add_argument("--run-name", default=None, help="Human-readable run name.")
    parser.add_argument("--run-dir", default=None, help="Output run directory.")
    parser.add_argument("--beam-width", type=int, default=2, help="Upper bound for frontier width.")
    parser.add_argument("--max-rounds", type=int, default=1, help="Upper bound for search rounds.")
    parser.add_argument(
        "--children-per-expansion",
        type=int,
        default=2,
        help="Upper bound for children proposed from one parent node.",
    )
    parser.add_argument(
        "--bench-arg",
        action="append",
        default=[],
        help="Extra argument to pass through to bench.py. Repeat to add multiple arguments.",
    )
    parser.add_argument("--verify-command", default=None)
    parser.add_argument("--planner-command", default=None)
    parser.add_argument(
        "--planner-handoff-mode",
        choices=["idea", "variant", "detailed"],
        default="variant",
        help="How much implementation detail the planner may hand to the executor.",
    )
    parser.add_argument("--executor-command", default=None)
    parser.add_argument("--reflector-command", default=None)
    parser.add_argument(
        "--command-timeout-seconds",
        type=float,
        default=600,
        help="Timeout for planner, executor, and reflector commands.",
    )
    parser.add_argument("--max-agent-calls", type=int, default=None)
    parser.add_argument("--max-probe-calls", type=int, default=None)
    parser.add_argument("--max-parallel-branches", type=int, default=1)
    parser.add_argument("--branch-resource-policy", default="conservative")
    add_role_config_args(parser)
    return parser


def default_run_dir(repo_root: Path, kernel_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return repo_root / "workspace" / "tree_runs" / f"{stamp}-{kernel_path.stem}"


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    from autopipeline.search.agent_backend import role_uses_app_server
    from autopipeline.search.roles import role_config_for
    roles = build_role_configs_from_args(args)
    if not args.executor_command and not role_uses_app_server(role_config_for(roles, "executor"), args.executor_command):
        parser.error("An executor is required: --executor-backend codex-app-server or --executor-command")

    repo_root = Path(__file__).resolve().parent
    kernel_file = Path(args.kernel_file).resolve()
    run_name = args.run_name or kernel_file.stem
    run_dir = Path(args.run_dir).resolve() if args.run_dir else default_run_dir(repo_root, kernel_file)
    if run_dir.exists():
        parser.error(f"Run directory already exists: {run_dir}; choose a new directory")

    task = AutoKernelTask(
        repo_root=repo_root,
        kernel_file=kernel_file,
        bench_args=list(args.bench_arg),
        verify_command=args.verify_command,
        run_name=run_name,
    )
    role_configs = build_role_configs_from_args(args)
    agent = CommandAgentAdapter(
        repo_root=repo_root,
        executor_command=args.executor_command,
        reflector_command=args.reflector_command,
        command_timeout_seconds=args.command_timeout_seconds,
        role_configs=role_configs,
    )
    config = SearchConfig(
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
        enable_final_verify=bool(args.verify_command),
        command_timeout_seconds=args.command_timeout_seconds,
        max_agent_calls=args.max_agent_calls,
        max_probe_calls=args.max_probe_calls,
    )

    engine = TreeSearchEngine(
        task=task,
        agent=agent,
        config=config,
        run_dir=run_dir,
    )
    result = engine.run()

    print(f"run_dir: {result.run_dir}")
    print(f"best_node_id: {result.best_node_id}")
    print(f"promoted_path: {result.promoted_path}")
    print(f"total_nodes: {result.total_nodes}")
    if result.final_verify is not None:
        print(f"final_verify: {result.final_verify}")


if __name__ == "__main__":
    main()
