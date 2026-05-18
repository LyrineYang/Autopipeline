from __future__ import annotations

import hashlib
import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .codex_dynamic_tools import run_dynamic_tool, sensing_dynamic_tools
from .codex_session import (
    is_codex_app_server_backend,
    load_session,
    persist_agent_session,
    session_path_for,
    should_use_codex_app_server,
)
from .types import AgentRoleConfig


def run_codex_app_server_executor(
    command: str | None,
    *,
    cwd: Path,
    prompt: str,
    payload: dict[str, Any],
    telemetry: dict[str, Any],
    logs_dir: Path,
    role_config: AgentRoleConfig,
    sensing=None,
    timeout_seconds: float | None = 600,
    budget_ledger: Any | None = None,
    session_key: str | None = None,
    session_registry_path: Path | None = None,
    error_cls: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    """Run one executor step through `codex app-server` over stdio JSONL."""

    return run_codex_app_server_role(
        command,
        cwd=cwd,
        prompt=prompt,
        payload=payload,
        telemetry=telemetry,
        logs_dir=logs_dir,
        action="execute",
        role="executor",
        role_config=role_config,
        sensing=sensing,
        timeout_seconds=timeout_seconds,
        budget_ledger=budget_ledger,
        session_key=session_key,
        session_registry_path=session_registry_path,
        output_mode="execution_diff",
        error_cls=error_cls,
    )


def run_codex_app_server_role(
    command: str | None,
    *,
    cwd: Path,
    prompt: str,
    payload: dict[str, Any],
    telemetry: dict[str, Any],
    logs_dir: Path,
    action: str,
    role: str,
    role_config: AgentRoleConfig,
    sensing=None,
    timeout_seconds: float | None = 600,
    budget_ledger: Any | None = None,
    session_key: str | None = None,
    session_registry_path: Path | None = None,
    output_mode: str = "json_message",
    error_cls: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    """Run one agent role through `codex app-server` over stdio JSONL."""

    logs_dir.mkdir(parents=True, exist_ok=True)
    effective_timeout = (
        role_config.timeout_seconds
        if role_config.timeout_seconds is not None
        else timeout_seconds
    )
    prompt_path = logs_dir / "prompt.txt"
    payload_path = logs_dir / "payload.json"
    telemetry_path = logs_dir / "telemetry.json"
    output_path = logs_dir / "output.json"
    status_path = logs_dir / "command_status.json"
    activity_path = logs_dir / "activity.jsonl"
    event_path = logs_dir / "app_server_events.jsonl"
    request_path = logs_dir / "app_server_requests.jsonl"
    stderr_path = logs_dir / "stderr.log"
    cancel_path = logs_dir / "cancel.request"
    session_path = session_path_for(logs_dir, role=role)

    _write_text_atomic(prompt_path, prompt)
    _write_json_atomic(payload_path, payload)
    _write_json_atomic(telemetry_path, telemetry)
    _write_json_atomic(
        status_path,
        {
            "status": "running",
            "action": action,
            "role": role,
            "backend": "codex_app_server",
            "timeout_seconds": effective_timeout,
            "cancel_path": str(cancel_path),
        },
    )

    sensing_paths: dict[str, str] = {}
    if sensing is not None:
        sensing_paths = sensing.prepare(action, logs_dir)

    candidate_path = _candidate_path_from_payload(payload, cwd)
    watched_paths = _watched_paths_from_payload(cwd, payload) if output_mode == "execution_diff" else None
    before = (
        _workspace_fingerprint(cwd, watched_paths=watched_paths)
        if output_mode == "execution_diff"
        else {}
    )
    app_command = _build_app_server_command(command)
    state = _AppServerState()
    started_at = time.monotonic()
    timed_out = False
    cancelled = False
    failed_error: Exception | None = None
    stored_session = load_session(session_path)
    resume_thread_id = str(stored_session.get("thread_id") or "")

    _record_activity(activity_path, "app_server_process_starting", {"command": _redact_command(app_command)})
    with stderr_path.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            app_command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            text=True,
            bufsize=1,
        )
        messages: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=_read_stdout_lines,
            args=(process.stdout, messages),
            daemon=True,
        )
        reader.start()
        _record_activity(activity_path, "app_server_process_started", {"pid": process.pid})

        try:
            _send(process, request_path, {"method": "initialize", "id": 1, "params": _initialize_params()})
            _wait_for_response(
                process,
                messages,
                request_id=1,
                state=state,
                event_path=event_path,
                activity_path=activity_path,
                request_path=request_path,
                deadline=_deadline(started_at, effective_timeout),
                cancel_path=cancel_path,
                sensing=sensing,
                logs_dir=logs_dir,
                action=action,
            )
            _send(process, request_path, {"method": "initialized", "params": {}})

            if resume_thread_id:
                state.thread_resumed = True
                thread_method = "thread/resume"
                thread_params = _resume_params(
                    thread_id=resume_thread_id,
                    cwd=cwd,
                    role_config=role_config,
                )
            else:
                thread_method = "thread/start"
                thread_params = _thread_params(cwd=cwd, role_config=role_config, sensing=sensing)
            _send(process, request_path, {"method": thread_method, "id": 2, "params": thread_params})
            thread_response = _wait_for_response(
                process,
                messages,
                request_id=2,
                state=state,
                event_path=event_path,
                activity_path=activity_path,
                request_path=request_path,
                deadline=_deadline(started_at, effective_timeout),
                cancel_path=cancel_path,
                sensing=sensing,
                logs_dir=logs_dir,
                action=action,
            )
            state.thread_id = _thread_id_from_response(thread_response)
            if not state.thread_id:
                raise error_cls("Codex app-server did not return a thread id.")

            _send(
                process,
                request_path,
                {
                    "method": "turn/start",
                    "id": 3,
                    "params": {
                        "threadId": state.thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": _turn_prompt(
                                    prompt,
                                    payload,
                                    telemetry,
                                    sensing_paths,
                                    action=action,
                                    role=role,
                                    output_mode=output_mode,
                                ),
                            }
                        ],
                        "cwd": str(cwd),
                        "sandbox": "workspaceWrite",
                        "approvalPolicy": "never",
                    },
                },
            )
            _wait_for_turn_completion(
                process,
                messages,
                state=state,
                event_path=event_path,
                activity_path=activity_path,
                request_path=request_path,
                deadline=_deadline(started_at, effective_timeout),
                cancel_path=cancel_path,
                sensing=sensing,
                logs_dir=logs_dir,
                action=action,
            )
        except _Cancelled:
            cancelled = True
            _interrupt_turn(process, request_path, state)
        except _TimedOut:
            timed_out = True
            _interrupt_turn(process, request_path, state)
        except Exception as exc:
            failed_error = exc
        finally:
            if sensing is not None:
                sensing.finalize(action, logs_dir)
            _shutdown_process(process)

    duration_seconds = round(time.monotonic() - started_at, 3)
    after = (
        _workspace_fingerprint(cwd, watched_paths=watched_paths)
        if output_mode == "execution_diff"
        else {}
    )
    changed_paths = sorted(path for path, digest in after.items() if before.get(path) != digest)
    deleted_paths = sorted(path for path in before if path not in after)
    changed = bool(changed_paths or deleted_paths)
    status = (
        "timed_out"
        if timed_out
        else "cancelled"
        if cancelled
        else "failed"
        if failed_error is not None
        else _result_status(state)
    )
    metadata = {
        "backend": "codex_app_server",
        "thread_id": state.thread_id,
        "turn_id": state.turn_id,
        "turn_status": state.turn_status,
        "thread_resumed": state.thread_resumed,
        "changed_paths": changed_paths[:20],
        "deleted_paths": deleted_paths[:20],
        "event_count": state.event_count,
        "command_count": len(state.commands),
        "file_change_count": len(state.file_changes),
        "dynamic_tool_call_count": state.dynamic_tool_calls,
        "latest_diff_bytes": len(state.latest_diff),
        "token_usage": state.token_usage,
        "session_path": str(session_path),
        "session_registry_path": str(session_registry_path) if session_registry_path else "",
        "events_path": str(event_path),
        "requests_path": str(request_path),
    }
    output: dict[str, Any] = {
        "status": status,
        "summary": _summary(state, changed),
        "changed": changed,
        "candidate_path": str(candidate_path),
        "metadata": metadata,
    }
    parse_error: Exception | None = None
    if output_mode == "json_message" and status == "completed":
        try:
            output = _json_output_from_messages(state.agent_messages)
            if not isinstance(output, dict):
                raise ValueError("Codex app-server role output must be a JSON object.")
            output_metadata = output.get("metadata")
            if not isinstance(output_metadata, dict):
                output_metadata = {}
            output["metadata"] = {**output_metadata, "app_server": metadata}
        except Exception as exc:
            parse_error = exc
            status = "invalid_output"
            output = {
                "status": status,
                "summary": str(exc),
                "metadata": metadata,
            }
    status_payload = {
        "status": status,
        "action": action,
        "role": role,
        "backend": "codex_app_server",
        "duration_seconds": duration_seconds,
        "timeout_seconds": effective_timeout,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "returncode": None,
        "thread_id": state.thread_id,
        "turn_id": state.turn_id,
        "thread_resumed": state.thread_resumed,
    }
    turn_failed = (
        output_mode == "json_message"
        and status != "completed"
        and parse_error is None
        and not timed_out
        and not cancelled
        and failed_error is None
    )
    if parse_error is not None:
        status_payload["failure_class"] = "invalid_output"
    if turn_failed:
        status_payload["failure_class"] = "codex_app_server_turn_failed"
    _write_json_atomic(output_path, output)
    _write_json_atomic(status_path, status_payload)
    persist_agent_session(
        session_path=session_path,
        registry_path=session_registry_path,
        existing_session=stored_session,
        session_key=session_key,
        role=role,
        status=status,
        thread_id=state.thread_id,
        turn_id=state.turn_id,
        turn_status=state.turn_status,
        thread_resumed=state.thread_resumed,
        cwd=cwd,
        logs_dir=logs_dir,
        event_count=state.event_count,
        dynamic_tool_call_count=state.dynamic_tool_calls,
        token_usage=state.token_usage,
    )
    _record_budget_command(budget_ledger, status_payload, output)
    if timed_out:
        raise error_cls(f"Codex app-server {role} timed out after {effective_timeout} seconds.")
    if cancelled:
        raise error_cls(f"Codex app-server {role} was cancelled via {cancel_path}.")
    if failed_error is not None:
        raise error_cls(f"Codex app-server {role} failed: {failed_error}") from failed_error
    if parse_error is not None:
        raise error_cls(f"Codex app-server {role} returned invalid JSON: {parse_error}") from parse_error
    if turn_failed:
        raise error_cls(f"Codex app-server {role} turn ended with status {state.turn_status or status}.")
    return output


