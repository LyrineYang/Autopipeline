from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
import time
from typing import Any


def run_json_command(
    command: str,
    *,
    cwd: Path,
    prompt: str,
    payload: dict[str, Any],
    telemetry: dict[str, Any],
    logs_dir: Path,
    action: str = "",
    sensing=None,
    error_cls: type[Exception] = RuntimeError,
    timeout_seconds: float | None = 600,
) -> Any:
    logs_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = logs_dir / "prompt.txt"
    payload_path = logs_dir / "payload.json"
    telemetry_path = logs_dir / "telemetry.json"
    output_path = logs_dir / "output.json"
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"
    cancel_path = logs_dir / "cancel.request"
    status_path = logs_dir / "command_status.json"

    _write_text_atomic(prompt_path, prompt)
    _write_json_atomic(payload_path, payload)
    _write_json_atomic(telemetry_path, telemetry)
    _write_json_atomic(
        status_path,
        {
            "status": "running",
            "action": action or "agent",
            "timeout_seconds": timeout_seconds,
            "cancel_path": str(cancel_path),
        },
    )

    env = os.environ.copy()
    env["TREE_PROMPT_PATH"] = str(prompt_path)
    env["TREE_PAYLOAD_PATH"] = str(payload_path)
    env["TREE_TELEMETRY_PATH"] = str(telemetry_path)
    env["TREE_OUTPUT_PATH"] = str(output_path)
    env["TREE_CANCEL_PATH"] = str(cancel_path)
    if sensing is not None:
        sensing_paths = sensing.prepare(action or "agent", logs_dir)
        env["TREE_SENSING_PATH"] = sensing_paths["manifest"]
        env["TREE_PROBE_REQUESTS_PATH"] = sensing_paths["probe_requests"]
        env["TREE_PROBE_RESULTS_PATH"] = sensing_paths["probe_results"]

    started_at = time.monotonic()
    timed_out = False
    cancelled = False
    returncode = None
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            shell=True,
            start_new_session=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
        while process.poll() is None:
            if sensing is not None:
                sensing.serve_pending(action or "agent", logs_dir)
            if cancel_path.exists():
                cancelled = True
                _terminate_process(process)
                break
            if timeout_seconds is not None and time.monotonic() - started_at > timeout_seconds:
                timed_out = True
                _terminate_process(process)
                break
            time.sleep(0.05)
        returncode = process.returncode
    if sensing is not None:
        sensing.finalize(action or "agent", logs_dir)

    duration_seconds = round(time.monotonic() - started_at, 3)
    if returncode is None:
        returncode = process.poll()
    _write_json_atomic(
        status_path,
        {
            "status": "timed_out" if timed_out else "cancelled" if cancelled else "completed",
            "action": action or "agent",
            "returncode": returncode,
            "duration_seconds": duration_seconds,
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "cancel_path": str(cancel_path),
        },
    )
    if timed_out:
        raise error_cls(
            f"Agent command timed out after {timeout_seconds} seconds: {command}",
        )
    if cancelled:
        raise error_cls(f"Agent command was cancelled via {cancel_path}: {command}")
    if returncode != 0:
        raise error_cls(
            f"Agent command failed with return code {returncode}: {command}",
        )
    if not output_path.exists():
        raise error_cls(f"Agent command did not write {output_path}.")

    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise error_cls(f"Agent command wrote invalid JSON: {exc}") from exc


def logs_for(logs_dir: Path) -> dict[str, str]:
    return {
        "prompt": str(logs_dir / "prompt.txt"),
        "payload": str(logs_dir / "payload.json"),
        "telemetry": str(logs_dir / "telemetry.json"),
        "output": str(logs_dir / "output.json"),
        "stdout": str(logs_dir / "stdout.log"),
        "stderr": str(logs_dir / "stderr.log"),
        "status": str(logs_dir / "command_status.json"),
        "cancel": str(logs_dir / "cancel.request"),
    }


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=1)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
