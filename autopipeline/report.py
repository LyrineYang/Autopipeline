"""Shared report command for the installed CLI and legacy script."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from tree.viewer import write_run_view


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopipeline report",
        description="Build a human-readable dashboard from an optimization run.",
    )
    parser.add_argument("run_dir", help="Run directory containing search_state.json.")
    parser.add_argument("--output-dir", help="Defaults to RUN_DIR/view.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = Path(args.run_dir).resolve()
        if not (run_dir / "search_state.json").is_file():
            raise ValueError(f"No search_state.json in run directory: {run_dir}")
        paths = write_run_view(
            run_dir,
            output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
        )
        # Use the summary just written instead of rebuilding the whole view.
        summary = json.loads(paths.run_summary.read_text(encoding="utf-8"))
        print(summary["title"])
        print(
            f"{summary['counts']['实验次数']} 次实验 | "
            f"{summary['counts']['变更被采纳']} 次变更被采纳 | "
            f"{summary['counts']['变更被回退']} 次变更被回退 | "
            f"{summary['counts']['失败次数']} 次失败"
        )
        print(f"Dashboard: {paths.dashboard}")
        print(f"Iteration effect: {paths.iteration_effect}")
        print(f"Summary: {paths.summary_markdown}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
