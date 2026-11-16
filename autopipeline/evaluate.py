"""Fixed PyTorch evaluator for user-supplied Model / ModelNew workloads.

This module belongs to the installed library, outside the agent's editable files.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load workload module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("workload.json must contain an object")
    if config.get("device") not in {"cpu", "cuda"}:
        raise ValueError("device must be cpu or cuda")
    if config.get("dtype") not in {"float32", "float16", "bfloat16"}:
        raise ValueError("dtype must be float32, float16, or bfloat16")
    for key in ("warmup", "repeats", "check_seeds"):
        value = config.get(key)
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("atol", "rtol"):
        value = config.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be finite and non-negative")


def evaluate(workspace: Path, reference_workspace: Path, config: dict, *, reference_role: bool = False) -> dict:
    validate_config(config)
    import torch

    device = config["device"]
    dtype = getattr(torch, config["dtype"])
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Install a matching CUDA PyTorch build on an NVIDIA server, or explicitly initialize a CPU workload.")

    def seed(value):
        random.seed(value)
        torch.manual_seed(value)
        try:
            import numpy as np
            np.random.seed(value)
        except ImportError:
            pass

    def move(value):
        if isinstance(value, torch.Tensor):
            return value.detach().clone().to(device=device, dtype=dtype if value.is_floating_point() else value.dtype)
        if isinstance(value, dict):
            return {key: move(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(move(item) for item in value)
        return value

    def call(model, inputs):
        return model(**inputs) if isinstance(inputs, dict) else model(*inputs)

    def sync():
        if device == "cuda":
            torch.cuda.synchronize()

    sys.path.insert(0, str(reference_workspace))
    reference = load_module(reference_workspace / "reference.py", "_autopipeline_reference")
    sys.path.insert(0, str(workspace))
    candidate = reference if reference_role else load_module(workspace / "candidate.py", "_autopipeline_candidate")
    seed(0)
    init_inputs = getattr(reference, "get_init_inputs", lambda: [])()
    ref_model = reference.Model(*init_inputs).to(device=device, dtype=dtype).eval()
    seed(0)
    candidate_class = reference.Model if reference_role else candidate.ModelNew
    model = candidate_class(*init_inputs).to(device=device, dtype=dtype).eval()
    model.load_state_dict(ref_model.state_dict(), strict=True)
    errors = {"max_abs_error": 0.0, "max_rel_error": 0.0, "max_normalized_error": 0.0, "compared_elements": 0}

    def compare(expected, actual):
        if isinstance(expected, torch.Tensor):
            if not isinstance(actual, torch.Tensor) or expected.shape != actual.shape or expected.dtype != actual.dtype:
                raise ValueError("Output tensor shape or dtype differs from reference")
            if expected.is_complex():
                raise ValueError("Complex outputs require a custom evaluator")
            if not bool(torch.isfinite(expected).all()) or not bool(torch.isfinite(actual).all()):
                raise ValueError("Non-finite model output")
            if expected.numel() == 0:
                return
            errors["compared_elements"] += expected.numel()
            if not expected.is_floating_point():
                if not torch.equal(expected, actual):
                    raise ValueError("Integer or boolean output differs from reference")
                return
            expected_double = expected.double()
            diff = (expected_double - actual.double()).abs()
            magnitude = expected_double.abs()
            bound = config["atol"] + config["rtol"] * magnitude
            ratio = torch.where(diff == 0, 0.0, diff / bound)
            relative = diff / magnitude.clamp_min(1e-300)
            for key, values in (("max_abs_error", diff), ("max_rel_error", relative), ("max_normalized_error", ratio)):
                errors[key] = max(errors[key], values.max().item())
        elif isinstance(expected, dict):
            if not isinstance(actual, dict) or expected.keys() != actual.keys():
                raise ValueError("Output dictionary keys differ from reference")
            for key in expected:
                compare(expected[key], actual[key])
        elif isinstance(expected, (list, tuple)):
            if type(actual) is not type(expected) or len(expected) != len(actual):
                raise ValueError("Output sequence structure differs from reference")
            for first, second in zip(expected, actual):
                compare(first, second)
        elif type(expected) is not type(actual) or expected != actual:
            raise ValueError("Non-tensor output differs from reference")

    with torch.inference_mode():
        for index in range(config["check_seeds"]):
            seed(1000 + index)
            inputs = reference.get_inputs()
            if not isinstance(inputs, (tuple, list, dict)):
                raise ValueError("get_inputs() must return positional arguments or keyword arguments")
            seed(2000 + index)
            expected = call(ref_model, move(inputs))
            seed(2000 + index)
            actual = call(model, move(inputs))
            compare(expected, actual)
        if not errors["compared_elements"]:
            raise ValueError("Workload must return at least one non-empty tensor")
        if errors["max_normalized_error"] > 1:
            raise ValueError(f"Numerical tolerance exceeded: normalized error {errors['max_normalized_error']:.6g}")
        seed(3000)
        timing_inputs = reference.get_inputs()
        for _ in range(config["warmup"]):
            call(model, move(timing_inputs))
        sync()
        if device == "cuda":
            torch.cuda.reset_peak_memory_stats()
        samples = []
        for _ in range(config["repeats"]):
            inputs = move(timing_inputs)
            sync()
            start = time.perf_counter()
            call(model, inputs)
            sync()
            samples.append((time.perf_counter() - start) * 1000)
        peak = torch.cuda.max_memory_allocated() / 1024**2 if device == "cuda" else None
        artifacts = workspace / ".autopipeline"
        artifacts.mkdir(exist_ok=True)
        try:
            activities = [torch.profiler.ProfilerActivity.CPU]
            if device == "cuda":
                activities.append(torch.profiler.ProfilerActivity.CUDA)
            inputs = move(timing_inputs)
            with torch.profiler.profile(activities=activities, record_shapes=True, profile_memory=True) as prof:
                call(model, inputs)
                sync()
            prof.export_chrome_trace(str(artifacts / "trace.json"))
            table = prof.key_averages().table(sort_by="self_device_time_total" if device == "cuda" else "self_cpu_time_total", row_limit=30)
            (artifacts / "operators.txt").write_text(table, encoding="utf-8")
            profiler = {"status": "completed", "trace": ".autopipeline/trace.json", "operators": ".autopipeline/operators.txt"}
        except Exception as exc:
            profiler = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}

    latency_ms = statistics.median(samples)
    return {"correctness": "PASS", **errors,
            "numerical_atol": config["atol"], "numerical_rtol": config["rtol"],
            "end_to_end_ms": latency_ms, "latency_samples_ms": samples,
            "throughput": 1000 / latency_ms, "peak_vram_mb": peak,
            "measurement_scope": "model forward, including host dispatch and device synchronization; excluding input preparation",
            "profiler": profiler, "device": device,
            "gpu": torch.cuda.get_device_name() if device == "cuda" else None,
            "torch_version": torch.__version__, "python_version": sys.version.split()[0],
            "workload_config": config}


def main() -> int:
    workspace = Path(os.environ.get("PIPELINE_WORKSPACE", ".")).resolve()
    reference = Path(os.environ.get("PIPELINE_REFERENCE_WORKSPACE", str(workspace))).resolve()
    metrics = Path(os.environ.get("PIPELINE_METRICS_PATH", str(workspace / "metrics.json")))
    try:
        config = json.loads((reference / "workload.json").read_text())
        result = evaluate(workspace, reference, config, reference_role=os.environ.get("PIPELINE_VERIFICATION_ROLE") == "reference")
        code = 0
    except Exception as exc:
        result = {"correctness": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        print(result["error"], file=sys.stderr)
        code = 1
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