class _TimedOut(Exception):
    pass


class _Cancelled(Exception):
    pass


class _AppServerState:
    def __init__(self) -> None:
        self.thread_id: str = ""
        self.turn_id: str = ""
        self.turn_status: str = ""
        self.thread_resumed = False
        self.latest_diff: str = ""
        self.token_usage: dict[str, Any] = {}
        self.agent_messages: list[str] = []
        self.commands: list[dict[str, Any]] = []
        self.file_changes: list[dict[str, Any]] = []
        self.dynamic_tool_calls = 0
        self.event_count = 0


def _wait_for_response(
    process: subprocess.Popen[str],
    messages: queue.Queue[str | None],
    *,
    request_id: int,
    state: _AppServerState,
    event_path: Path,
    activity_path: Path,
    request_path: Path,
    deadline: float | None,
    cancel_path: Path,
    sensing,
    logs_dir: Path,
    action: str,
) -> dict[str, Any]:
    while True:
        message = _next_message(process, messages, deadline, cancel_path, sensing, logs_dir, action)
        if message is None:
            raise RuntimeError("Codex app-server exited before responding.")
        _record_raw(event_path, message)
        if message.get("id") == request_id and "method" not in message:
            if message.get("error"):
                raise RuntimeError(f"Codex app-server request {request_id} failed: {message['error']}")
            return message
        _handle_message(process, message, state, event_path, activity_path, request_path, sensing, logs_dir, action)


