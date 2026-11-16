from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

_PROFILE_TRACE_PATH_RE = re.compile(r"^profile_trace:\s*(?P<path>.+)$", re.MULTILINE)
_PTXAS_REG_RE = re.compile(r"Used\s+(?P<value>\d+)\s+registers")
_PTXAS_SMEM_RE = re.compile(r"(?P<value>\d+)\s+bytes\s+smem")
_PTXAS_CMEM_RE = re.compile(r"(?P<value>\d+)\s+bytes\s+cmem(?:\[\d+\])?")
_PTXAS_STACK_RE = re.compile(r"(?P<value>\d+)\s+bytes\s+stack frame")
_PTXAS_SPILL_STORE_RE = re.compile(r"(?P<value>\d+)\s+bytes\s+spill stores")
_PTXAS_SPILL_LOAD_RE = re.compile(r"(?P<value>\d+)\s+bytes\s+spill loads")
_DEFAULT_SOFTWARE_AUDIT_CHECKS = {
    "mixed_precision": [r"\bbfloat16\b", r"\bfloat16\b", r"autocast", r"\bamp\b", r"\bbf16\b", r"\bfp16\b"],
    "torch_compile": [r"torch\.compile\s*\("],
    "flash_attention": [r"flash_attn", r"flash_attention", r"scaled_dot_product_attention", r"\bsdpa\b"],
    "distributed": [r"\bFSDP\b", r"DeepSpeed", r"Zero\d?", r"DistributedDataParallel", r"\bDDP\b"],
    "checkpointing": [r"checkpoint", r"activation_checkpoint"],
    "dataloader": [r"DataLoader\s*\(", r"num_workers\s*=", r"pin_memory\s*=", r"prefetch_factor\s*=", r"persistent_workers\s*="],
    "host_sync": [r"\.item\s*\(", r"torch\.cuda\.synchronize\s*\(", r"\.cpu\s*\(", r"\.numpy\s*\("],
    "logging_barrier": [r"dist\.barrier\s*\(", r"barrier\s*\(", r"log", r"wandb", r"tensorboard"],
}


def run_autokernel_probe(
    request: dict[str, Any],
    *,
    cwd: Path,
    repo_root: Path,
    bench_args: list[str],
) -> dict[str, Any]:
    kind = str(request.get("kind", "")).strip()
    if kind == "hardware_profile":
        return _probe_hardware_profile(cwd, request, repo_root)
    if kind == "software_audit":
        return _probe_software_audit(cwd, request)
    if kind == "gpu_status":
        return _probe_gpu_status(cwd)
    if kind == "compile_smoke":
        target = _resolve_probe_path(cwd, request.get("path", "kernel.py"))
        return _probe_compile_smoke(cwd, target)
    if kind == "compile_report":
        target = _resolve_probe_path(cwd, request.get("path", "kernel.py"))
        return _probe_compile_report(cwd, target, request, repo_root)
    if kind == "ncu_profile":
        return _probe_ncu_profile(cwd, request, repo_root, bench_args)
    if kind == "nsys_profile":
        return _probe_nsys_profile(cwd, request, repo_root, bench_args)
    if kind == "stage_summary":
        return _probe_stage_summary(cwd, request, repo_root, bench_args)
    if kind == "runtime_breakdown":
        return _probe_runtime_breakdown(cwd, request, repo_root, bench_args)
    if kind == "python_experiment":
        return _probe_python_experiment(cwd, request, repo_root)
    raise ValueError("Unsupported task probe kind")


def detect_hardware_context() -> dict[str, Any]:
    context: dict[str, Any] = {
        "gpu_name": "unknown",
        "gpu_count": 0,
        "driver_version": "unknown",
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "all"),
        "torch_cuda_available": False,
    }

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines:
                first = [part.strip() for part in lines[0].split(",")]
                context["gpu_count"] = len(lines)
                context["gpu_name"] = first[0]
                if len(first) > 1:
                    context["memory_total_mb"] = float(first[1])
                if len(first) > 2:
                    context["driver_version"] = first[2]
    except OSError:
        pass

    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            device_index = 0
            props = torch.cuda.get_device_properties(device_index)
            context["torch_cuda_available"] = True
            context["gpu_count"] = max(context.get("gpu_count", 0), torch.cuda.device_count())
            context["gpu_name"] = props.name
            context["compute_capability"] = f"{props.major}.{props.minor}"
            context["memory_total_mb"] = round(props.total_memory / (1024 * 1024), 2)
    except Exception:
        pass

    return context


