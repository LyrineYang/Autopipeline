from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

from .roles import normalize_role_config
from .types import AgentRoleConfig


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
    budget_ledger: Any | None = None,
    role: str | None = None,
    role_config: AgentRoleConfig | dict[str, Any] | None = None,
) -> Any:
    logs_dir.mkdir(parents=True, exist_ok=True)
    resolved_role = role or action or "agent"
    resolved_role_config = normalize_role_config(role_config)
    effective_timeout = (
        resolved_role_config.timeout_seconds
        if resolved_role_config.timeout_seconds is not None
        else timeout_seconds
    )
    prompt_path = logs_dir / "prompt.txt"
    payload_path = logs_dir / "payload.json"
    telemetry_path = logs_dir / "telemetry.json"
    output_path = logs_dir / "output.json"
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"
    cancel_path = logs_dir / "cancel.request"
    status_path = logs_dir / "command_status.json"
    activity_path = logs_dir / "activity.jsonl"
    diagnostic_path = logs_dir / "timeout_diagnostic.json"
    env_redacted_path = logs_dir / "env_redacted.json"

    _write_text_atomic(prompt_path, prompt)
    _write_json_atomic(payload_path, payload)
    _write_json_atomic(telemetry_path, telemetry)
    _write_json_atomic(
        status_path,
        {
            "status": "running",
            "action": action or "agent",
            "role": resolved_role,
            "timeout_seconds": effective_timeout,
            "cancel_path": str(cancel_path),
        },
    )
    _write_json_atomic(
        env_redacted_path,
        _redacted_environment_snapshot(
            command=command,
            cwd=cwd,
            action=action or "agent",
            role=resolved_role,
            timeout_seconds=effective_timeout,
            role_config=resolved_role_config,
            paths={
                "prompt": prompt_path,
                "payload": payload_path,
                "telemetry": telemetry_path,
                "output": output_path,
                "cancel": cancel_path,
                "activity": activity_path,
            },
            sensing_enabled=sensing is not None,
        ),
    )

    env = os.environ.copy()
    env["TREE_PROMPT_PATH"] = str(prompt_path)
    env["TREE_PAYLOAD_PATH"] = str(payload_path)
    env["TREE_TELEMETRY_PATH"] = str(telemetry_path)
    env["TREE_OUTPUT_PATH"] = str(output_path)
    env["TREE_CANCEL_PATH"] = str(cancel_path)
    env["TREE_ACTIVITY_PATH"] = str(activity_path)
    env["TREE_AGENT_ROLE"] = resolved_role
    _inject_role_env(env, resolved_role_config, effective_timeout)
    sensing_paths: dict[str, str] = {}
    if sensing is not None:
        sensing_paths = sensing.prepare(action or "agent", logs_dir)
        env["TREE_SENSING_PATH"] = sensing_paths["manifest"]
        env["TREE_PROBE_REQUESTS_PATH"] = sensing_paths["probe_requests"]
        env["TREE_PROBE_RESULTS_PATH"] = sensing_paths["probe_results"]

    started_at = time.monotonic()
    timed_out = False
    cancelled = False
    returncode = None
    observed = _ActivityObserver(
        activity_path,
        {
            "stdout_changed": stdout_path,
            "stderr_changed": stderr_path,
            "output_written": output_path,
            "probe_request_changed": Path(sensing_paths["probe_requests"]) if sensing_paths.get("probe_requests") else None,
            "probe_results_written": Path(sensing_paths["probe_results"]) if sensing_paths.get("probe_results") else None,
        },
    )
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
        observed.record("process_started", {"pid": process.pid})
        while process.poll() is None:
            if sensing is not None:
                sensing.serve_pending(action or "agent", logs_dir)
            observed.poll()
            if cancel_path.exists():
                cancelled = True
                observed.record("cancel_requested", {"path": str(cancel_path)})
                _terminate_process(process)
                break
            if effective_timeout is not None and time.monotonic() - started_at > effective_timeout:
                timed_out = True
                observed.record("timeout_reached", {"timeout_seconds": effective_timeout})
                _terminate_process(process)
                break
            time.sleep(0.05)
        returncode = process.returncode
    observed.poll()
    if sensing is not None:
        sensing.finalize(action or "agent", logs_dir)
        observed.poll()

    duration_seconds = round(time.monotonic() - started_at, 3)
    if returncode is None:
        returncode = process.poll()
    status_payload = {
        "status": "timed_out" if timed_out else "cancelled" if cancelled else "completed",
        "action": action or "agent",
        "role": resolved_role,
        "returncode": returncode,
        "duration_seconds": duration_seconds,
        "timeout_seconds": effective_timeout,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "cancel_path": str(cancel_path),
    }
    _write_json_atomic(status_path, status_payload)
    if timed_out:
        status_payload["failure_class"] = _classify_failure(
            status="timed_out",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            observed=observed,
        )
        _write_json_atomic(status_path, status_payload)
        _write_failure_diagnostic(
            diagnostic_path,
            status_payload=status_payload,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            sensing_paths=sensing_paths,
            observed=observed,
        )
        _record_budget_command(budget_ledger, status_payload, None)
        raise error_cls(
            f"Agent command timed out after {effective_timeout} seconds: {_redact_command(command)}",
        )
    if cancelled:
        status_payload["failure_class"] = "cancelled"
        _write_json_atomic(status_path, status_payload)
        _write_failure_diagnostic(
            diagnostic_path,
            status_payload=status_payload,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            sensing_paths=sensing_paths,
            observed=observed,
        )
        _record_budget_command(budget_ledger, status_payload, None)
        raise error_cls(f"Agent command was cancelled via {cancel_path}: {_redact_command(command)}")
    if returncode != 0:
        status_payload["status"] = "failed"
        status_payload["failure_class"] = _classify_failure(
            status="failed",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            observed=observed,
        )
        _write_json_atomic(status_path, status_payload)
        _write_failure_diagnostic(
            diagnostic_path,
            status_payload=status_payload,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            sensing_paths=sensing_paths,
            observed=observed,
        )
        _record_budget_command(budget_ledger, status_payload, None)
        raise error_cls(
            f"Agent command failed with return code {returncode}: {_redact_command(command)}",
        )
    if not output_path.exists():
        status_payload["status"] = "missing_output"
        status_payload["failure_class"] = "missing_output"
        _write_json_atomic(status_path, status_payload)
        _write_failure_diagnostic(
            diagnostic_path,
            status_payload=status_payload,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            sensing_paths=sensing_paths,
            observed=observed,
        )
        _record_budget_command(budget_ledger, status_payload, None)
        raise error_cls(f"Agent command did not write {output_path}.")

    try:
        output = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        status_payload["status"] = "invalid_output"
        status_payload["failure_class"] = "invalid_output"
        _write_json_atomic(status_path, status_payload)
        _write_failure_diagnostic(
            diagnostic_path,
            status_payload=status_payload,
            command=command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_path=output_path,
            sensing_paths=sensing_paths,
            observed=observed,
        )
        _record_budget_command(budget_ledger, status_payload, None)
        raise error_cls(f"Agent command wrote invalid JSON: {exc}") from exc
    _record_budget_command(budget_ledger, status_payload, output)
    return output


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
        "activity": str(logs_dir / "activity.jsonl"),
        "diagnostic": str(logs_dir / "timeout_diagnostic.json"),
        "env": str(logs_dir / "env_redacted.json"),
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