def _wait_for_turn_completion(
    process: subprocess.Popen[str],
    messages: queue.Queue[str | None],
    *,
    state: _AppServerState,
    event_path: Path,
    activity_path: Path,
    request_path: Path,
    deadline: float | None,
    cancel_path: Path,
    sensing,
    logs_dir: Path,
    action: str,
) -> None:
    while True:
        message = _next_message(process, messages, deadline, cancel_path, sensing, logs_dir, action)
        if message is None:
            raise RuntimeError("Codex app-server exited before turn completion.")
        _record_raw(event_path, message)
        _handle_message(process, message, state, event_path, activity_path, request_path, sensing, logs_dir, action)
        if message.get("method") == "turn/completed":
            return


def _next_message(
    process: subprocess.Popen[str],
    messages: queue.Queue[str | None],
    deadline: float | None,
    cancel_path: Path,
    sensing,
    logs_dir: Path,
    action: str,
) -> dict[str, Any] | None:
    while True:
        if cancel_path.exists():
            raise _Cancelled()
        if deadline is not None and time.monotonic() > deadline:
            raise _TimedOut()
        if sensing is not None:
            sensing.serve_pending(action, logs_dir)
        timeout = 0.05 if deadline is None else max(0.01, min(0.05, deadline - time.monotonic()))
        try:
            line = messages.get(timeout=timeout)
        except queue.Empty:
            if process.poll() is not None:
                return None
            continue
        if line is None:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            return message


