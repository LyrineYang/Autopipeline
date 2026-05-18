from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_app_server import run_codex_app_server_role
from .codex_session import should_use_codex_app_server
from .command import run_json_command
from .types import AgentRoleConfig


@dataclass(slots=True)
class AgentRunRequest:
    command: str | None
    cwd: Path
    prompt: str
    payload: dict[str, Any]
    telemetry: dict[str, Any]
    logs_dir: Path
    action: str
    role: str
    role_config: AgentRoleConfig
    sensing: Any | None = None
    timeout_seconds: float | None = 600
    budget_ledger: Any | None = None
    session_key: str | None = None
    session_registry_path: Path | None = None
    output_mode: str = "json_file"
    error_cls: type[Exception] = RuntimeError


def role_uses_app_server(role_config: AgentRoleConfig, command: str | None) -> bool:
    return should_use_codex_app_server(role_config, command=command)


def run_agent_role(request: AgentRunRequest) -> Any:
    if role_uses_app_server(request.role_config, request.command):
        return run_codex_app_server_role(
            request.command,
            cwd=request.cwd,
            prompt=request.prompt,
            payload=request.payload,
            telemetry=request.telemetry,
            logs_dir=request.logs_dir,
            action=request.action,
            role=request.role,
            role_config=request.role_config,
            sensing=request.sensing,
            timeout_seconds=request.timeout_seconds,
            budget_ledger=request.budget_ledger,
            session_key=request.session_key,
            session_registry_path=request.session_registry_path,
            output_mode=request.output_mode,
            error_cls=request.error_cls,
        )
    if not request.command:
        raise request.error_cls(f"No command configured for {request.role}.")
    return run_json_command(
        request.command,
        cwd=request.cwd,
        prompt=request.prompt,
        payload=request.payload,
        telemetry=request.telemetry,
        logs_dir=request.logs_dir,
        action=request.action,
        sensing=request.sensing,
        error_cls=request.error_cls,
        timeout_seconds=request.timeout_seconds,
        budget_ledger=request.budget_ledger,
        role=request.role,
        role_config=request.role_config,
    )
