from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Protocol

from .types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot

SUPPORTED_SANDBOX_BACKENDS = {"copy"}


def validate_sandbox_backend(name: str) -> None:
    if name not in SUPPORTED_SANDBOX_BACKENDS:
        raise ValueError(
            f"Unsupported sandbox backend '{name}'. Supported backends: {sorted(SUPPORTED_SANDBOX_BACKENDS)}",
        )


def create_copy_sandbox(
    source_candidate: Path,
    sandbox_dir: Path,
    *,
    candidate_name: str = "kernel.py",
) -> tuple[str, str]:
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = sandbox_dir / candidate_name
    shutil.copy2(source_candidate, candidate_path)
    return str(sandbox_dir), str(candidate_path)


def checkpoint_candidate(task: object, node: SearchNode, checkpoint_path: Path) -> None:
    writer = getattr(task, "checkpoint_candidate", None)
    if callable(writer):
        writer(node, checkpoint_path)
        return
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(node.candidate_path), checkpoint_path)


def restore_candidate(task: object, node: SearchNode, checkpoint_path: Path) -> None:
    reader = getattr(task, "restore_candidate", None)
    if callable(reader):
        reader(node, checkpoint_path)
        return
    shutil.copy2(checkpoint_path, Path(node.candidate_path))


def candidate_checkpoint_path(node: SearchNode, stem: str) -> Path:
    candidate = Path(node.candidate_path)
    suffix = candidate.suffix or ".artifact"
    return Path(node.node_dir) / f"{stem}{suffix}"


class OptimizationTask(Protocol):
    def snapshot(self) -> TaskSnapshot: ...

    def prepare_run(self, run_dir: Path) -> None: ...

    def create_sandbox(
        self,
        node: SearchNode,
        parent: SearchNode | None,
    ) -> None: ...

    def evaluate(self, node: SearchNode) -> EvaluationResult: ...

    def promote(self, node: SearchNode, root: RootMemory) -> str | None: ...

    def final_verify(self, root: RootMemory) -> dict | None: ...

    def seed_hypotheses(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
    ) -> list[Hypothesis]: ...

    def planner_context(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
        *,
        upper_bounds: dict[str, int] | None = None,
    ) -> dict[str, Any]: ...

    def prepare_execution_surface(
        self,
        node: SearchNode,
        parent: SearchNode | None,
    ) -> None: ...

    def handle_probe(
        self,
        request: dict[str, Any],
        *,
        cwd: Path,
        node: SearchNode | None,
        parent: SearchNode | None,
    ) -> dict[str, Any]: ...

    def checkpoint_candidate(self, node: SearchNode, checkpoint_path: Path) -> None: ...

    def restore_candidate(self, node: SearchNode, checkpoint_path: Path) -> None: ...

    def build_python_probe_command(
        self,
        *,
        cwd: Path,
        script_path: Path,
        args: list[str],
    ) -> list[str]: ...

    def probe_environment(self, *, cwd: Path) -> dict[str, str]: ...

    def build_theoretical_limit_inputs(
        self,
        *,
        cwd: Path,
        node: SearchNode | None,
        parent: SearchNode | None,
    ) -> dict[str, Any]: ...

    def augment_verifier_summary(
        self,
        *,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        recent_probe_results: list[dict[str, Any]],
        parent: SearchNode | None,
        node: SearchNode | None,
    ) -> dict[str, Any]: ...
