from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

from .types import RootMemory, SearchNode, TaskSnapshot
from .verifier import build_verifier_summary


@dataclass(slots=True)
class SensingConfig:
    max_probes_per_action: int = 3
    max_read_lines: int = 80
    max_read_bytes: int = 32768
    max_list_entries: int = 40
    max_grep_matches: int = 20


class SensingRuntime:
    def __init__(
        self,
        *,
        base_dir: Path,
        cwd: Path,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode | None = None,
        node: SearchNode | None = None,
        config: SensingConfig | None = None,
        task: Any | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.cwd = cwd
        self.task_snapshot = task_snapshot
        self.root = root
        self.parent = parent
        self.node = node
        self.config = config or SensingConfig()
        self.task = task
        self.sensing_dir = self.base_dir / "sensing"
        self.events_path = self.sensing_dir / "events.jsonl"
        self.probe_history_path = self.sensing_dir / "probe_results.jsonl"
        self._served_requests: dict[str, str] = {}

    def prepare(self, action: str, logs_dir: Path) -> dict[str, str]:
        self.sensing_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        passive_snapshot_path = logs_dir / "passive_snapshot.json"
        probe_requests_path = logs_dir / "probe_requests.json"
        probe_results_path = logs_dir / "probe_results.json"
        manifest_path = logs_dir / "sensing.json"
        recent_probe_results = self._recent_probe_results()
        verifier = build_verifier_summary(
            task_snapshot=self.task_snapshot,
            root=self.root,
            recent_probe_results=recent_probe_results,
            parent=self.parent,
            node=self.node,
            task=self.task,
        )

        passive_snapshot_path.write_text(
            json.dumps(self._passive_snapshot(action), indent=2),
            encoding="utf-8",
        )
        probe_requests_path.write_text("[]", encoding="utf-8")
        probe_results_path.write_text("[]", encoding="utf-8")
        self._served_requests[str(logs_dir.resolve())] = "[]"
        self._write_manifest(
            action=action,
            manifest_path=manifest_path,
            passive_snapshot_path=passive_snapshot_path,
            probe_requests_path=probe_requests_path,
            probe_results_path=probe_results_path,
            recent_probe_results=recent_probe_results,
            verifier=verifier,
            probe_result_count=0,
        )
        return {
            "manifest": str(manifest_path),
            "probe_requests": str(probe_requests_path),
            "probe_results": str(probe_results_path),
        }

    def serve_pending(self, action: str, logs_dir: Path) -> bool:
        probe_requests_path = logs_dir / "probe_requests.json"
        probe_results_path = logs_dir / "probe_results.json"
        manifest_path = logs_dir / "sensing.json"
        passive_snapshot_path = logs_dir / "passive_snapshot.json"
        request_text = probe_requests_path.read_text(encoding="utf-8")
        request_key = str(logs_dir.resolve())
        if request_text == self._served_requests.get(request_key):
            return False

        results = self._execute_probe_requests(request_text)
        self._served_requests[request_key] = request_text
        probe_results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        self._record_probe_results(action, results)
        recent_probe_results = self._recent_probe_results()
        verifier = build_verifier_summary(
            task_snapshot=self.task_snapshot,
            root=self.root,
            recent_probe_results=recent_probe_results,
            parent=self.parent,
            node=self.node,
            task=self.task,
        )
        self._write_manifest(
            action=action,
            manifest_path=manifest_path,
            passive_snapshot_path=passive_snapshot_path,
            probe_requests_path=probe_requests_path,
            probe_results_path=probe_results_path,
            recent_probe_results=recent_probe_results,
            verifier=verifier,
            probe_result_count=len(results),
        )
        return True

    def finalize(self, action: str, logs_dir: Path) -> None:
        self.serve_pending(action, logs_dir)
        self._served_requests.pop(str(logs_dir.resolve()), None)

    def _passive_snapshot(self, action: str) -> dict[str, Any]:
        cwd_entries = []
        try:
            cwd_entries = sorted(path.name for path in self.cwd.iterdir())[:20]
        except OSError:
            cwd_entries = []
        return {
            "timestamp": _now_iso(),
            "action": action,
            "cwd": str(self.cwd),
            "cwd_entries": cwd_entries,
            "task": {
                "type": self.task_snapshot.task_type,
                "objective": self.task_snapshot.objective,
                "target_path": self.task_snapshot.target_path,
            },
            "root": {
                "best_score": self.root.best_score,
                "rounds_completed": self.root.rounds_completed,
            },
            "parent_id": self.parent.id if self.parent is not None else None,
            "node_id": self.node.id if self.node is not None else None,
            "sandbox_path": self.node.sandbox_path if self.node is not None else "",
            "candidate_path": self.node.candidate_path if self.node is not None else "",
            "planner_context": self.node.planner_context if self.node is not None else {},
            "system": {
                "pid": os.getpid(),
                "platform": os.uname().sysname if hasattr(os, "uname") else "unknown",
            },
        }

    def _run_probe(self, index: int, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {
                "index": index,
                "kind": "invalid",
                "status": "rejected",
                "summary": "Probe request must be a JSON object.",
                "diagnostic_only": True,
            }

        kind = str(request.get("kind", "")).strip()
        handler = getattr(self, f"_probe_{kind}", None)
        if callable(handler):
            try:
                payload = handler(request)
                return {
                    "index": index,
                    "kind": kind,
                    "status": "completed",
                    "summary": payload.get("summary", f"{kind} completed"),
                    "data": payload.get("data"),
                    "diagnostic_only": True,
                }
            except Exception as exc:  # pragma: no cover - defensive
                return {
                    "index": index,
                    "kind": kind,
                    "status": "failed",
                    "summary": f"{kind} failed: {exc}",
                    "diagnostic_only": True,
                }

        custom_handler = getattr(self.task, "handle_probe", None)
        if callable(custom_handler):
            try:
                result = custom_handler(request, cwd=self.cwd, node=self.node, parent=self.parent)
                return {
                    "index": index,
                    "kind": kind or "custom",
                    "status": str(result.get("status", "completed")),
                    "summary": str(result.get("summary", "custom probe completed")),
                    "data": result.get("data"),
                    "diagnostic_only": True,
                }
            except Exception as exc:
                return {
                    "index": index,
                    "kind": kind or "custom",
                    "status": "failed",
                    "summary": f"{kind or 'custom'} failed: {exc}",
                    "diagnostic_only": True,
                }

        return {
            "index": index,
            "kind": kind or "unknown",
            "status": "rejected",
            "summary": "Unsupported probe kind.",
            "diagnostic_only": True,
        }

    def _execute_probe_requests(self, request_text: str) -> list[dict[str, Any]]:
        try:
            raw_requests = json.loads(request_text)
        except json.JSONDecodeError:
            return [
                {
                    "index": 0,
                    "kind": "invalid",
                    "status": "failed",
                    "summary": "Probe request file must contain a JSON list.",
                    "diagnostic_only": True,
                }
            ]
        if not isinstance(raw_requests, list):
            return [
                {
                    "index": 0,
                    "kind": "invalid",
                    "status": "failed",
                    "summary": "Probe request file must contain a JSON list.",
                    "diagnostic_only": True,
                }
            ]
        return [
            self._run_probe(index, request)
            for index, request in enumerate(raw_requests[: self.config.max_probes_per_action])
        ]

    def _record_probe_results(self, action: str, results: list[dict[str, Any]]) -> None:
        for result in results:
            self._append_event(
                {
                    "timestamp": _now_iso(),
                    "action": action,
                    "probe": result.get("kind"),
                    "status": result.get("status"),
                    "summary": result.get("summary"),
                }
            )
            self._append_probe_result(
                {
                    "timestamp": _now_iso(),
                    "action": action,
                    **result,
                }
            )

    def _write_manifest(
        self,
        *,
        action: str,
        manifest_path: Path,
        passive_snapshot_path: Path,
        probe_requests_path: Path,
        probe_results_path: Path,
        recent_probe_results: list[dict[str, Any]],
        verifier: dict[str, Any],
        probe_result_count: int,
    ) -> None:
        manifest_path.write_text(
            json.dumps(
                {
                    "action": action,
                    "budgets": {
                        "max_probes_per_action": self.config.max_probes_per_action,
                        "max_read_lines": self.config.max_read_lines,
                        "max_read_bytes": self.config.max_read_bytes,
                        "max_list_entries": self.config.max_list_entries,
                        "max_grep_matches": self.config.max_grep_matches,
                    },
                    "probe_request_model": "agent_defined_requests",
                    "built_in_probe_handlers": [
                        "read_text",
                        "read_json",
                        "list_dir",
                        "grep",
                        "command",
                        "python",
                        "theoretical_limit_inputs",
                    ],
                    "task_probe_handler": callable(getattr(self.task, "handle_probe", None)),
                    "probe_guidance": {
                        "role_split": {
                            "planner_or_executor": "author diagnostic requests and decide what evidence is needed",
                            "sensing_runtime": "execute diagnostic requests safely and return structured results",
                        },
                        "execution_modes": {
                            "delegated_runtime": "use built-in handlers or task-specific probes for quick diagnostics",
                            "planner_authored_experiment": "use the built-in python probe or a task-specific experiment probe for a tiny diagnostic script when static probes are not enough",
                        },
                        "examples": [
                            {"kind": "read_text", "path": "kernel.py", "lines": 20},
                            {"kind": "command", "argv": ["python3", "--version"]},
                            {"kind": "python", "code": "print('hello from generic probe')"},
                            {"kind": "theoretical_limit_inputs"},
                            {"kind": "hardware_profile"},
                            {"kind": "software_audit", "paths": ["kernel.py"]},
                            {"kind": "compile_smoke", "path": "kernel.py"},
                            {"kind": "compile_report", "path": "kernel.py"},
                            {"kind": "gpu_status"},
                            {"kind": "ncu_profile", "metrics": ["sm__throughput.avg.pct_of_peak_sustained_elapsed"]},
                            {"kind": "nsys_profile"},
                            {"kind": "stage_summary"},
                            {"kind": "runtime_breakdown"},
                            {
                                "kind": "python_experiment",
                                "code": "print('hello from probe')",
                            },
                        ],
                        "derived_calculations": {
                            "theoretical_limit_flow": (
                                "request theoretical_limit_inputs first, then use the built-in "
                                "python probe or a task-specific experiment probe to compute "
                                "roofline/headroom values from those stable inputs"
                            ),
                        },
                    },
                    "diagnostic_only": True,
                    "passive_snapshot_path": str(passive_snapshot_path),
                    "probe_requests_path": str(probe_requests_path),
                    "probe_results_path": str(probe_results_path),
                    "probe_result_count": probe_result_count,
                    "recent_events": self._recent_events(),
                    "recent_probe_results": recent_probe_results,
                    "verifier": verifier,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _probe_read_text(self, request: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(request.get("path"))
        text = path.read_text(encoding="utf-8")
        mode = str(request.get("mode", "tail")).lower()
        lines = max(1, min(int(request.get("lines", 20)), self.config.max_read_lines))
        split = text.splitlines()
        selected = split[:lines] if mode == "head" else split[-lines:]
        return {
            "summary": f"Read {len(selected)} lines from {path.name}.",
            "data": {"path": str(path), "lines": selected},
        }

    def _probe_read_json(self, request: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(request.get("path"))
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "summary": f"Read JSON from {path.name}.",
            "data": {"path": str(path), "data": data},
        }

    def _probe_list_dir(self, request: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(request.get("path", "."))
        entries = sorted(child.name for child in path.iterdir())[: self.config.max_list_entries]
        return {
            "summary": f"Listed {len(entries)} entries under {path.name or str(path)}.",
            "data": {"path": str(path), "entries": entries},
        }

    def _probe_grep(self, request: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(request.get("path"))
        pattern = str(request.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("grep probe requires a non-empty pattern")
        regex = re.compile(pattern)
        matches = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if regex.search(line):
                matches.append({"line": line_no, "text": line})
            if len(matches) >= self.config.max_grep_matches:
                break
        return {
            "summary": f"Found {len(matches)} matches in {path.name}.",
            "data": {"path": str(path), "matches": matches},
        }

    def _probe_command(self, request: dict[str, Any]) -> dict[str, Any]:
        raw_argv = request.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv:
            raise ValueError("command probe requires a non-empty argv list")
        argv = [str(item) for item in raw_argv]
        timeout_seconds = _safe_timeout(request.get("timeout_seconds", 30))
        result = subprocess.run(
            argv,
            cwd=str(self.cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "summary": f"command returned {result.returncode}: {' '.join(argv[:4])}",
            "data": {
                "argv": argv,
                "returncode": result.returncode,
                "stdout": _trim_text(result.stdout, self.config.max_read_bytes),
                "stderr": _trim_text(result.stderr, self.config.max_read_bytes),
            },
        }

    def _probe_python(self, request: dict[str, Any]) -> dict[str, Any]:
        code = request.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("python probe requires non-empty code")
        raw_args = request.get("args", [])
        if not isinstance(raw_args, list):
            raise ValueError("python probe args must be a JSON list when present")
        timeout_seconds = _safe_timeout(request.get("timeout_seconds", 30))
        scripts_dir = self.sensing_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="probe-",
            dir=scripts_dir,
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(code)
            script_path = Path(handle.name)
        args = [str(item) for item in raw_args]
        command_builder = getattr(self.task, "build_python_probe_command", None)
        if callable(command_builder):
            command = command_builder(cwd=self.cwd, script_path=script_path, args=args)
        else:
            command = [sys.executable, str(script_path), *args]
        env_builder = getattr(self.task, "probe_environment", None)
        env = env_builder(cwd=self.cwd) if callable(env_builder) else None
        result = subprocess.run(
            command,
            cwd=str(self.cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "summary": f"python probe returned {result.returncode}: {script_path.name}",
            "data": {
                "command": command,
                "script_path": str(script_path),
                "returncode": result.returncode,
                "stdout": _trim_text(result.stdout, self.config.max_read_bytes),
                "stderr": _trim_text(result.stderr, self.config.max_read_bytes),
            },
        }

    def _probe_theoretical_limit_inputs(self, request: dict[str, Any]) -> dict[str, Any]:
        builder = getattr(self.task, "build_theoretical_limit_inputs", None)
        task_inputs = builder(cwd=self.cwd, node=self.node, parent=self.parent) if callable(builder) else {}
        if not isinstance(task_inputs, dict):
            raise ValueError("build_theoretical_limit_inputs must return a JSON object")
        current_node = self.node if self.node is not None else self.parent
        metrics_source = current_node
        if metrics_source is not None and metrics_source.evaluation_result is None:
            metrics_source = self.parent
        metrics = {}
        if metrics_source is not None and metrics_source.evaluation_result is not None:
            metrics = dict(metrics_source.evaluation_result.metrics)
            if metrics_source.evaluation_result.score is not None:
                metrics.setdefault("score_value", metrics_source.evaluation_result.score)
        branch = {}
        if current_node is not None:
            branch = {
                "scope": current_node.hypothesis.scope or current_node.planner_context.get("effective_scope", ""),
                "target": current_node.hypothesis.target or current_node.planner_context.get("focus_target", ""),
                "verification_intent": list(current_node.hypothesis.verification_intent),
            }
        return {
            "summary": "theoretical_limit_inputs returned stable facts for agent-authored bound calculations.",
            "data": {
                "task": {
                    "type": self.task_snapshot.task_type,
                    "objective": self.task_snapshot.objective,
                    "target_path": self.task_snapshot.target_path,
                    "target_kind": self.task_snapshot.target_kind,
                    "surface_type": self.task_snapshot.surface_type,
                    "objective_signals": self.task_snapshot.objective_signals,
                    "metadata": self.task_snapshot.metadata,
                },
                "root": {
                    "baseline_score": self.root.baseline_score,
                    "best_score": self.root.best_score,
                },
                "branch": branch,
                "current_metrics": metrics,
                "task_inputs": task_inputs,
                "agent_next_step": {
                    "recommended_probe": "python",
                    "goal": (
                        "compute a theoretical upper bound or headroom estimate from the "
                        "stable inputs above; keep the calculation script inside the probe request"
                    ),
                    "recommended_outputs": [
                        "peak_compute_tflops",
                        "peak_bandwidth_gbps",
                        "arithmetic_intensity",
                        "roofline_ceiling_tflops",
                        "achieved_tflops",
                        "efficiency_vs_ceiling",
                        "headroom_ratio",
                        "bound_type",
                    ],
                },
            },
        }

    def _resolve_path(self, raw: Any) -> Path:
        path = Path(str(raw or "")).expanduser()
        if not path.is_absolute():
            path = (self.cwd / path).resolve()
        else:
            path = path.resolve()
        cwd_resolved = self.cwd.resolve()
        if cwd_resolved not in path.parents and path != cwd_resolved:
            raise ValueError("Probe path must stay inside the current working directory.")
        return path

    def _recent_events(self, limit: int = 8) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        try:
            rows = [
                json.loads(line)
                for line in self.events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError:
            return []
        return rows[-limit:]

    def _append_event(self, payload: dict[str, Any]) -> None:
        self.sensing_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _append_probe_result(self, payload: dict[str, Any]) -> None:
        self.sensing_dir.mkdir(parents=True, exist_ok=True)
        with self.probe_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _recent_probe_results(self, limit: int = 5) -> list[dict[str, Any]]:
        if not self.probe_history_path.exists():
            return []
        try:
            rows = [
                json.loads(line)
                for line in self.probe_history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError:
            return []
        return rows[-limit:]

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _safe_timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return 30
    return max(1, timeout)
