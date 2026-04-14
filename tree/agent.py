from __future__ import annotations

import abc
from typing import Any

from pathlib import Path

from .command import logs_for as _logs_for, run_json_command
from .prompts import (
    build_agent_telemetry,
    build_execution_payload,
    build_execution_prompt,
    build_reflection_payload,
    build_reflection_prompt,
)
from .task import OptimizationTask
from .types import ExecutionResult, Hypothesis, Insight, RootMemory, SearchNode, TaskSnapshot
from .watchman import WatchmanPacket


class AgentStepError(RuntimeError):
    """Raised when an adapter step fails structurally."""


class AgentAdapter(abc.ABC):
    @abc.abstractmethod
    def propose(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        watchman_packet: WatchmanPacket | None = None,
    ) -> list[Hypothesis]:
        raise NotImplementedError

    @abc.abstractmethod
    def execute(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        raise NotImplementedError

    @abc.abstractmethod
    def reflect(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode | None,
        node: SearchNode,
    ) -> Insight:
        raise NotImplementedError


class NoOpAgentAdapter(AgentAdapter):
    def propose(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        watchman_packet: WatchmanPacket | None = None,
    ) -> list[Hypothesis]:
        if watchman_packet is None:
            return task.seed_hypotheses(parent, root, limit)
        return watchman_packet.branch_cards[:limit]

    def execute(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        return ExecutionResult(
            status="completed",
            summary="No executor command configured; candidate preserved as-is for evaluation.",
            changed=False,
            candidate_path=node.candidate_path,
        )

    def reflect(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode | None,
        node: SearchNode,
    ) -> Insight:
        if node.evaluation_result is None:
            raise AgentStepError("Cannot reflect without an evaluation result.")

        parent_score = None
        if parent is not None and parent.evaluation_result is not None:
            parent_score = parent.evaluation_result.score

        accepted = node.evaluation_result.accepted
        score = node.evaluation_result.score
        summary = node.evaluation_result.summary

        if not accepted:
            return Insight(
                polarity="negative",
                summary=f"Rejected: {summary}",
                evidence=_basic_evidence(node),
                source_node_id=node.id,
            )

        if parent_score is None:
            polarity = "neutral"
            insight_summary = f"Accepted baseline or root candidate at score {score:.3f}."
        elif score > parent_score:
            polarity = "positive"
            insight_summary = (
                f"Accepted child improved score from {parent_score:.3f} to {score:.3f}."
            )
        elif score < parent_score:
            polarity = "negative"
            insight_summary = (
                f"Accepted child regressed relative to parent score {parent_score:.3f}."
            )
        else:
            polarity = "neutral"
            insight_summary = f"Accepted child matched parent score {score:.3f}."

        return Insight(
            polarity=polarity,
            summary=insight_summary,
            evidence=_basic_evidence(node),
            source_node_id=node.id,
        )


class CommandAgentAdapter(AgentAdapter):
    def __init__(
        self,
        repo_root: Path,
        *,
        executor_command: str | None = None,
        reflector_command: str | None = None,
        fallback: AgentAdapter | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.executor_command = executor_command
        self.reflector_command = reflector_command
        self.fallback = fallback or NoOpAgentAdapter()

    def propose(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        watchman_packet: WatchmanPacket | None = None,
    ) -> list[Hypothesis]:
        if watchman_packet is None:
            return self.fallback.propose(task, task_snapshot, root, parent, limit)
        return watchman_packet.branch_cards[:limit]

    def execute(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        if not self.executor_command:
            return self.fallback.execute(task, task_snapshot, root, parent, node)

        payload = build_execution_payload(task_snapshot, root, parent, node)
        prompt = build_execution_prompt(task_snapshot, root, parent, node)
        telemetry = build_agent_telemetry(
            action="execute",
            task_snapshot=task_snapshot,
            root=root,
            parent=parent,
            node=node,
        )
        logs_dir = Path(node.node_dir) / "agent" / "execute"
        raw = self._run_json_command(
            self.executor_command,
            cwd=Path(node.sandbox_path),
            prompt=prompt,
            payload=payload,
            telemetry=telemetry,
            logs_dir=logs_dir,
        )
        if not isinstance(raw, dict):
            raise AgentStepError("Executor output must be a JSON object.")

        return ExecutionResult(
            status=str(raw.get("status", "completed")),
            summary=str(raw.get("summary", "Executor completed.")),
            changed=bool(raw.get("changed", False)),
            candidate_path=str(raw.get("candidate_path", node.candidate_path)),
            raw_log_paths=_logs_for(logs_dir),
            metadata=_dict_value(raw.get("metadata")),
        )

    def reflect(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode | None,
        node: SearchNode,
    ) -> Insight:
        if not self.reflector_command:
            return self.fallback.reflect(task, task_snapshot, root, parent, node)

        payload = build_reflection_payload(task_snapshot, root, parent, node)
        prompt = build_reflection_prompt(task_snapshot, root, parent, node)
        telemetry = build_agent_telemetry(
            action="reflect",
            task_snapshot=task_snapshot,
            root=root,
            parent=parent,
            node=node,
        )
        logs_dir = Path(node.node_dir) / "agent" / "reflect"
        raw = self._run_json_command(
            self.reflector_command,
            cwd=self.repo_root,
            prompt=prompt,
            payload=payload,
            telemetry=telemetry,
            logs_dir=logs_dir,
        )
        if not isinstance(raw, dict):
            raise AgentStepError("Reflector output must be a JSON object.")

        polarity = str(raw.get("polarity", "neutral")).lower()
        if polarity not in {"positive", "negative", "neutral"}:
            raise AgentStepError("Reflector polarity must be positive, negative, or neutral.")

        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list):
            raise AgentStepError("Reflector evidence must be a JSON list.")

        return Insight(
            polarity=polarity,
            summary=str(raw.get("summary", "No insight summary provided.")),
            evidence=[str(item) for item in evidence],
            source_node_id=node.id,
        )

    def _run_json_command(
        self,
        command: str,
        *,
        cwd: Path,
        prompt: str,
        payload: dict[str, Any],
        telemetry: dict[str, Any],
        logs_dir: Path,
    ) -> Any:
        return run_json_command(
            command,
            cwd=cwd,
            prompt=prompt,
            payload=payload,
            telemetry=telemetry,
            logs_dir=logs_dir,
            error_cls=AgentStepError,
        )


def _basic_evidence(node: SearchNode) -> list[str]:
    if node.evaluation_result is None:
        return []

    evidence: list[str] = []
    metrics = node.evaluation_result.metrics
    if "score_metric" in metrics:
        evidence.append(f"score_metric={metrics['score_metric']}")
    if "score_value" in metrics:
        evidence.append(f"score_value={metrics['score_value']}")
    correctness = metrics.get("correctness")
    if correctness is not None:
        evidence.append(f"correctness={correctness}")
    return evidence


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
