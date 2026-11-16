"""Create portable inference workspaces without importing compute dependencies."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
from importlib.resources import files

from .workloads import EXAMPLES


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".autopipeline", ".git", ".venv"))


def initialize(destination: Path, *, source: Path | None, device: str, dtype: str, example: str = "residual-mlp", model_dir: Path | None = None) -> Path:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}. Choose a new directory.")
    if source:
        source = source.resolve()
        if not (source / "reference.py").is_file():
            raise ValueError("Source needs reference.py defining Model and get_inputs()")
        if source in destination.parents:
            raise ValueError("Destination must be outside the source workspace")
        if any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Source symlinks are unsupported; copy required files first")
    if example not in EXAMPLES:
        raise ValueError(f"Unknown example: {example}")
    if source and (example != "residual-mlp" or model_dir):
        raise ValueError("Use either --source or --example/--model-dir")
    if example == "hf-local":
        if model_dir is None:
            raise ValueError("hf-local requires --model-dir pointing to an existing local model directory")
        model_dir = model_dir.resolve()
        validate_model_directory(model_dir)
    elif model_dir is not None:
        raise ValueError("--model-dir is only supported with --example hf-local")
    workspace = destination / "workload"
    if source:
        copy_workspace(source, workspace)
    else:
        workspace.mkdir(parents=True)
        template = files("autopipeline.workloads").joinpath(example.replace("-", "_") + ".py").read_text(encoding="utf-8")
        (workspace / "reference.py").write_text(template, encoding="utf-8")
        if example == "hf-local":
            write_json(workspace / "model.json", {"model_dir": str(model_dir), "batch_size": 2, "sequence_length": 32,
                                                  "asset_hashes": model_asset_hashes(model_dir)})
    if not (workspace / "candidate.py").exists():
        (workspace / "candidate.py").write_text(
            '"""Optimize this implementation; preserve constructor, weights, and outputs."""\n'
            'from reference import Model\n\n\nclass ModelNew(Model):\n    pass\n', encoding="utf-8")
    workload = {"device": device, "dtype": dtype, "warmup": 5, "repeats": 20,
                "check_seeds": 3, "atol": 1e-5, "rtol": 1e-4}
    write_json(workspace / "workload.json", workload)
    command = ["{python}", "-m", "autopipeline.evaluate"]
    spec = {"name": destination.name, "objective": "Reduce measured model forward latency while preserving all outputs.",
            "workspace_source": "workload", "target_path": "candidate.py", "editable_paths": ["candidate.py"],
            "reference_paths": ["reference.py", "workload.json"],
            "constraints": ["Edit only candidate.py; preserve ModelNew constructor and state_dict compatibility.",
                            "Do not change the reference, inputs, evaluator, metrics, or profiler artifacts.",
                            "No cached outputs, input-specific constants, or skipped computation."],
            "objective_signals": ["forward_latency_ms", "throughput", "peak_vram_mb"],
            "evaluate_command": command, "final_verify_command": command,
            "metrics_path": "metrics.json", "score_metric": "forward_latency_ms", "score_direction": "lower",
            "timeout_seconds": 600, "metadata": {"example": "custom" if source else example, "runtime": "pytorch", "numerical_tolerances": {"atol": 1e-5, "rtol": 1e-4}}}
    write_json(destination / "pipeline.json", spec)
    (destination / "README.md").write_text(
        f"""# Workload Quickstart

Device: {device}. Dtype: {dtype}. Run these commands from this directory with AutoPipeline
and PyTorch installed in the active environment.

## Evaluate and Export

```sh
autopipeline pipeline check --spec pipeline.json
autopipeline pipeline evaluate --spec pipeline.json --run-dir runs/baseline
autopipeline report runs/baseline
autopipeline pipeline export runs/baseline --output-dir exported
autopipeline pipeline evaluate --spec exported/pipeline.json --candidate exported/candidate
```

Open `runs/baseline/view/dashboard.html` for the verdict, latency, and profile. These commands
run without an agent service. Use fresh output directories, or omit `--run-dir` to get a
timestamped directory.

## Run a Tree Search

Authenticate Codex on this machine, or connect another agent with `--executor-command`.
This example allows up to four calls to the configured agent.

```sh
autopipeline pipeline run --spec pipeline.json --executor-backend codex-app-server \\
  --max-rounds 2 --beam-width 2 --children-per-expansion 2 \\
  --max-agent-calls 4 --max-probe-calls 4
```

Each round can retain two passing branches. The run prints its directory and next commands.
Call `autopipeline report RUN_DIR` to inspect a failed or interrupted run.

## Customize the Workload

`workload/reference.py` defines `Model`, `get_inputs()`, and optionally `get_init_inputs()`.
`workload/candidate.py` defines `ModelNew` with a compatible constructor and weight keys.
The candidate is the default edit target. Fix device, dtype, input shapes, weights, and
tolerances before running. Use the same external weights when rechecking an export.

Call `autopipeline pipeline run --help` for search options or `--help-all` for advanced settings.
""",
        encoding="utf-8",
    )
    return destination / "pipeline.json"


def validate_model_directory(directory: Path) -> None:
    if not directory.is_dir() or not (directory / "config.json").is_file():
        raise ValueError(f"Local model needs config.json: {directory}. Download or save a Transformers encoder first.")
    if not any(directory.glob("*.safetensors")) and not any(directory.glob("pytorch_model*.bin")):
        raise ValueError(f"No model weights found in {directory}; expected safetensors or pytorch_model*.bin")


def model_asset_hashes(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in {".json", ".safetensors", ".bin"}:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.name] = digest.hexdigest()
    return result
