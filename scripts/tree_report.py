#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from tree.viewer import build_run_view, write_run_view


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a human-readable dashboard from a tree optimization run.",
    )
    parser.add_argument("run_dir", help="Tree run directory containing search_state.json.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for generated view files. Defaults to RUN_DIR/view.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_dir = Path(args.run_dir).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    paths = write_run_view(run_dir, output_dir=output_dir)
    view = build_run_view(run_dir)
    summary = view["run_summary"]
    print(summary["title"])
    print(
        f"{summary['counts']['实验次数']} 次实验 | "
        f"{summary['counts']['变更被采纳']} 次变更被采纳 | "
        f"{summary['counts']['变更被回退']} 次变更被回退 | "
        f"{summary['counts']['失败次数']} 次失败"
    )
    print(f"dashboard: {paths.dashboard}")
    print(f"iteration_effect: {paths.iteration_effect}")
    print(f"summary: {paths.summary_markdown}")


if __name__ == "__main__":
    main()
