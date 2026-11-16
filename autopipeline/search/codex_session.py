from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any

from .types import AgentRoleConfig


CODEX_APP_SERVER_BACKENDS = {"codex-app-server", "codex_app_server", "app-server", "app_server"}
LEGACY_COMMAND_BACKENDS = {"command", "json-command", "json_command", "subprocess"}
CODEX_PROVIDER_NAMES = {"codex", "openai-codex", "openai_codex"}


def should_use_codex_app_server(role_config: AgentRoleConfig, *, command: str | None) -> bool:
    backend = _normalize_backend(role_config.backend)
    if backend in LEGACY_COMMAND_BACKENDS:
        return False
    if backend in CODEX_APP_SERVER_BACKENDS:
        return True
    provider = _normalize_backend(role_config.provider)
    return provider in CODEX_PROVIDER_NAMES and (not command or _looks_like_app_server_command(command))


def session_path_for(logs_dir: Path, *, role: str = "executor") -> Path:
    if role == "executor":
        return logs_dir.parent / "codex_session.json"
    return logs_dir.parent / f"{role}_codex_session.json"


def registry_path_for_node_dir(node_dir: Path) -> Path:
    if node_dir.parent.name == "nodes":
        return node_dir.parent.parent / "agent_sessions.jsonl"
    return node_dir / "agent_sessions.jsonl"


def load_session(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def persist_agent_session(
    *,
    session_path: Path,
    registry_path: Path | None,
    existing_session: dict[str, Any],
    session_key: str | None,
    role: str = "executor",
    status: str,
    thread_id: str,
    turn_id: str,
    turn_status: str,
    thread_resumed: bool,
    cwd: Path,
    logs_dir: Path,
    event_count: int,
    dynamic_tool_call_count: int,
    token_usage: dict[str, Any],
) -> None:
    key = session_key or _default_session_key(logs_dir)
    payload = {
        "session_key": key,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "turn_status": turn_status,
        "thread_resumed": thread_resumed,
        "backend": "codex_app_server",
        "role": role,
        "cwd": str(cwd),
        "logs_dir": str(logs_dir),
        "updated_at": time.time(),
    }
    if thread_id or not existing_session:
        _write_json_atomic(session_path, payload)

    if registry_path is None:
        return
    _append_jsonl(
        registry_path,
        {
            "timestamp": time.time(),
            "session_key": key,
            "role": role,
            "backend": "codex_app_server",
            "status": status,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "thread_resumed": thread_resumed,
            "cwd": str(cwd),
            "logs_dir": str(logs_dir),
            "session_path": str(session_path),
            "event_count": event_count,
            "dynamic_tool_call_count": dynamic_tool_call_count,
            "token_usage": token_usage,
        },
    )




def _default_session_key(logs_dir: Path) -> str:
    try:
        return logs_dir.parent.parent.name
    except IndexError:
        return logs_dir.parent.name


def _normalize_backend(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _looks_like_app_server_command(command: str | None) -> bool:
    if not command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return "app-server" in parts or "app_server" in parts


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)
