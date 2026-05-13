#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from tree.agent import CommandAgentAdapter
from tree.roles import add_role_config_args, build_role_configs_from_args
from pipeline.task import (
    PipelineSpec,
    PipelineTask,
    default_pipeline_run_dir,
    run_pipeline_smoke,
    setup_pipeline_workspace,
)
from tree.search import TreeSearchEngine
from tree.types import SearchConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generic pipeline bridge -- set up and run pipeline optimization tasks.",
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    info_p = sub.add_parser("info", help="Show a pipeline task spec summary")
    info_p.add_argument("--spec", required=True, help="Path to pipeline task JSON.")

    setup_p = sub.add_parser("setup", help="Copy a pipeline task workspace into an active directory")
    setup_p.add_argument("--spec", required=True, help="Path to pipeline task JSON.")
    setup_p.add_argument(
        "--output-dir",
        default=str(PROJECT_DIR / "workspace" / "pipeline_active"),
        help="Destination workspace directory.",
    )

    run_p = sub.add_parser("run", help="Run tree search for a pipeline task")
    run_p.add_argument("--spec", required=True, help="Path to pipeline task JSON.")
    run_p.add_argument("--run-name", default=None)
    run_p.add_argument("--run-dir", default=None)
    run_p.add_argument("--beam-width", type=int, default=2)
    run_p.add_argument("--max-rounds", type=int, default=1)
    run_p.add_argument("--children-per-expansion", type=int, default=2)
    run_p.add_argument("--planner-command", default=None)
    run_p.add_argument(
        "--planner-handoff-mode",
        choices=["idea", "variant", "detailed"],
        default="variant",
        help="How much implementation detail the planner may hand to the executor.",
    )
    run_p.add_argument("--executor-command", default=None)
    run_p.add_argument("--reflector-command", default=None)
    run_p.add_argument("--command-timeout-seconds", type=float, default=600)
    run_p.add_argument("--max-agent-calls", type=int, default=None)
    run_p.add_argument("--max-probe-calls", type=int, default=None)
    run_p.add_argument("--max-parallel-branches", type=int, default=1)
    run_p.add_argument("--branch-resource-policy", default="conservative")
    add_role_config_args(run_p)

    smoke_p = sub.add_parser("smoke", help="Run the built-in pipeline smoke path")
    smoke_p.add_argument(
        "--run-dir",
        default=None,
        help="Output directory for the generated smoke fixture and tree run.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "info":
        spec = PipelineSpec.load(Path(args.spec))
        print(f"name: {spec.name}")
        print(f"objective: {spec.objective}")
        print(f"workspace_source: {spec.workspace_source}")
        print(f"target_path: {spec.target_path}")
        print(f"editable_paths: {', '.join(spec.editable_paths) or spec.target_path}")
        print(f"objective_signals: {', '.join(spec.objective_signals)}")
        print(f"metrics_path: {spec.metrics_path}")
        return

    if args.command == "setup":
        result = setup_pipeline_workspace(Path(args.spec), Path(args.output_dir))
        print("Pipeline workspace setup complete.")
        print(f"workspace: {result['workspace']}")
        print(f"metadata: {result['metadata']}")
        print(f"target: {result['target']}")
        return

    if args.command == "run":
        spec = PipelineSpec.load(Path(args.spec))
        run_name = args.run_name or spec.name
        run_dir = Path(args.run_dir).resolve() if args.run_dir else default_pipeline_run_dir(PROJECT_DIR, run_name)
        role_configs = build_role_configs_from_args(args)
        task = PipelineTask(repo_root=PROJECT_DIR, spec_path=Path(args.spec), run_name=run_name)
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
                enable_final_verify=bool(spec.final_verify_command),
                command_timeout_seconds=args.command_timeout_seconds,
                max_agent_calls=args.max_agent_calls,
                max_probe_calls=args.max_probe_calls,
            ),
            run_dir=run_dir,
        )
        result = engine.run()
        _print_result(result)
        return

    if args.command == "smoke":
        run_dir = (
            Path(args.run_dir).resolve()
            if args.run_dir
            else default_pipeline_run_dir(PROJECT_DIR, "smoke")
        )
        result = run_pipeline_smoke(repo_root=PROJECT_DIR, run_dir=run_dir)
        _print_result(result)
        return

    parser.print_help()
    print()
    print("Examples:")
    print("  uv run pipeline/bridge.py smoke")
    print("  uv run pipeline/bridge.py setup --spec path/to/pipeline_task.json")
    print("  uv run pipeline/bridge.py run --spec path/to/pipeline_task.json --executor-command '...'")


def _print_result(result) -> None:
    print(f"run_dir: {result.run_dir}")
    print(f"best_node_id: {result.best_node_id}")
    print(f"promoted_path: {result.promoted_path}")
    print(f"frontier_node_ids: {json.dumps(result.frontier_node_ids)}")
    print(f"total_nodes: {result.total_nodes}")
    if result.final_verify is not None:
        print(f"final_verify: {result.final_verify}")


if __name__ == "__main__":
    main()