def _record_budget_command(
    budget_ledger: Any | None,
    status_payload: dict[str, Any],
    output: Any,
) -> None:
    recorder = getattr(budget_ledger, "record_command", None)
    if not callable(recorder):
        return
    recorder(
        action=str(status_payload.get("action", "agent")),
        status=str(status_payload.get("status", "unknown")),
        duration_seconds=float(status_payload.get("duration_seconds") or 0.0),
        output=output,
        role=str(status_payload.get("role") or status_payload.get("action") or "agent"),
        failure_class=str(status_payload.get("failure_class") or ""),
    )


class _ActivityObserver:
    def __init__(self, activity_path: Path, watched_paths: dict[str, Path | None]) -> None:
        self.activity_path = activity_path
        self.watched_paths = watched_paths
        self.last_sizes: dict[str, int] = {}
        self.last_activity: dict[str, Any] | None = None

    def record(self, kind: str, data: dict[str, Any] | None = None) -> None:
        payload = {
            "t": time.time(),
            "kind": kind,
            **(data or {}),
        }
        self.last_activity = payload
        self.activity_path.parent.mkdir(parents=True, exist_ok=True)
        with self.activity_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def poll(self) -> None:
        for kind, path in self.watched_paths.items():
            if path is None or not path.exists():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            key = str(path)
            if size > 0 and self.last_sizes.get(key) != size:
                self.last_sizes[key] = size
                self.record(kind, {"path": key, "bytes": size})


