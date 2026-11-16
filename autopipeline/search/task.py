from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

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
