from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .agent import AgentAdapter, AgentStepError
from .task import OptimizationTask, validate_sandbox_backend
from .types import (
    ExecutionResult,
    Hypothesis,
    RootMemory,
    SearchConfig,
    SearchNode,
    SearchResult,
)


class TreeSearchEngine:
    def __init__(
        self,
        *,
        task: OptimizationTask,
        agent: AgentAdapter,
        config: SearchConfig,
        run_dir: Path,
    ) -> None:
        validate_sandbox_backend(config.sandbox_backend)
        self.task = task
        self.agent = agent
        self.config = config
        self.run_dir = run_dir
        self.nodes_dir = self.run_dir / "nodes"
        self.state_path = self.run_dir / "search_state.json"
        self.events_path = self.run_dir / "tree_events.jsonl"
        self.task_snapshot = self.task.snapshot()
        self.root_memory = RootMemory(objective=self.task_snapshot.objective)
        self.nodes: dict[str, SearchNode] = {}
        self.frontier_node_ids: list[str] = []
        self.final_result: SearchResult | None = None
        self._node_counter = 0

    def run(self) -> SearchResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.task.prepare_run(self.run_dir)

        baseline = self._initialize_baseline()
        self.frontier_node_ids = [baseline.id]

        for round_index in range(self.config.max_rounds):
            accepted_this_round: list[SearchNode] = []
            for parent_id in list(self.frontier_node_ids):
                parent = self.nodes[parent_id]
                proposals = self.agent.propose(
                    self.task,
                    self.task_snapshot,
                    self.root_memory,
                    parent,
                    self.config.children_per_expansion,
                )
                self._log_event(
                    "proposal_completed",
                    {
                        "round": round_index + 1,
                        "parent_id": parent.id,
                        "proposal_count": len(proposals),
                    },
                )
                for hypothesis in proposals[: self.config.children_per_expansion]:
                    if self._is_duplicate_hypothesis(hypothesis):
                        continue
                    node = self._create_node(parent, hypothesis)
                    self._record_hypothesis(node.hypothesis.core_hypothesis)
                    self._run_candidate(node, parent)
                    if node.status == "completed" and node.evaluation_result and node.evaluation_result.accepted:
                        accepted_this_round.append(node)

            if accepted_this_round:
                accepted_this_round.sort(
                    key=lambda item: item.evaluation_result.score if item.evaluation_result else float("-inf"),
                    reverse=True,
                )
                next_frontier = [node.id for node in accepted_this_round[: self.config.beam_width]]
                promoted = set(next_frontier)
                for node in accepted_this_round[self.config.beam_width :]:
                    node.status = "pruned"
                self.frontier_node_ids = next_frontier
            else:
                promoted = set(self.frontier_node_ids)

            self.root_memory.rounds_completed = round_index + 1
            self._log_event(
                "frontier_updated",
                {
                    "round": round_index + 1,
                    "frontier_node_ids": list(self.frontier_node_ids),
                    "accepted_node_ids": [node.id for node in accepted_this_round],
                    "retained_previous_frontier": not bool(accepted_this_round),
                    "retained_node_ids": sorted(promoted),
                },
            )
            self._persist_state()

        best_node = self.nodes.get(self.root_memory.best_node_id or "")
        promoted_path = None
        if best_node is not None:
            promoted_path = self.task.promote(best_node, self.root_memory)
            best_node.status = "promoted"

        final_verify = None
        if self.config.enable_final_verify:
            final_verify = self.task.final_verify(self.root_memory)

        self.final_result = SearchResult(
            run_dir=str(self.run_dir),
            best_node_id=self.root_memory.best_node_id,
            promoted_path=promoted_path,
            final_verify=final_verify,
            frontier_node_ids=list(self.frontier_node_ids),
            total_nodes=len(self.nodes),
        )
        self._log_event(
            "search_finished",
            {
                "best_node_id": self.final_result.best_node_id,
                "promoted_path": self.final_result.promoted_path,
                "final_verify": self.final_result.final_verify,
                "total_nodes": self.final_result.total_nodes,
            },
        )
        self._persist_state()
        return self.final_result

    def _initialize_baseline(self) -> SearchNode:
        baseline = self._create_node(
            None,
            Hypothesis(
                id="baseline",
                title="Baseline",
                core_hypothesis="Record the unmodified baseline candidate.",
                constraints_summary="No edits.",
            ),
        )
        self.task.create_sandbox(baseline, None)
        baseline.execution_result = ExecutionResult(
            status="baseline",
            summary="Baseline candidate copied into sandbox.",
            changed=False,
            candidate_path=baseline.candidate_path,
        )
        baseline.evaluation_result = self.task.evaluate(baseline)
        baseline.status = "completed"

        self.root_memory.baseline_score = baseline.evaluation_result.score
        self.root_memory.best_score = baseline.evaluation_result.score
        self.root_memory.best_node_id = baseline.id
        self._record_hypothesis(baseline.hypothesis.core_hypothesis)
        self._log_event(
            "baseline_completed",
            {
                "node_id": baseline.id,
                "score": baseline.evaluation_result.score,
                "accepted": baseline.evaluation_result.accepted,
                "summary": baseline.evaluation_result.summary,
            },
        )
        self._persist_state()
        return baseline

    def _create_node(
        self,
        parent: SearchNode | None,
        hypothesis: Hypothesis,
    ) -> SearchNode:
        node_id = self._next_node_id()
        node_dir = self.nodes_dir / node_id
        node_dir.mkdir(parents=True, exist_ok=True)
        node = SearchNode(
            id=node_id,
            parent_id=parent.id if parent is not None else None,
            depth=0 if parent is None else parent.depth + 1,
            hypothesis=hypothesis,
            node_dir=str(node_dir),
            status="queued",
        )
        self.nodes[node_id] = node
        return node

    def _run_candidate(self, node: SearchNode, parent: SearchNode) -> None:
        node.status = "running"
        self.task.create_sandbox(node, parent)
        self._persist_state()

        try:
            node.execution_result = self.agent.execute(
                self.task,
                self.task_snapshot,
                self.root_memory,
                parent,
                node,
            )
            self._log_event(
                "execution_completed",
                {
                    "node_id": node.id,
                    "parent_id": parent.id,
                    "status": node.execution_result.status,
                    "changed": node.execution_result.changed,
                },
            )
            execution_status = node.execution_result.status.lower()
            if execution_status != "completed":
                node.status = "failed"
                node.error = node.execution_result.summary
            else:
                node.evaluation_result = self.task.evaluate(node)
                self._log_event(
                    "evaluation_completed",
                    {
                        "node_id": node.id,
                        "accepted": node.evaluation_result.accepted,
                        "score": node.evaluation_result.score,
                        "summary": node.evaluation_result.summary,
                    },
                )
                node.insight = self.agent.reflect(
                    self.task,
                    self.task_snapshot,
                    self.root_memory,
                    parent,
                    node,
                )
                self._log_event(
                    "reflection_completed",
                    {
                        "node_id": node.id,
                        "polarity": node.insight.polarity,
                        "summary": node.insight.summary,
                    },
                )
                node.status = "completed"
                self._merge_completed_node(node)
        except AgentStepError as exc:
            node.status = "failed"
            node.error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive catch
            node.status = "failed"
            node.error = f"Unexpected node failure: {exc}"

        self._log_event(
            "candidate_completed",
            {
                "node_id": node.id,
                "parent_id": node.parent_id,
                "status": node.status,
                "score": node.evaluation_result.score if node.evaluation_result else None,
                "accepted": node.evaluation_result.accepted if node.evaluation_result else None,
                "error": node.error,
            },
        )
        self._persist_state()

    def _merge_completed_node(self, node: SearchNode) -> None:
        if node.insight is not None:
            if node.insight.polarity == "positive":
                self.root_memory.positive_insights.append(node.insight.summary)
            elif node.insight.polarity == "negative":
                self.root_memory.negative_insights.append(node.insight.summary)
            else:
                self.root_memory.neutral_insights.append(node.insight.summary)

        if node.evaluation_result is None:
            return

        if (
            node.evaluation_result.accepted
            and (
                self.root_memory.best_score is None
                or node.evaluation_result.score > self.root_memory.best_score
            )
        ):
            self.root_memory.best_score = node.evaluation_result.score
            self.root_memory.best_node_id = node.id

    def _record_hypothesis(self, hypothesis_summary: str) -> None:
        if hypothesis_summary not in self.root_memory.explored_hypotheses:
            self.root_memory.explored_hypotheses.append(hypothesis_summary)

    def _is_duplicate_hypothesis(self, hypothesis: Hypothesis) -> bool:
        return hypothesis.core_hypothesis in self.root_memory.explored_hypotheses

    def _persist_state(self) -> None:
        payload = {
            "task_snapshot": asdict(self.task_snapshot),
            "search_config": asdict(self.config),
            "root_memory": asdict(self.root_memory),
            "frontier_node_ids": list(self.frontier_node_ids),
            "nodes": {node_id: asdict(node) for node_id, node in self.nodes.items()},
            "final_result": asdict(self.final_result) if self.final_result else None,
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _log_event(self, event_type: str, payload: dict[str, object]) -> None:
        event = {
            "timestamp": _now_iso(),
            "event": event_type,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def _next_node_id(self) -> str:
        self._node_counter += 1
        return f"node-{self._node_counter:04d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
