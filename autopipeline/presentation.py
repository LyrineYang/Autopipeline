"""User-facing workflow messages; never decides whether a candidate is valid."""
from __future__ import annotations

import json
from pathlib import Path
import shlex


def command(*parts: object) -> str:
    """Copyable POSIX-shell command, including paths with spaces."""
    return shlex.join([str(part) for part in parts])


def evaluate_command(spec: Path, candidate: Path | None = None) -> str:
    parts = ["autopipeline", "pipeline", "evaluate", "--spec", str(spec)]
    if candidate is not None:
        parts += ["--candidate", str(candidate)]
    return command(*parts)


def outcome(receipt: dict) -> dict[str, str]:
    status = receipt.get("status")
    labels = {
        "verified": ("Evaluation passed", "The selected implementation passed final verification."),
        "completed_improved": ("Improved candidate verified", "Search selected changed code with a better score. Repeat the measurement to confirm the gain."),
        "completed_no_improvement": ("Search complete with no improvement", "The selected implementation passed final verification."),
        "failed": ("Run failed", "Inspect the failure and the evaluator logs."),
        "interrupted": ("Run interrupted", "Partial records are available."),
        "running": ("Run incomplete", "The receipt is awaiting a final result."),
    }
    title, detail = labels.get(status, ("Outcome unconfirmed", "Inspect the run receipt."))
    if status in {"verified", "completed_improved", "completed_no_improvement"} and receipt.get("verified") is not True:
        title, detail = "Outcome unconfirmed", "Candidate export requires a successful final verification."
    return {"title": title, "detail": detail,
            "reason": str(receipt.get("failure_reason") or receipt.get("error") or "")}


def print_outcome(receipt: dict) -> None:
    message = outcome(receipt)
    print(f"{message['title']}. {message['detail']}")
    if message["reason"]:
        print(f"Reason: {message['reason']}")


def load_receipt(run_dir: Path) -> dict | None:
    path = run_dir / "result.json"
    if not path.exists():
        return None  # Legacy tree runs do not have a product receipt.
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError(f"Invalid run receipt: {path}. Expected an object.")
    return receipt


def next_steps(run_dir: Path, receipt: dict) -> list[str]:
    if receipt.get("status") == "running":
        return ["Refresh the report to see new records. If the process has stopped, retry with a new run directory.",
                command("autopipeline", "report", run_dir)]
    if receipt.get("verified") is True and receipt.get("status") in {
        "verified", "completed_improved", "completed_no_improvement",
    }:
        destination = run_dir.parent / f"{run_dir.name}-export"
        suffix = 2
        while destination.exists():
            destination = run_dir.parent / f"{run_dir.name}-export-{suffix}"
            suffix += 1
        return ["Export the verified implementation.", command("autopipeline", "pipeline", "export", run_dir,
                                              "--output-dir", destination)]
    lines = [f"Run records: {run_dir}"]
    if receipt.get("status") == "failed":
        for path in [run_dir / "final_verification/verification_report.json",
                     *sorted(run_dir.glob("nodes/*/verification/verification_report.json"))]:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict) or report.get("accepted") is not False:
                continue
            explanations = {
                "candidate_failed": "The candidate failed execution or the numerical check. Inspect its code and dependencies.",
                "reference_failed": "The reference evaluation failed. Check the inputs, device, and dependencies.",
                "performance_regressed": "The candidate failed the performance requirement. Recheck it under the same conditions.",
                "protected_files_changed": "Protected files changed. Restore the reference and evaluator configuration.",
            }
            lines.append(explanations.get(report.get("failure_reason"), "Inspect the evaluation evidence below."))
            lines.append(f"Evaluation evidence: {path}")
            for label in ("reference", "candidate"):
                for stream in ("stderr", "stdout"):
                    log = path.parent / f"{label}.{stream}.log"
                    if log.is_file() and log.stat().st_size:
                        lines.append(f"{label.title()} diagnostics: {log}")
            break
    if (run_dir / "input" / "pipeline.json").is_file():
        lines += ["To fix the implementation, edit the candidate in the original project and start a new run.",
                  "After fixing the environment, you can evaluate the frozen input in a new run.",
                  evaluate_command(run_dir / "input" / "pipeline.json")]
    else:
        lines += ["After addressing the cause, retry with a new run directory."]
    return lines
