from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def run_json_command(
    command: str,
    *,
    cwd: Path,
    prompt: str,
    payload: dict[str, Any],
    telemetry: dict[str, Any],
    logs_dir: Path,
    error_cls: type[Exception] = RuntimeError,
) -> Any:
    logs_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = logs_dir / "prompt.txt"
    payload_path = logs_dir / "payload.json"
    telemetry_path = logs_dir / "telemetry.json"
    output_path = logs_dir / "output.json"
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"

    prompt_path.write_text(prompt, encoding="utf-8")
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    telemetry_path.write_text(json.dumps(telemetry, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env["TREE_PROMPT_PATH"] = str(prompt_path)
    env["TREE_PAYLOAD_PATH"] = str(payload_path)
    env["TREE_TELEMETRY_PATH"] = str(telemetry_path)
    env["TREE_OUTPUT_PATH"] = str(output_path)

    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        shell=True,
        text=True,
        capture_output=True,
    )
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")

    if result.returncode != 0:
        raise error_cls(
            f"Agent command failed with return code {result.returncode}: {command}",
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
    }