def _handle_message(
    process: subprocess.Popen[str],
    message: dict[str, Any],
    state: _AppServerState,
    event_path: Path,
    activity_path: Path,
    request_path: Path,
    sensing,
    logs_dir: Path,
    action: str,
) -> None:
    method = str(message.get("method") or "")
    if method:
        state.event_count += 1
    if method == "thread/started":
        thread_id = _dig(message, "params", "thread", "id")
        if thread_id:
            state.thread_id = str(thread_id)
        _record_activity(activity_path, "thread_started", {"thread_id": state.thread_id})
    elif method == "turn/started":
        turn_id = _dig(message, "params", "turn", "id")
        if turn_id:
            state.turn_id = str(turn_id)
        _record_activity(activity_path, "turn_started", {"thread_id": state.thread_id, "turn_id": state.turn_id})
    elif method == "turn/diff/updated":
        state.latest_diff = str(_dig(message, "params", "diff") or "")
        _record_activity(activity_path, "turn_diff_updated", {"bytes": len(state.latest_diff)})
    elif method == "turn/plan/updated":
        _record_activity(activity_path, "turn_plan_updated", {})
    elif method == "thread/tokenUsage/updated":
        params = message.get("params")
        if isinstance(params, dict):
            state.token_usage = params
        _record_activity(activity_path, "token_usage_updated", {})
    elif method == "item/completed":
        _handle_completed_item(message, state, activity_path)
    elif method == "turn/completed":
        turn = _dig(message, "params", "turn")
        if isinstance(turn, dict):
            state.turn_status = str(turn.get("status") or "")
            if turn.get("id"):
                state.turn_id = str(turn["id"])
        _record_activity(
            activity_path,
            "turn_completed",
            {"thread_id": state.thread_id, "turn_id": state.turn_id, "status": state.turn_status},
        )
    elif method == "item/tool/call" and message.get("id") is not None:
        _handle_dynamic_tool_call(process, message, state, activity_path, request_path, sensing, logs_dir, action)
    elif message.get("id") is not None and method:
        _record_activity(activity_path, "unsupported_server_request", {"method": method})
        _send(
            process,
            request_path,
            {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": f"Unsupported app-server request: {method}",
                },
            },
        )


