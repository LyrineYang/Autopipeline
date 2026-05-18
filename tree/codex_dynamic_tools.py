from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RUN_SENSING_PROBE = "run_sensing_probe"


def sensing_dynamic_tools(sensing) -> list[dict[str, Any]]:
    if sensing is None:
        return []
    return [
        {
            "name": RUN_SENSING_PROBE,
            "description": (
                "Run one diagnostic sensing probe in the current sandbox and return structured "
                "evidence. Use this for quick read/list/grep/command/python/task-specific probes."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["kind"],
                "properties": {
                    "kind": {"type": "string"},
                    "path": {"type": "string"},
                    "mode": {"type": "string"},
                    "lines": {"type": "integer"},
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "code": {"type": "string"},
                    "args": {"type": "array"},
                    "timeout_seconds": {"type": "number"},
                },
                "additionalProperties": True,
            },
        }
    ]


def run_dynamic_tool(
    params: dict[str, Any],
    *,
    sensing,
    logs_dir: Path,
    action: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    tool = str(params.get("tool") or "")
    arguments = _parse_arguments(params.get("arguments"))
    if tool != RUN_SENSING_PROBE or sensing is None:
        response = _text_response(
            success=False,
            payload={"status": "rejected", "summary": "Unsupported dynamic tool."},
        )
    else:
        probe_result = sensing.run_probe_request(action, logs_dir, arguments)
        response = _text_response(
            success=probe_result.get("status") == "completed",
            payload=probe_result,
        )
    activity = {
        "tool": tool,
        "call_id": params.get("callId"),
        "success": bool(response.get("success")),
    }
    return response, activity


def _text_response(*, success: bool, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": success,
        "contentItems": [{"type": "inputText", "text": json.dumps(payload, indent=2)}],
    }


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"kind": "invalid"}
        return parsed if isinstance(parsed, dict) else {"kind": "invalid"}
    return {"kind": "invalid"}
