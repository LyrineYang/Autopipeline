#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Codex CLI wrapper for the tree framework command contract.",
    )
    parser.add_argument("role", choices=["execute"], help="Tree command role to run.")
    parser.add_argument("--model", default=os.environ.get("CODEX_TREE_MODEL") or os.environ.get("TREE_AGENT_MODEL"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN"))
    parser.add_argument(
        "--approval",
        default=os.environ.get("CODEX_TREE_APPROVAL", "never"),
        choices=["never", "on-request", "untrusted"],
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=_truthy(os.environ.get("CODEX_TREE_DRY_RUN")),
        help="Do not launch Codex; only write a successful no-change result.",
    )
    args = parser.parse_args()
    if args.role == "execute":
        run_execute(args)


def run_execute(args: argparse.Namespace) -> None:
    env_paths = _env_paths()
    cwd = Path.cwd().resolve()
    output_path = env_paths["output"]
    watched_paths = _watched_paths_from_payload(cwd, env_paths["payload"])
    before = _workspace_fingerprint(cwd, watched_paths=watched_paths)
    if args.dry_run:
        _write_output(
            output_path,
            {
                "status": "completed",
                "summary": "Codex wrapper dry-run completed without changing the sandbox.",
                "changed": False,
                "candidate_path": str(cwd / "kernel.py") if (cwd / "kernel.py").exists() else "",
                "metadata": {"codex_wrapper": "dry_run"},
            },
        )
        return

    codex_bin = _resolve_codex_bin(args.codex_bin)
    if not codex_bin:
        _write_output(
            output_path,
            {
                "status": "failed",
                "summary": "Codex CLI was not found. Set CODEX_BIN or install codex.",
                "changed": False,
                "candidate_path": str(cwd / "kernel.py") if (cwd / "kernel.py").exists() else "",
            },
        )
        return

    command = _build_codex_command(
        codex_bin=codex_bin,
        cwd=cwd,
        approval=args.approval,
        model=args.model,
    )

    child_env = os.environ.copy()
    for key in list(child_env):
        if key.startswith("TREE_"):
            child_env.pop(key, None)
    prompt = _build_codex_prompt(env_paths=env_paths, cwd=cwd)
    result = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        input=prompt,
        capture_output=True,
        env=child_env,
    )
    after = _workspace_fingerprint(cwd, watched_paths=watched_paths)
    changed_paths = sorted(path for path, digest in after.items() if before.get(path) != digest)
    deleted_paths = sorted(path for path in before if path not in after)
    changed = bool(changed_paths or deleted_paths)

    if result.returncode != 0:
        _write_output(
            output_path,
            {
                "status": "failed",
                "summary": f"Codex CLI failed with return code {result.returncode}.",
                "changed": changed,
                "candidate_path": str(cwd / "kernel.py") if (cwd / "kernel.py").exists() else "",
                "metadata": {
                    "changed_paths": changed_paths[:20],
                    "deleted_paths": deleted_paths[:20],
                    "stdout_tail": result.stdout[-2000:],
                    "stderr_tail": result.stderr[-2000:],
                },
            },
        )
        return

    _write_output(
        output_path,
        {
            "status": "completed",
            "summary": (
                "Codex CLI completed with sandbox changes."
                if changed
                else "Codex CLI completed without modifying tracked sandbox files."
            ),
            "changed": changed,
            "candidate_path": str(cwd / "kernel.py") if (cwd / "kernel.py").exists() else "",
            "metadata": {
                "changed_paths": changed_paths[:20],
                "deleted_paths": deleted_paths[:20],
                "stdout_tail": result.stdout[-2000:],
            },
        },
    )


def _build_codex_prompt(*, env_paths: dict[str, Path], cwd: Path) -> str:
    prompt = _read_text(env_paths["prompt"])
    payload = _read_text(env_paths["payload"])
    sensing = _read_text(env_paths.get("sensing"))
    probe_requests = env_paths.get("probe_requests")
    probe_results = env_paths.get("probe_results")
    return f"""You are the executor for one tree-search branch.

Work only inside this sandbox:
{cwd}

Read the task prompt, structured payload, and sensing manifest below. Make one focused code change inside the sandbox if it is likely to improve the candidate. Do not edit files outside the sandbox. Do not write TREE_OUTPUT_PATH; the wrapper writes it after you finish.

If you need diagnostics, run local commands in the sandbox. Keep the change small and leave the benchmark harness files untouched unless the payload explicitly allows otherwise.

TREE_PROMPT:
{prompt}

TREE_PAYLOAD_JSON:
```json
{payload}
```

TREE_SENSING_JSON:
```json
{sensing}
```

Probe request path, if needed by a future wrapper:
{probe_requests or ""}

Probe result path, if needed by a future wrapper:
{probe_results or ""}
"""


def _env_paths() -> dict[str, Path]:
    required = {
        "prompt": "TREE_PROMPT_PATH",
        "payload": "TREE_PAYLOAD_PATH",
        "output": "TREE_OUTPUT_PATH",
    }
    paths: dict[str, Path] = {}
    for name, env_key in required.items():
        value = os.environ.get(env_key)
        if not value:
            raise SystemExit(f"{env_key} is required")
        paths[name] = Path(value)
    optional = {
        "sensing": "TREE_SENSING_PATH",
        "probe_requests": "TREE_PROBE_REQUESTS_PATH",
        "probe_results": "TREE_PROBE_RESULTS_PATH",
    }
    for name, env_key in optional.items():
        value = os.environ.get(env_key)
        if value:
            paths[name] = Path(value)
    return paths


def _watched_paths_from_payload(root: Path, payload_path: Path) -> list[Path] | None:
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

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
    rel_parts = path.relative_to(root).parts
    ignored_dirs = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".triton",
    }
    if any(part in ignored_dirs for part in rel_parts):
        return True
    return path.suffix in {".pyc", ".so", ".o", ".log"}


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _default_codex_bin() -> str | None:
    candidate = Path("/Applications/Codex.app/Contents/Resources/codex")
    return str(candidate) if candidate.exists() else None


def _resolve_codex_bin(configured: str | None) -> str | None:
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        found = shutil.which(configured)
        if found:
            return found
        return None
    return shutil.which("codex") or _default_codex_bin()


def _build_codex_command(
    *,
    codex_bin: str,
    cwd: Path,
    approval: str,
    model: str | None,
) -> list[str]:
    command = [codex_bin]
    if model:
        command.extend(["--model", model])
    command.extend(
        [
            "--ask-for-approval",
            approval,
            "exec",
            "-C",
            str(cwd),
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-",
        ]
    )
    return command


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()
