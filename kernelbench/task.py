from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tree.task import OptimizationTask
from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot
from tree.prior import kernel_optimization_prior, kernel_prior_family_ids

from .bridge import KernelBenchProblem


@dataclass(slots=True)
class KernelBenchSurface:
    name: str
    uid: str
    level: int | str
    problem_id: int | str
    metadata: dict[str, Any]
    reference_source: str | None = None
    starter_source: str | None = None
    kernel_file: Path | None = None
    active_dir: Path | None = None


class KernelBenchTask(OptimizationTask):
    """Thin tree adapter for one KernelBench problem surface."""

    def __init__(
        self,
        *,
        repo_root: Path,
        surface: KernelBenchSurface,
        bench_args: list[str] | None = None,
        run_name: str | None = None,
        timeout_seconds: float | None = 300,
        bench_script_path: Path | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.surface = surface
        self.bench_args = list(bench_args or [])
        self.run_name = run_name or str(surface.uid)
        self.timeout_seconds = timeout_seconds
        self.bench_script_path = bench_script_path.resolve() if bench_script_path is not None else None
        self.run_dir: Path | None = None
        self._last_promoted_path: Path | None = None

    @classmethod
    def from_problem(
        cls,
        *,
        repo_root: Path,
        problem: KernelBenchProblem,
        backend: str = "cuda",
        bench_args: list[str] | None = None,
        run_name: str | None = None,
        timeout_seconds: float | None = 300,
        bench_script_path: Path | None = None,
    ) -> "KernelBenchTask":
        analysis = problem.analyze()
        metadata = {
            "level": problem.level,
            "problem_id": problem.problem_id,
            "name": problem.name,
            "uid": problem.uid,
            "analysis": analysis,
            "backend": backend,
        }
        surface = KernelBenchSurface(
            name=problem.name,
            uid=problem.uid,
            level=problem.level,
            problem_id=problem.problem_id,
            metadata=metadata,
            reference_source=problem.source_code,
            starter_source=problem.generate_starter(backend=backend),
        )
        return cls(
            repo_root=repo_root,
            surface=surface,
            bench_args=bench_args,
            run_name=run_name,
            timeout_seconds=timeout_seconds,
            bench_script_path=bench_script_path,
        )

    @classmethod
    def from_active_workspace(
        cls,
        *,
        repo_root: Path,
        kernel_file: Path | None = None,
        active_dir: Path | None = None,
        bench_args: list[str] | None = None,
        run_name: str | None = None,
        timeout_seconds: float | None = 300,
        bench_script_path: Path | None = None,
    ) -> "KernelBenchTask":
        repo = repo_root.resolve()
        kernel_path = (kernel_file or repo / "kernel.py").resolve()
        kb_active = (active_dir or repo / "workspace" / "kb_active").resolve()
        metadata_path = kb_active / "metadata.json"
        metadata = _read_json(metadata_path)
        uid = str(metadata.get("uid") or kernel_path.stem)
        surface = KernelBenchSurface(
            name=str(metadata.get("name") or uid),
            uid=uid,
            level=metadata.get("level", "?"),
            problem_id=metadata.get("problem_id", "?"),
            metadata=metadata,
            kernel_file=kernel_path,
            active_dir=kb_active,
        )
        return cls(
            repo_root=repo,
            surface=surface,
            bench_args=bench_args,
            run_name=run_name,
            timeout_seconds=timeout_seconds,
            bench_script_path=bench_script_path,
        )

    def snapshot(self) -> TaskSnapshot:
        analysis = _dict_value(self.surface.metadata.get("analysis"))
        operations = ", ".join(str(item) for item in analysis.get("operations", []) if item)
        objective = (
            f"Optimize KernelBench {self.surface.uid} correctness and speedup"
            + (f" for operations: {operations}." if operations else ".")
        )
        return TaskSnapshot(
            task_type="kernelbench",
            objective=objective,
            target_path="kernel.py",
            editable_paths=["kernel.py"],
            constraints=[
                "Only edit kernel.py inside the node sandbox.",
                "Preserve the KernelBench ModelNew interface and input/output semantics.",
                "Treat reference.py, metadata.json, and benchmark output as read-only evidence.",
            ],
            reference_paths=["workspace/kb_active/reference.py", "workspace/kb_active/metadata.json"],
            target_kind="kernelbench_model",
            surface_type="workspace",
            objective_signals=[
                "correctness",
                "speedup",
                "kernel_time_ms",
                "reference_time_ms",
                "fast_1.0",
                "fast_1.5",
            ],
            metadata={
                "benchmark": "KernelBench",
                "uid": self.surface.uid,
                "level": self.surface.level,
                "problem_id": self.surface.problem_id,
                "name": self.surface.name,
            },
        )

    def prepare_run(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def create_sandbox(self, node: SearchNode, parent: SearchNode | None) -> None:
        sandbox_dir = Path(node.node_dir) / "sandbox"
        if sandbox_dir.exists():
            shutil.rmtree(sandbox_dir)
        if parent is not None and parent.sandbox_path:
            shutil.copytree(parent.sandbox_path, sandbox_dir, ignore=_sandbox_copy_ignore)
            results_path = sandbox_dir / "workspace" / "kb_active" / "results.json"
            if results_path.exists():
                results_path.unlink()
        else:
            self._write_baseline_sandbox(sandbox_dir)
        node.sandbox_path = str(sandbox_dir)
        node.candidate_path = str(sandbox_dir / "kernel.py")

    def evaluate(self, node: SearchNode) -> EvaluationResult:
        sandbox = Path(node.sandbox_path)
        node_dir = Path(node.node_dir)
        stdout_path = node_dir / "kb_bench.stdout.log"
        stderr_path = node_dir / "kb_bench.stderr.log"
        command = [
            sys.executable,
            "kernelbench/bench_kb.py",
            *self.bench_args,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(sandbox),
                env=self._bench_env(node_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            returncode = -1
            stderr = (stderr + "\n" if stderr else "") + f"KernelBench evaluation timed out after {self.timeout_seconds} seconds."
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")

        metrics = parse_kernelbench_output(stdout + ("\n" + stderr if stderr else ""))
        metrics["returncode"] = returncode
        metrics["score_metric"] = "speedup"
        score = float(metrics.get("speedup") or 0.0)
        metrics["score_value"] = score
        accepted = returncode == 0 and str(metrics.get("correctness", "")).upper() == "PASS"
        return EvaluationResult(
            accepted=accepted,
            score=score,
            metrics=metrics,
            summary=_kernelbench_summary(metrics, accepted),
            raw_log_paths={
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "results": str(sandbox / "workspace" / "kb_active" / "results.json"),
            },
        )

    def promote(self, node: SearchNode, root: RootMemory) -> str | None:
        if self.run_dir is None:
            return None
        promoted_path = self.run_dir / "promoted_kernelbench"
        if promoted_path.exists():
            shutil.rmtree(promoted_path)
        shutil.copytree(Path(node.sandbox_path), promoted_path, ignore=_sandbox_copy_ignore)
        self._last_promoted_path = promoted_path
        return str(promoted_path)

    def final_verify(self, root: RootMemory) -> dict | None:
        if self._last_promoted_path is None:
            return None
        command = [
            sys.executable,
            "kernelbench/bench_kb.py",
            *self.bench_args,
        ]
        stdout_path = self._last_promoted_path / "final_verify.stdout.log"
        stderr_path = self._last_promoted_path / "final_verify.stderr.log"
        try:
            result = subprocess.run(
                command,
                cwd=str(self._last_promoted_path),
                env=self._bench_env(self._last_promoted_path),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            returncode = -1
            stderr = (stderr + "\n" if stderr else "") + f"KernelBench final verify timed out after {self.timeout_seconds} seconds."
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        metrics = parse_kernelbench_output(stdout + ("\n" + stderr if stderr else ""))
        metrics["returncode"] = returncode
        accepted = returncode == 0 and str(metrics.get("correctness", "")).upper() == "PASS"
        return {
            "accepted": accepted,
            "summary": _kernelbench_summary(metrics, accepted),
            "metrics": metrics,
            "raw_log_paths": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
        }

    def seed_hypotheses(self, parent: SearchNode, root: RootMemory, limit: int) -> list[Hypothesis]:
        analysis = _dict_value(self.surface.metadata.get("analysis"))
        operations = [str(item) for item in analysis.get("operations", []) if item]
        op_summary = ", ".join(operations[:4]) or "the current operation"
        candidates = [
            Hypothesis(
                id="memory-access-coalescing",
                title="Improve memory access",
                core_hypothesis="Improve data movement and access locality while preserving ModelNew outputs.",
                idea_id="memory-access",
                implementation_variant="coalescing-or-staging",
                scope="kernelbench",
                target="kernel.py",
                editable_path_hints=["kernel.py"],
                family="memory_access",
                verification_intent=["correctness", "speedup"],
            ),
            Hypothesis(
                id="parallelism-occupancy",
                title="Improve parallelism",
                core_hypothesis=f"Improve work distribution or launch shape for {op_summary}.",
                idea_id="parallelism-occupancy",
                implementation_variant="launch-or-tiling-adjustment",
                scope="kernelbench",
                target="kernel.py",
                editable_path_hints=["kernel.py"],
                family="parallelism_occupancy",
                verification_intent=["correctness", "speedup"],
            ),
            Hypothesis(
                id="compute-instruction",
                title="Reduce compute instructions",
                core_hypothesis=f"Reduce arithmetic or indexing instruction overhead in the {op_summary} path.",
                idea_id="compute-instruction",
                implementation_variant="instruction-or-index-simplification",
                scope="kernelbench",
                target="kernel.py",
                editable_path_hints=["kernel.py"],
                family="compute_instruction",
                verification_intent=["correctness", "speedup"],
            ),
            Hypothesis(
                id="precision-datatype",
                title="Adjust precision",
                core_hypothesis="Use cheaper datatypes only where KernelBench tolerance and semantics allow it.",
                idea_id="precision-datatype",
                implementation_variant="safe-lower-precision-path",
                scope="kernelbench",
                target="kernel.py",
                editable_path_hints=["kernel.py"],
                family="precision_datatype",
                verification_intent=["correctness", "speedup"],
            ),
        ]
        return candidates[:limit]

    def planner_context(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
        *,
        upper_bounds: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "task_kind": "kernelbench",
            "benchmark": "KernelBench",
            "problem": {
                "uid": self.surface.uid,
                "level": self.surface.level,
                "problem_id": self.surface.problem_id,
                "name": self.surface.name,
            },
            "analysis": _dict_value(self.surface.metadata.get("analysis")),
            "optimization_prior": kernel_optimization_prior(),
            "editable_paths": ["kernel.py"],
            "objective_signals": self.snapshot().objective_signals,
            "planner_rules": {
                "allowed_scopes": ["kernelbench"],
                "allowed_families": kernel_prior_family_ids(),
                "editable_paths": ["kernel.py"],
            },
            "agent_role": "modify only sandbox/kernel.py; benchmark evidence comes from kernelbench/bench_kb.py",
        }

    def prepare_execution_surface(self, node: SearchNode, parent: SearchNode | None) -> None:
        sandbox = Path(node.sandbox_path)
        context_path = sandbox / "kernelbench_context.json"
        context_path.write_text(
            json.dumps(
                {
                    "problem": {
                        "uid": self.surface.uid,
                        "level": self.surface.level,
                        "problem_id": self.surface.problem_id,
                        "name": self.surface.name,
                    },
                    "metadata": self.surface.metadata,
                    "editable_paths": ["kernel.py"],
                    "benchmark_command": ["python", "kernelbench/bench_kb.py", *self.bench_args],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        node.planner_context.setdefault("effective_scope", "kernelbench")
        node.planner_context["effective_editable_paths"] = ["kernel.py"]
        node.planner_context["context_files"] = ["kernelbench_context.json"]

    def checkpoint_candidate(self, node: SearchNode, checkpoint_path: Path) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(node.candidate_path), checkpoint_path)

    def restore_candidate(self, node: SearchNode, checkpoint_path: Path) -> None:
        shutil.copy2(checkpoint_path, Path(node.candidate_path))

    def _write_baseline_sandbox(self, sandbox_dir: Path) -> None:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        kernelbench_dir = sandbox_dir / "kernelbench"
        kernelbench_dir.mkdir(parents=True, exist_ok=True)
        bench_script = self.bench_script_path or self.repo_root / "kernelbench" / "bench_kb.py"
        shutil.copy2(bench_script, kernelbench_dir / "bench_kb.py")
        init_path = self.repo_root / "kernelbench" / "__init__.py"
        if init_path.exists():
            shutil.copy2(init_path, kernelbench_dir / "__init__.py")
        kernels_source = self.repo_root / "kernels"
        if kernels_source.exists():
            shutil.copytree(kernels_source, sandbox_dir / "kernels", ignore=_sandbox_copy_ignore)

        active_dir = sandbox_dir / "workspace" / "kb_active"
        active_dir.mkdir(parents=True, exist_ok=True)
        if self.surface.reference_source is not None and self.surface.starter_source is not None:
            (active_dir / "reference.py").write_text(self.surface.reference_source, encoding="utf-8")
            (active_dir / "metadata.json").write_text(
                json.dumps(self.surface.metadata, indent=2),
                encoding="utf-8",
            )
            (sandbox_dir / "kernel.py").write_text(self.surface.starter_source, encoding="utf-8")
            return

        if self.surface.active_dir is None or self.surface.kernel_file is None:
            raise RuntimeError("KernelBench active workspace is incomplete.")
        shutil.copy2(self.surface.active_dir / "reference.py", active_dir / "reference.py")
        metadata_path = self.surface.active_dir / "metadata.json"
        if metadata_path.exists():
            shutil.copy2(metadata_path, active_dir / "metadata.json")
        else:
            (active_dir / "metadata.json").write_text(json.dumps(self.surface.metadata, indent=2), encoding="utf-8")
        shutil.copy2(self.surface.kernel_file, sandbox_dir / "kernel.py")

    def _bench_env(self, node_dir: Path) -> dict[str, str]:
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        pythonpath = str(self.repo_root) if not existing_pythonpath else f"{self.repo_root}{os.pathsep}{existing_pythonpath}"
        return {
            **os.environ,
            "PYTHONPATH": pythonpath,
        }


def parse_kernelbench_output(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if key in {"correctness", "problem", "name", "stability", "determinism"}:
            metrics[key] = value
        elif key.startswith("fast_"):
            metrics[key] = value.upper() == "PASS"
        elif key in {
            "speedup",
            "kernel_time_ms",
            "reference_time_ms",
            "peak_vram_mb",
            "worst_max_abs_error",
        }:
            metrics[key] = _optional_float(value.replace("x", ""))
    return metrics


def default_kernelbench_tree_run_dir(repo_root: Path, name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "kernelbench"
    return repo_root / "workspace" / "kernelbench_tree_runs" / f"{stamp}-{safe_name}"


def _kernelbench_summary(metrics: dict[str, Any], accepted: bool) -> str:
    correctness = metrics.get("correctness", "UNKNOWN")
    speedup = float(metrics.get("speedup") or 0.0)
    kernel_time = float(metrics.get("kernel_time_ms") or 0.0)
    reference_time = float(metrics.get("reference_time_ms") or 0.0)
    return (
        f"accepted={accepted} correctness={correctness} speedup={speedup:.3f}x "
        f"kernel_time_ms={kernel_time:.4f} reference_time_ms={reference_time:.4f}"
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sandbox_copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".triton",
    }
    return {name for name in names if name in ignored or name.endswith((".pyc", ".so"))}