def _handle_completed_item(message: dict[str, Any], state: _AppServerState, activity_path: Path) -> None:
    item = _dig(message, "params", "item")
    if not isinstance(item, dict):
        return
    item_type = str(item.get("type") or "")
    if item_type == "agentMessage":
        text = item.get("text") or _content_text(item)
        if text:
            state.agent_messages.append(str(text))
        _record_activity(activity_path, "agent_message_completed", {"bytes": len(str(text or ""))})
    elif item_type == "commandExecution":
        state.commands.append(item)
        _record_activity(
            activity_path,
            "command_completed",
            {
                "status": item.get("status"),
                "exit_code": item.get("exitCode"),
                "duration_ms": item.get("durationMs"),
            },
        )
    elif item_type == "fileChange":
        state.file_changes.append(item)
        _record_activity(activity_path, "file_change_completed", {"status": item.get("status")})
    elif item_type == "dynamicToolCall":
        state.dynamic_tool_calls += 1
        _record_activity(activity_path, "dynamic_tool_completed", {"tool": item.get("tool"), "status": item.get("status")})


def _handle_dynamic_tool_call(
    process: subprocess.Popen[str],
    message: dict[str, Any],
    state: _AppServerState,
    activity_path: Path,
    request_path: Path,
    sensing,
    logs_dir: Path,
    action: str,
) -> None:
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    result, activity = run_dynamic_tool(params, sensing=sensing, logs_dir=logs_dir, action=action)
    _record_activity(activity_path, "dynamic_tool_request", activity)
    _send(process, request_path, {"id": message["id"], "result": result})


def _content_text(item: dict[str, Any]) -> str:
    content_items = item.get("contentItems")
    if content_items is None:
        content_items = item.get("content")
    if not isinstance(content_items, list):
        return ""
    parts: list[str] = []
    for content in content_items:
        if not isinstance(content, dict):
            continue
        text = content.get("text") or content.get("content")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _send(process: subprocess.Popen[str], request_path: Path, message: dict[str, Any]) -> None:
    _record_raw(request_path, message)
    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin is closed.")
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_stdout_lines(stdout, messages: queue.Queue[str | None]) -> None:
    try:
        if stdout is not None:
            for line in stdout:
                messages.put(line)
    finally:
        messages.put(None)


def _interrupt_turn(process: subprocess.Popen[str], request_path: Path, state: _AppServerState) -> None:
    if not state.thread_id or not state.turn_id or process.poll() is not None:
        return
    try:
        _send(
            process,
            request_path,
            {
                "method": "turn/interrupt",
                "id": 999,
                "params": {"threadId": state.thread_id, "turnId": state.turn_id},
            },
        )
    except Exception:
        return


def _shutdown_process(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _build_app_server_command(command: str | None) -> list[str]:
    if command:
        return shlex.split(command)
    codex_bin = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
    return [codex_bin, "app-server"]


def _initialize_params() -> dict[str, Any]:
    return {
        "clientInfo": {
            "name": "autokernel_tree",
            "title": "AutoKernel Tree Runtime",
            "version": "0.1.0",
        },
        "capabilities": {"experimentalApi": True},
    }


def _thread_params(*, cwd: Path, role_config: AgentRoleConfig, sensing) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(cwd),
        "approvalPolicy": "never",
        "sandbox": "workspaceWrite",
        "serviceName": "autokernel_tree_agent",
    }
    if role_config.model:
        params["model"] = role_config.model
    dynamic_tools = sensing_dynamic_tools(sensing)
    if dynamic_tools:
        params["dynamicTools"] = dynamic_tools
    return params


def _resume_params(*, thread_id: str, cwd: Path, role_config: AgentRoleConfig) -> dict[str, Any]:
    params = _thread_params(cwd=cwd, role_config=role_config, sensing=None)
    params["threadId"] = thread_id
    return params


