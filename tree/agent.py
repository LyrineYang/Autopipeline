from __future__ import annotations

import abc
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .prompts import (
    build_execution_payload,
    build_execution_prompt,
    build_proposal_payload,
    build_proposal_prompt,
    build_reflection_payload,
    build_reflection_prompt,
)
from .task import OptimizationTask
from .types import ExecutionResult, Hypothesis, Insight, RootMemory, SearchNode, TaskSnapshot
from .watchman import Watchman, WatchmanPacket


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
    def __init__(self, *, watchman: Watchman | None = None) -> None:
        self.watchman = watchman or Watchman()

    def propose(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        watchman_packet: WatchmanPacket | None = None,
    ) -> list[Hypothesis]:
        packet = watchman_packet or self.watchman.build_packet(task, task_snapshot, root, parent, limit)
        return packet.branch_cards[:limit]

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
        planner_command: str | None = None,
        executor_command: str | None = None,
        reflector_command: str | None = None,
        fallback: AgentAdapter | None = None,
        watchman: Watchman | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.planner_command = planner_command
        self.executor_command = executor_command
        self.reflector_command = reflector_command
        self.fallback = fallback or NoOpAgentAdapter()
        self.watchman = watchman or Watchman()

    def propose(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        watchman_packet: WatchmanPacket | None = None,
    ) -> list[Hypothesis]:
        watchman_packet = watchman_packet or self.watchman.build_packet(task, task_snapshot, root, parent, limit)
        if not self.planner_command:
            return watchman_packet.branch_cards[:limit]

        payload = build_proposal_payload(task_snapshot, root, parent, limit, watchman_packet)
        prompt = build_proposal_prompt(task_snapshot, root, parent, limit, watchman_packet)
        logs_dir = Path(parent.node_dir) / "agent" / "propose"
        raw = self._run_json_command(
            self.planner_command,
            cwd=self.repo_root,
            prompt=prompt,
            payload=payload,
            logs_dir=logs_dir,
        )
        hypotheses_raw = raw if isinstance(raw, list) else raw.get("hypotheses")
        if not isinstance(hypotheses_raw, list):
            raise AgentStepError("Planner output must be a JSON list or an object with 'hypotheses'.")

        hypotheses: list[Hypothesis] = []
        for index, item in enumerate(hypotheses_raw[:limit]):
            default_card = _match_watchman_card(item, index, watchman_packet.branch_cards)
            hypotheses.append(_normalize_hypothesis(item, index, default=default_card))
        return hypotheses

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
        logs_dir = Path(node.node_dir) / "agent" / "execute"
        raw = self._run_json_command(
            self.executor_command,
            cwd=Path(node.sandbox_path),
            prompt=prompt,
            payload=payload,
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
        logs_dir = Path(node.node_dir) / "agent" / "reflect"
        raw = self._run_json_command(
            self.reflector_command,
            cwd=self.repo_root,
            prompt=prompt,
            payload=payload,
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
        logs_dir: Path,
    ) -> Any:
        logs_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = logs_dir / "prompt.txt"
        payload_path = logs_dir / "payload.json"
        output_path = logs_dir / "output.json"
        stdout_path = logs_dir / "stdout.log"
        stderr_path = logs_dir / "stderr.log"

        prompt_path.write_text(prompt, encoding="utf-8")
        payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["TREE_PROMPT_PATH"] = str(prompt_path)
        env["TREE_PAYLOAD_PATH"] = str(payload_path)
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
            raise AgentStepError(
                f"Agent command failed with return code {result.returncode}: {command}",
            )
        if not output_path.exists():
            raise AgentStepError(f"Agent command did not write {output_path}.")

        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentStepError(f"Agent command wrote invalid JSON: {exc}") from exc


def _normalize_hypothesis(
    item: Any,
    index: int,
    *,
    default: Hypothesis | None = None,
) -> Hypothesis:
    if isinstance(item, str):
        return Hypothesis(
            id=default.id if default is not None else f"hypothesis-{index + 1}",
            title=default.title if default is not None else f"Hypothesis {index + 1}",
            core_hypothesis=item.strip(),
            constraints_summary=default.constraints_summary if default is not None else "",
            editable_path_hints=list(default.editable_path_hints) if default is not None else [],
            family=default.family if default is not None else "unspecified",
            rationale=default.rationale if default is not None else "",
            target_signals=list(default.target_signals) if default is not None else [],
            supporting_evidence=list(default.supporting_evidence) if default is not None else [],
            disallowed_directions=list(default.disallowed_directions) if default is not None else [],
        )

    if not isinstance(item, dict):
        raise AgentStepError("Each hypothesis must be a JSON object or string.")

    hints = item.get("editable_path_hints")
    if hints is not None and not isinstance(hints, list):
        raise AgentStepError("editable_path_hints must be a JSON list when present.")

    return Hypothesis(
        id=str(item.get("id", default.id if default is not None else f"hypothesis-{index + 1}")),
        title=str(item.get("title", default.title if default is not None else f"Hypothesis {index + 1}")),
        core_hypothesis=str(
            item.get("core_hypothesis", default.core_hypothesis if default is not None else "")
        ).strip(),
        constraints_summary=str(
            item.get(
                "constraints_summary",
                default.constraints_summary if default is not None else "",
            )
        ),
        editable_path_hints=(
            [str(hint) for hint in hints]
            if hints is not None
            else list(default.editable_path_hints) if default is not None else []
        ),
        family=str(item.get("family", default.family if default is not None else "unspecified")),
        rationale=str(item.get("rationale", default.rationale if default is not None else "")),
        target_signals=_string_list_or_default(item, "target_signals", default.target_signals if default is not None else []),
        supporting_evidence=_string_list_or_default(item, "supporting_evidence", default.supporting_evidence if default is not None else []),
        disallowed_directions=_string_list_or_default(item, "disallowed_directions", default.disallowed_directions if default is not None else []),
    )


def _match_watchman_card(
    item: Any,
    index: int,
    branch_cards: list[Hypothesis],
) -> Hypothesis | None:
    if index >= len(branch_cards):
        return None
    fallback = branch_cards[index]
    if not isinstance(item, dict):
        return fallback

    family = str(item.get("family", "")).strip()
    if family:
        for card in branch_cards:
            if card.family == family:
                return card

    identifier = str(item.get("id", "")).strip()
    if identifier:
        for card in branch_cards:
            if card.id == identifier:
                return card

    title = str(item.get("title", "")).strip()
    if title:
        for card in branch_cards:
            if card.title == title:
                return card

    return fallback


def _string_list_or_default(item: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = item.get(key)
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(entry) for entry in value]
    return list(default)


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


def _logs_for(logs_dir: Path) -> dict[str, str]:
    return {
        "prompt": str(logs_dir / "prompt.txt"),
        "payload": str(logs_dir / "payload.json"),
        "output": str(logs_dir / "output.json"),
        "stdout": str(logs_dir / "stdout.log"),
        "stderr": str(logs_dir / "stderr.log"),
    }
