"""Acceptance of the installed CPU product, outside its source checkout.

Run with the target environment's Python. Requires its installed CPU extra.
Executes real model evaluation and rejection, but makes no agent/API calls.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1]
    module = importlib.util.find_spec("autopipeline")
    assert module and module.origin and Path(module.origin).resolve().parent != source / "autopipeline", "Must import the installed package, not the checkout"
    assert root != source and source not in root.parents, "Run acceptance outside the source checkout"
    cli = Path(sys.executable).parent / "autopipeline"
    assert cli.is_file(), f"Missing installed entrypoint: {cli}"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    results = []

    def run(*parts, expected=0):
        result = subprocess.run([str(cli), *map(str, parts)], cwd=root, env=env,
                                capture_output=True, text=True, timeout=180)
        log = root / f"{len(results) + 1:02d}-{parts[0]}.log"
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        results.append({"command": list(map(str, parts)), "exit_code": result.returncode, "log": str(log)})
        assert result.returncode == expected, f"Expected {expected}, got {result.returncode}; see {log}"
        return result.stdout

    def follow(text, operation):
        line = next(line for line in text.splitlines() if line.startswith(f"autopipeline pipeline {operation} "))
        return run(*shlex.split(line)[1:])

    def receipt_path(text):
        return Path(next(line.removeprefix("Result: ") for line in text.splitlines() if line.startswith("Result: ")))

    version = run("--version").strip()
    project = root / "my workload"
    initialized = run("pipeline", "init", project, "--device", "cpu")
    checked = follow(initialized, "check")
    evaluated = follow(checked, "evaluate")
    receipt = receipt_path(evaluated)
    baseline = json.loads(receipt.read_text())
    assert baseline["verified"] and baseline["status"] == "verified"
    assert "Evaluation passed" in evaluated
    metrics = json.loads((receipt.parent / "promoted_pipeline/metrics.json").read_text())
    assert metrics["correctness"] == "PASS" and metrics["compared_elements"] > 0
    assert metrics["forward_latency_ms"] > 0 and metrics["profiler"]["status"] == "completed"
    report = run("report", receipt.parent)
    assert "Evaluation passed" in report and "Final correctness: Passed" in report
    final_report = json.loads((receipt.parent / "final_verification/verification_report.json").read_text())
    overview = json.loads((receipt.parent / "view/run_summary.json").read_text())["result"]
    assert overview["correctness_passed"] is True
    assert overview["reference_latency_ms"] == final_report["reference"]["metrics"]["forward_latency_ms"]
    assert overview["candidate_latency_ms"] == final_report["candidate"]["metrics"]["forward_latency_ms"]
    assert overview["duration_seconds"] == baseline["duration_seconds"] > 0
    assert overview["agent_calls"] == overview["total_tokens"] == 0
    dashboard = (receipt.parent / "view/dashboard.html").read_text()
    assert '<html lang="en">' in dashboard and "Reference forward latency" in dashboard
    assert not list(receipt.parent.rglob("__pycache__"))
    assert not (receipt.parent / "baseline/verification_report.json").exists()
    trial = json.loads((receipt.parent / "baseline/trial.json").read_text())
    assert Path(trial["verification_report"]).is_file()
    exported = follow(report, "export")
    rechecked = follow(exported, "evaluate")
    assert json.loads(receipt_path(rechecked).read_text())["verified"]
    old_receipt = receipt.read_bytes()
    run("pipeline", "evaluate", "--spec", project / "pipeline.json", "--run-dir", receipt.parent, expected=2)
    assert receipt.read_bytes() == old_receipt, "Existing result was overwritten"
    run("pipeline", "run", "--spec", project / "pipeline.json", "--run-dir", root / "no-agent", expected=2)
    assert not (root / "no-agent").exists(), "Missing-agent validation should precede execution"
    (project / "workload/candidate.py").write_text(
        "from reference import Model\nclass ModelNew(Model):\n def forward(self, x):\n  return super().forward(x) + 100\n")
    failed = root / "wrong candidate"
    output = run("pipeline", "evaluate", "--spec", project / "pipeline.json", "--run-dir", failed, expected=1)
    assert "Run failed" in output and "pipeline export" not in output
    assert "Candidate diagnostics:" in output and "edit the candidate in the original project" in output
    assert not json.loads((failed / "result.json").read_text())["verified"]
    assert "Run failed" in run("report", failed)
    run("pipeline", "export", failed, "--output-dir", root / "invalid-export", expected=2)
    assert not (root / "invalid-export").exists()
    (root / "acceptance.json").write_text(json.dumps({"version": version, "import_path": module.origin,
        "status": "passed", "real_cpu_evaluation": True, "agent_calls": 0, "cuda_tested": False,
        "commands": results}, indent=2), encoding="utf-8")
    print(f"PASS: installed CPU workflow, report, export, recheck and rejection; {root / 'acceptance.json'}")


if __name__ == "__main__":
    main()