def _inject_role_env(
    env: dict[str, str],
    role_config: AgentRoleConfig,
    timeout_seconds: float | None,
) -> None:
    optional_values = {
        "TREE_AGENT_PROVIDER": role_config.provider,
        "TREE_AGENT_MODEL": role_config.model,
        "TREE_AGENT_BASE_URL": role_config.base_url,
        "TREE_AGENT_REASONING_EFFORT": role_config.reasoning_effort,
        "TREE_AGENT_MAX_OUTPUT_TOKENS": role_config.max_output_tokens,
        "TREE_AGENT_TEMPERATURE": role_config.temperature,
        "TREE_AGENT_SUBAGENT": role_config.subagent,
        "TREE_AGENT_TIMEOUT_SECONDS": timeout_seconds,
    }
    for key, value in optional_values.items():
        if value is not None:
            env[key] = str(value)


def _redacted_environment_snapshot(
    *,
    command: str,
    cwd: Path,
    action: str,
    role: str,
    timeout_seconds: float | None,
    role_config: AgentRoleConfig,
    paths: dict[str, Path],
    sensing_enabled: bool,
) -> dict[str, Any]:
    return {
        "action": action,
        "role": role,
        "command": _redact_command(command),
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "role_config": {
            "provider": role_config.provider,
            "model": role_config.model,
            "base_url_host": _host_only(role_config.base_url),
            "reasoning_effort": role_config.reasoning_effort,
            "max_output_tokens": role_config.max_output_tokens,
            "temperature": role_config.temperature,
            "subagent": role_config.subagent,
        },
        "paths": {key: str(path) for key, path in paths.items()},
        "sensing_enabled": sensing_enabled,
    }


def _write_failure_diagnostic(
    diagnostic_path: Path,
    *,
    status_payload: dict[str, Any],
    command: str,
    stdout_path: Path,
    stderr_path: Path,
    output_path: Path,
    sensing_paths: dict[str, str],
    observed: _ActivityObserver,
) -> None:
    last_activity = observed.last_activity or {}
    age = None
    if last_activity.get("t") is not None:
        age = round(time.time() - float(last_activity["t"]), 3)
    payload = {
        "action": status_payload.get("action"),
        "role": status_payload.get("role"),
        "status": status_payload.get("status"),
        "failure_class": status_payload.get("failure_class"),
        "duration_seconds": status_payload.get("duration_seconds"),
        "timeout_seconds": status_payload.get("timeout_seconds"),
        "started": bool(last_activity),
        "returncode": status_payload.get("returncode"),
        "command": _redact_command(command),
        "output_exists": output_path.exists(),
        "last_activity_kind": last_activity.get("kind"),
        "last_activity_age_seconds": age,
        "stdout_tail": _tail_text(stdout_path),
        "stderr_tail": _tail_text(stderr_path),
        "output_tail": _tail_text(output_path),
        "probe_request_tail": _tail_text(Path(sensing_paths["probe_requests"])) if sensing_paths.get("probe_requests") else "",
        "probe_result_tail": _tail_text(Path(sensing_paths["probe_results"])) if sensing_paths.get("probe_results") else "",
    }
    _write_json_atomic(diagnostic_path, payload)


def _classify_failure(
    *,
    status: str,
    stdout_path: Path,
    stderr_path: Path,
    output_path: Path,
    observed: _ActivityObserver,
) -> str:
    text = f"{_tail_text(stdout_path)}\n{_tail_text(stderr_path)}".lower()
    if status == "timed_out":
        if "auth" in text or "credential" in text or "api key" in text or "model" in text:
            return "provider_unreachable_or_auth"
        last_kind = (observed.last_activity or {}).get("kind")
        if last_kind == "probe_request_changed":
            return "probe_wait_loop"
        if not output_path.exists() and not text.strip():
            return "zero_call_timeout"
        return "command_hung"
    if "compile" in text and ("fail" in text or "error" in text):
        return "benchmark_or_compile_failure"
    return "command_failed"


def _tail_text(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _host_only(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.netloc or parsed.path or None


def _redact_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    redacted: list[str] = []
    redact_next = False
    for part in parts:
        lower = part.lower()
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if any(token in lower for token in ("api-key", "api_key", "token", "secret", "password")):
            if "=" in part:
                redacted.append(part.split("=", 1)[0] + "=[REDACTED]")
            else:
                redacted.append(part)
                redact_next = True
            continue
        redacted.append(part)
    return " ".join(redacted)
