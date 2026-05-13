from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .types import AgentRoleConfig

KNOWN_AGENT_ROLES = ("planner", "executor", "reflector")


def normalize_role_configs(value: Any) -> dict[str, AgentRoleConfig]:
    if not isinstance(value, dict):
        return {}
    configs: dict[str, AgentRoleConfig] = {}
    for role, raw in value.items():
        key = str(role).strip().lower()
        if key not in KNOWN_AGENT_ROLES:
            continue
        configs[key] = normalize_role_config(raw)
    return configs


def normalize_role_config(value: Any) -> AgentRoleConfig:
    if isinstance(value, AgentRoleConfig):
        return value
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        return AgentRoleConfig()
    return AgentRoleConfig(
        command=_optional_str(value.get("command")),
        provider=_optional_str(value.get("provider")),
        model=_optional_str(value.get("model")),
        base_url=_optional_str(value.get("base_url")),
        timeout_seconds=_optional_float(value.get("timeout_seconds")),
        max_output_tokens=_optional_int(value.get("max_output_tokens")),
        reasoning_effort=_optional_str(value.get("reasoning_effort")),
        temperature=_optional_float(value.get("temperature")),
        subagent=_optional_str(value.get("subagent")),
    )


def role_config_for(configs: dict[str, AgentRoleConfig], role: str) -> AgentRoleConfig:
    return configs.get(role, AgentRoleConfig())


def add_role_config_args(parser: Any) -> None:
    for role in KNOWN_AGENT_ROLES:
        parser.add_argument(f"--{role}-model", default=None)
        parser.add_argument(f"--{role}-provider", default=None)
        parser.add_argument(f"--{role}-base-url", default=None)
        parser.add_argument(f"--{role}-subagent", default=None)
        parser.add_argument(f"--{role}-reasoning-effort", default=None)
        parser.add_argument(f"--{role}-max-output-tokens", type=int, default=None)
        parser.add_argument(f"--{role}-temperature", type=float, default=None)
        parser.add_argument(f"--{role}-timeout-seconds", type=float, default=None)


def build_role_configs_from_args(args: Any) -> dict[str, AgentRoleConfig]:
    configs: dict[str, AgentRoleConfig] = {}
    for role in KNOWN_AGENT_ROLES:
        config = AgentRoleConfig(
            provider=_optional_str(getattr(args, f"{role}_provider", None)),
            model=_optional_str(getattr(args, f"{role}_model", None)),
            base_url=_optional_str(getattr(args, f"{role}_base_url", None)),
            timeout_seconds=_optional_float(getattr(args, f"{role}_timeout_seconds", None)),
            max_output_tokens=_optional_int(getattr(args, f"{role}_max_output_tokens", None)),
            reasoning_effort=_optional_str(getattr(args, f"{role}_reasoning_effort", None)),
            temperature=_optional_float(getattr(args, f"{role}_temperature", None)),
            subagent=_optional_str(getattr(args, f"{role}_subagent", None)),
        )
        if any(value is not None for value in asdict(config).values()):
            configs[role] = config
    return configs


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
