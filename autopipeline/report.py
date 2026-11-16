"""Shared report command for the installed CLI and legacy script."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from autopipeline.search.run_artifacts import format_counts
from autopipeline.search.viewer import write_run_view
from .presentation import load_receipt, outcome, print_outcome, next_steps


def write_product_view(run_dir: Path, output_dir: Path | None = None):
    receipt = load_receipt(run_dir)
    message = outcome(receipt) if receipt is not None else None
    if message is not None:
        message["next_step"] = "\n".join(next_steps(run_dir, receipt))
    return write_run_view(run_dir, output_dir=output_dir, outcome=message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopipeline report",
        description="Explain an evaluation or optimization result and build its report; no agent call.",
        epilog="Failed or interrupted runs can also be inspected. Reporting success does not mean the workload passed verification.",
    )
    parser.add_argument("run_dir", help="Run directory printed by evaluate/run, not the workload project directory.")
    parser.add_argument("--output-dir", help="Defaults to RUN_DIR/view.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = Path(args.run_dir).resolve()
        receipt = load_receipt(run_dir)
        if not (run_dir / "search_state.json").is_file() and receipt is None:
            raise ValueError(f"No run record (result.json or search_state.json) in: {run_dir}. "
                             "Use the run directory printed by evaluate/run, not the project or export directory.")
        paths = write_product_view(
            run_dir,
            output_dir=Path(args.output_dir).resolve() if args.output_dir else None,
        )
        # Use the summary just written instead of rebuilding the whole view.
        summary = json.loads(paths.run_summary.read_text(encoding="utf-8"))
        if receipt is not None:
            print_outcome(receipt)
        else:
            print(summary["title"])
        print(format_counts(summary["search_counts"]))
        print(f"Dashboard: {paths.dashboard}")
        print(f"Summary: {paths.summary_markdown}")
        if receipt is not None:
            print("\n".join(next_steps(run_dir, receipt)))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