def _resolve_probe_path(cwd: Path, raw: Any) -> Path:
    path = Path(str(raw or "")).expanduser()
    if not path.is_absolute():
        path = (cwd / path).resolve()
    else:
        path = path.resolve()
    cwd_resolved = cwd.resolve()
    if cwd_resolved not in path.parents and path != cwd_resolved:
        raise ValueError("Probe path must stay inside the current working directory.")
    return path


def _probe_gpu_status(cwd: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0:
        summary = "gpu_status probe could not read nvidia-smi."
    else:
        summary = f"gpu_status returned {len(lines)} GPU rows."
    return {
        "summary": summary,
        "data": {
            "returncode": result.returncode,
            "rows": lines,
            "stderr": result.stderr[-4000:],
        },
    }


def _probe_hardware_profile(cwd: Path, request: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    hardware = detect_hardware_context()
    versions = _collect_runtime_versions(cwd, repo_root)
    inventory = _run_optional_command(
        ["nvidia-smi", "-L"],
        cwd=cwd,
        timeout=_probe_timeout(request, default=5, maximum=15),
    )
    topology = _run_optional_command(
        ["nvidia-smi", "topo", "-m"],
        cwd=cwd,
        timeout=_probe_timeout(request, default=5, maximum=15),
    )
    summary = (
        f"hardware_profile collected gpu={hardware.get('gpu_name', 'unknown')} "
        f"count={hardware.get('gpu_count', 0)} "
        f"torch={versions.get('torch_version', 'unknown')} "
        f"triton={versions.get('triton_version', 'unknown')}."
    )
    return {
        "summary": summary,
        "data": {
            "hardware": hardware,
            "runtime_versions": versions,
            "gpu_inventory": inventory.stdout[-4000:] if inventory is not None else "",
            "topology": topology.stdout[-4000:] if topology is not None else "",
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "all"),
        },
        "status": "completed",
    }


def _probe_software_audit(cwd: Path, request: dict[str, Any]) -> dict[str, Any]:
    targets = _software_audit_targets(cwd, request)
    checks = _software_audit_checks(request)
    matches: list[dict[str, Any]] = []
    scanned_files: list[str] = []
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned_files.append(str(path))
        for label, patterns in checks:
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    excerpt = _line_excerpt(text, match.start())
                    matches.append({"check": label, "path": str(path), "excerpt": excerpt})
                    break
    summary = (
        f"software_audit scanned {len(scanned_files)} file(s) and found "
        f"{len(matches)} match(es) across {len(checks)} check(s)."
    )
    return {
        "summary": summary,
        "data": {
            "scanned_files": scanned_files,
            "requested_checks": [label for label, _ in checks],
            "matches": matches,
        },
        "status": "completed",
    }


def _probe_compile_smoke(cwd: Path, target: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["python3", "-m", "py_compile", str(target)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    passed = result.returncode == 0
    return {
        "summary": f"compile_smoke {'passed' if passed else 'failed'} for {target.name}.",
        "data": {
            "path": str(target),
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        },
        "status": "completed" if passed else "failed",
    }


def _probe_compile_report(cwd: Path, target: Path, request: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    command = _request_command(request.get("command"))
    if command is None:
        command = _managed_command_prefix(repo_root, cwd) + ["python3", "-m", "py_compile", str(target)]
    timeout = _probe_timeout(request, default=20, maximum=60)
    result = _run_probe_command(command, cwd=cwd, timeout=timeout)
    report_text = result.stdout + ("\n" + result.stderr if result.stderr else "")
    parsed = _parse_compile_facts(report_text)
    summary_parts = [f"compile_report {'passed' if result.returncode == 0 else 'failed'} for {target.name}"]
    if parsed:
        concise = ", ".join(f"{key}={value}" for key, value in parsed.items())
        summary_parts.append(concise)
    return {
        "summary": "; ".join(summary_parts) + ".",
        "data": {
            "path": str(target),
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
            "parsed": parsed,
        },
        "status": "completed" if result.returncode == 0 else "failed",
    }


def _probe_python_experiment(cwd: Path, request: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    code = str(request.get("code", "")).strip()
    if not code:
        raise ValueError("python_experiment requires non-empty code")
    probe_dir = cwd / ".tree_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    filename = str(request.get("filename", "probe_experiment.py")).strip() or "probe_experiment.py"
    script_path = probe_dir / Path(filename).name
    script_path.write_text(code + "\n", encoding="utf-8")
    raw_args = request.get("args", [])
    args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
    timeout = _probe_timeout(request, default=10, maximum=30)
    env = _probe_env(probe_dir)
    command = _managed_command_prefix(repo_root, cwd) + ["python3", str(script_path), *args]
    if not command:
        command = ["python3", str(script_path), *args]
    result = _run_probe_command(command, cwd=cwd, timeout=timeout, env=env)
    passed = result.returncode == 0
    return {
        "summary": f"python_experiment {'passed' if passed else 'failed'} for {script_path.name}.",
        "data": {
            "path": str(script_path),
            "args": args,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        },
        "status": "completed" if passed else "failed",
    }


def _probe_ncu_profile(
    cwd: Path,
    request: dict[str, Any],
    repo_root: Path,
    bench_args: list[str],
) -> dict[str, Any]:
    output_dir = cwd / ".tree_probe" / "ncu"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_prefix = output_dir / (str(request.get("report_name", "ncu_profile")).strip() or "ncu_profile")
    target_command = _probe_target_command(
        request=request,
        repo_root=repo_root,
        cwd=cwd,
        bench_args=bench_args,
        extra_args=["--quick"],
    )
    command = [
        "ncu",
        "--csv",
        "--page",
        "raw",
        "--target-processes",
        "all",
        "--force-overwrite",
        "true",
        "--export",
        str(report_prefix),
    ]
    metrics = _string_values(request.get("metrics"))
    if metrics:
        command.extend(["--metrics", ",".join(metrics)])
    section = str(request.get("section", "")).strip()
    if section:
        command.extend(["--section", section])
    kernel_name = str(request.get("kernel_name", "")).strip()
    if kernel_name:
        command.extend(["--kernel-name", kernel_name])
    command.extend(target_command)
    result = _run_probe_command(command, cwd=cwd, timeout=_probe_timeout(request, default=120, maximum=300))
    artifacts = sorted(str(path) for path in output_dir.glob(f"{report_prefix.name}*"))
    return {
        "summary": (
            f"ncu_profile {'completed' if result.returncode == 0 else 'failed'} "
            f"with {len(artifacts)} artifact(s)."
        ),
        "data": {
            "command": command,
            "report_prefix": str(report_prefix),
            "artifacts": artifacts,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        },
        "status": "completed" if result.returncode == 0 else "failed",
    }


def _probe_nsys_profile(
    cwd: Path,
    request: dict[str, Any],
    repo_root: Path,
    bench_args: list[str],
) -> dict[str, Any]:
    output_dir = cwd / ".tree_probe" / "nsys"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_prefix = output_dir / (str(request.get("report_name", "nsys_profile")).strip() or "nsys_profile")
    target_command = _probe_target_command(
        request=request,
        repo_root=repo_root,
        cwd=cwd,
        bench_args=bench_args,
        extra_args=["--quick"],
    )
    command = [
        "nsys",
        "profile",
        "--force-overwrite=true",
        "-o",
        str(report_prefix),
    ]
    capture_range = str(request.get("capture_range", "")).strip()
    if capture_range:
        command.extend(["--capture-range", capture_range])
    command.extend(target_command)
    result = _run_probe_command(command, cwd=cwd, timeout=_probe_timeout(request, default=180, maximum=300))
    artifacts = sorted(str(path) for path in output_dir.glob(f"{report_prefix.name}*"))
    return {
        "summary": (
            f"nsys_profile {'completed' if result.returncode == 0 else 'failed'} "
            f"with {len(artifacts)} artifact(s)."
        ),
        "data": {
            "command": command,
            "report_prefix": str(report_prefix),
            "artifacts": artifacts,
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        },
        "status": "completed" if result.returncode == 0 else "failed",
    }


def _probe_stage_summary(
    cwd: Path,
    request: dict[str, Any],
    repo_root: Path,
    bench_args: list[str],
) -> dict[str, Any]:
    trace_path = _resolve_optional_probe_path(cwd, request.get("trace_path"))
    capture = bool(request.get("capture", trace_path is None))
    capture_result = None
    if trace_path is None and capture:
        command = _probe_target_command(
            request=request,
            repo_root=repo_root,
            cwd=cwd,
            bench_args=bench_args,
            extra_args=["--quick", "--profile"],
        )
        capture_result = _run_probe_command(
            command,
            cwd=cwd,
            timeout=_probe_timeout(request, default=180, maximum=300),
        )
        trace_path = _trace_path_from_output(capture_result.stdout, cwd) or _discover_trace_path(cwd)
    if trace_path is None or not trace_path.exists():
        return {
            "summary": "stage_summary could not find a trace artifact.",
            "data": {
                "trace_path": str(trace_path) if trace_path is not None else "",
                "capture_returncode": capture_result.returncode if capture_result is not None else None,
                "stdout": capture_result.stdout[-4000:] if capture_result is not None else "",
                "stderr": capture_result.stderr[-4000:] if capture_result is not None else "",
            },
            "status": "failed",
        }
    summary = _summarize_trace(trace_path)
    return {
        "summary": (
            f"stage_summary found {summary['complete_event_count']} timed events across "
            f"{summary['unique_event_count']} names."
        ),
        "data": {
            "trace_path": str(trace_path),
            **summary,
        },
        "status": "completed",
    }


def _probe_runtime_breakdown(
    cwd: Path,
    request: dict[str, Any],
    repo_root: Path,
    bench_args: list[str],
) -> dict[str, Any]:
    result = _probe_stage_summary(cwd, request, repo_root, bench_args)
    if result.get("status") != "completed":
        return result
    data = dict(result.get("data") or {})
    summary = (
        f"runtime_breakdown summarized {data.get('complete_event_count', 0)} timed events "
        f"and {data.get('unique_event_count', 0)} unique event names."
    )
    return {
        "summary": summary,
        "data": data,
        "status": "completed",
    }


def _managed_command_prefix(repo_root: Path, cwd: Path) -> list[str]:
    if not (repo_root / "pyproject.toml").exists():
        return []
    return [
        "uv",
        "run",
        "--project",
        str(repo_root.resolve()),
        "--directory",
        str(cwd.resolve()),
        "--no-sync",
    ]


def _probe_env(probe_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("UV_CACHE_DIR", str((probe_dir / "uv-cache").resolve()))
    return env


def _run_probe_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        if command[:1] == ["uv"]:
            fallback = _fallback_probe_command(command)
            return subprocess.run(
                fallback,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        raise


def _fallback_probe_command(command: list[str]) -> list[str]:
    tail = command[7:]
    if not tail:
        raise FileNotFoundError("uv fallback has no target command")
    first = tail[0]
    if first.endswith(".py"):
        return ["python3", *tail]
    return tail


def _probe_target_command(
    *,
    request: dict[str, Any],
    repo_root: Path,
    cwd: Path,
    bench_args: list[str],
    extra_args: list[str] | None = None,
) -> list[str]:
    command = _request_command(request.get("command"))
    if command is not None:
        return command
    args = list(bench_args)
    for item in extra_args or []:
        if item not in args:
            args.append(item)
    return _managed_command_prefix(repo_root, cwd) + [str(repo_root / "bench.py"), *args]


def _request_command(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return shlex.split(text) if text else None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    raise ValueError("Probe command must be a string or list.")


def _probe_timeout(request: dict[str, Any], *, default: int, maximum: int) -> int:
    timeout = int(request.get("timeout_seconds", default))
    return max(1, min(timeout, maximum))


def _resolve_optional_probe_path(cwd: Path, raw: Any) -> Path | None:
    if raw in (None, ""):
        return None
    return _resolve_probe_path(cwd, raw)


def _parse_compile_facts(text: str) -> dict[str, int]:
    facts: dict[str, int] = {}
    patterns = {
        "registers": _PTXAS_REG_RE,
        "shared_mem_bytes": _PTXAS_SMEM_RE,
        "constant_mem_bytes": _PTXAS_CMEM_RE,
        "stack_frame_bytes": _PTXAS_STACK_RE,
        "spill_store_bytes": _PTXAS_SPILL_STORE_RE,
        "spill_load_bytes": _PTXAS_SPILL_LOAD_RE,
    }
    for key, pattern in patterns.items():
        match = pattern.search(text)
        if match:
            facts[key] = int(match.group("value"))
    return facts


def _collect_runtime_versions(cwd: Path, repo_root: Path) -> dict[str, Any]:
    script = (
        "import json, platform, sys\n"
        "data={'python_version': sys.version.split()[0], 'platform': platform.platform()}\n"
        "try:\n"
        " import torch\n"
        " data['torch_version']=torch.__version__\n"
        " data['cuda_version']=getattr(torch.version, 'cuda', None)\n"
        " data['nccl_available']=bool(getattr(torch.cuda, 'nccl', None)) if hasattr(torch, 'cuda') else False\n"
        "except Exception as exc:\n"
        " data['torch_error']=str(exc)\n"
        "try:\n"
        " import triton\n"
        " data['triton_version']=triton.__version__\n"
        "except Exception as exc:\n"
        " data['triton_error']=str(exc)\n"
        "print(json.dumps(data))\n"
    )
    command = _managed_command_prefix(repo_root, cwd) + ["python3", "-c", script]
    if not command:
        command = ["python3", "-c", script]
    result = _run_probe_command(command, cwd=cwd, timeout=20, env=_probe_env(cwd / ".tree_probe"))
    try:
        return json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {
            "python_version": "unknown",
            "probe_stdout": result.stdout[-2000:],
            "probe_stderr": result.stderr[-2000:],
        }


def _software_audit_checks(request: dict[str, Any]) -> list[tuple[str, list[str]]]:
    raw = request.get("checks")
    if not isinstance(raw, list) or not raw:
        return list(_DEFAULT_SOFTWARE_AUDIT_CHECKS.items())

    checks: list[tuple[str, list[str]]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            patterns = _DEFAULT_SOFTWARE_AUDIT_CHECKS.get(item)
            if patterns:
                checks.append((item, patterns))
            continue
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", f"check_{index + 1}")).strip() or f"check_{index + 1}"
        patterns = item.get("patterns")
        if isinstance(patterns, list):
            normalized = [str(pattern).strip() for pattern in patterns if str(pattern).strip()]
            if normalized:
                checks.append((label, normalized))
    return checks or list(_DEFAULT_SOFTWARE_AUDIT_CHECKS.items())


def _run_optional_command(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except OSError:
        return None


def _software_audit_targets(cwd: Path, request: dict[str, Any]) -> list[Path]:
    if "paths" in request and isinstance(request.get("paths"), list):
        return [_resolve_probe_path(cwd, item) for item in request["paths"]]
    if "path" in request:
        return [_resolve_probe_path(cwd, request.get("path"))]
    targets = sorted(path for path in cwd.glob("*.py") if path.is_file())[:12]
    return targets or [cwd / "kernel.py"]


def _line_excerpt(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end].strip()


def _trace_path_from_output(text: str, cwd: Path) -> Path | None:
    match = _PROFILE_TRACE_PATH_RE.search(text)
    if not match:
        return None
    raw = match.group("path").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = (cwd / path).resolve()
    return path if path.exists() else None


def _discover_trace_path(cwd: Path) -> Path | None:
    candidates = [
        cwd / "traces" / "kernel_trace.json",
        cwd / "trace.json",
        cwd / "workspace" / "trace.json",
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()
    return None


def _summarize_trace(trace_path: Path) -> dict[str, Any]:
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        events = payload.get("traceEvents", [])
    elif isinstance(payload, list):
        events = payload
    else:
        events = []

    complete_events = [event for event in events if isinstance(event, dict) and event.get("ph") == "X"]
    aggregates: dict[str, dict[str, float]] = {}
    memcpy_count = 0
    sync_count = 0
    kernel_like_count = 0
    total_timed_us = 0.0
    for event in complete_events:
        name = str(event.get("name", "unknown"))
        duration = float(event.get("dur") or 0.0)
        total_timed_us += duration
        item = aggregates.setdefault(name, {"count": 0.0, "total_us": 0.0})
        item["count"] += 1
        item["total_us"] += duration
        lowered = name.lower()
        if "memcpy" in lowered or "copy" in lowered:
            memcpy_count += 1
        if "sync" in lowered or "wait" in lowered or "barrier" in lowered:
            sync_count += 1
        if "kernel" in lowered or "triton" in lowered or "cuda" in lowered:
            kernel_like_count += 1

    top_events = sorted(
        (
            {
                "name": name,
                "count": int(values["count"]),
                "total_us": round(values["total_us"], 3),
            }
            for name, values in aggregates.items()
        ),
        key=lambda item: item["total_us"],
        reverse=True,
    )[:5]
    return {
        "event_count": len(events),
        "complete_event_count": len(complete_events),
        "unique_event_count": len(aggregates),
        "kernel_like_count": kernel_like_count,
        "memcpy_count": memcpy_count,
        "sync_or_wait_count": sync_count,
        "total_timed_us": round(total_timed_us, 3),
        "top_events": top_events,
    }


def _string_values(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return []