def _turn_prompt(
    prompt: str,
    payload: dict[str, Any],
    telemetry: dict[str, Any],
    sensing_paths: dict[str, str],
    *,
    action: str,
    role: str,
    output_mode: str,
) -> str:
    if output_mode == "json_message":
        role_guidance = (
            f"You are the {role} for one tree-search {action} step.\n\n"
            "Return exactly one JSON object in your final message. Do not wrap it in markdown. "
            "Do not write TREE_OUTPUT_PATH even if the embedded prompt mentions it; this "
            "app-server runtime captures your final JSON message directly. Do not edit files "
            "unless the embedded task explicitly asks for a code change.\n\n"
        )
    else:
        role_guidance = (
            "You are the executor for one tree-search branch.\n\n"
            "Work only inside the current sandbox. Make one focused code change if it is likely "
            "to improve the candidate. Do not edit benchmark control files unless the payload "
            "explicitly allows it. Do not write framework result JSON; the runtime will summarize "
            "your file changes after the turn. When more evidence is needed, call run_sensing_probe "
            "instead of editing the probe request file manually.\n\n"
        )
    return role_guidance + (
        f"TREE_PROMPT:\n{prompt}\n\n"
        f"TREE_PAYLOAD_JSON:\n```json\n{json.dumps(payload, indent=2)}\n```\n\n"
        f"TREE_TELEMETRY_JSON:\n```json\n{json.dumps(telemetry, indent=2)}\n```\n\n"
        f"SENSING_PATHS_JSON:\n```json\n{json.dumps(sensing_paths, indent=2)}\n```\n"
    )


def _deadline(started_at: float, timeout_seconds: float | None) -> float | None:
    if timeout_seconds is None:
        return None
    return started_at + timeout_seconds


def _thread_id_from_response(message: dict[str, Any]) -> str:
    value = _dig(message, "result", "thread", "id")
    return str(value or "")


def _result_status(state: _AppServerState) -> str:
    status = state.turn_status.lower()
    if status == "completed" or not status:
        return "completed"
    if status == "interrupted":
        return "cancelled"
    return "failed"


def _summary(state: _AppServerState, changed: bool) -> str:
    status = state.turn_status or "completed"
    if state.agent_messages:
        tail = state.agent_messages[-1].strip().splitlines()[0][:240]
        return f"Codex app-server turn {status}; {tail}"
    if changed:
        return f"Codex app-server turn {status} with sandbox changes."
    return f"Codex app-server turn {status} without modifying watched sandbox files."


def _json_output_from_messages(messages: list[str]) -> dict[str, Any]:
    for message in reversed(messages):
        candidate = _strip_json_message(message)
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in the final app-server agent messages.")


def _strip_json_message(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    if candidate.lower().startswith("json\n"):
        candidate = candidate[5:].strip()
    return candidate


def _candidate_path_from_payload(payload: dict[str, Any], cwd: Path) -> Path:
    raw = payload.get("candidate_path")
    if raw:
        path = Path(str(raw))
        return path if path.is_absolute() else cwd / path
    return cwd / "kernel.py"


def _watched_paths_from_payload(root: Path, payload: dict[str, Any]) -> list[Path] | None:
    raw_paths: list[Any] = []
    candidate_path = payload.get("candidate_path")
    if candidate_path:
        raw_paths.append(candidate_path)
    task = payload.get("task")
    if isinstance(task, dict):
        editable_paths = task.get("editable_paths")
        if isinstance(editable_paths, list):
            raw_paths.extend(editable_paths)
    watched: list[Path] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        path_text = str(raw_path).strip()
        if not path_text:
            continue
        path = Path(path_text)
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        key = resolved.as_posix()
        if key not in seen:
            watched.append(resolved)
            seen.add(key)
    return watched or None


def _workspace_fingerprint(root: Path, *, watched_paths: list[Path] | None = None) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    roots = watched_paths or [root]
    for watched in roots:
        candidates = watched.rglob("*") if watched.exists() and watched.is_dir() else [watched]
        for path in candidates:
            if not path.is_file() or _ignored(path, root):
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            fingerprints[rel] = hashlib.sha256(data).hexdigest()
    return fingerprints


def _ignored(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} for part in rel_parts)


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
        failure_class="" if status_payload.get("status") == "completed" else "codex_app_server",
    )


def _record_activity(path: Path, kind: str, data: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"t": time.time(), "kind": kind, **(data or {})}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _record_raw(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _dig(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _redact_command(command: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for part in command:
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
    return " ".join(shlex.quote(part) for part in redacted)
