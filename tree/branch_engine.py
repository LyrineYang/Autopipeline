from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from .agent import AgentAdapter
from .roles import role_uses_agent
from .task import candidate_checkpoint_path, checkpoint_candidate, restore_candidate
from .types import ExecutionResult, Insight, RootMemory, SearchConfig, SearchNode, TaskSnapshot


class BranchEngine:
    """Run one branch-local optimization loop under a fixed branch identity."""

    def __init__(
        self,
        *,
        task,
        agent: AgentAdapter,
        task_snapshot: TaskSnapshot,
        root_memory: RootMemory,
        config: SearchConfig,
        log_event: Callable[[str, dict[str, object]], None],
        persist_state: Callable[[], None],
        merge_completed: Callable[[SearchNode], None],
        budget_ledger: Any | None = None,
    ) -> None:
        self.task = task
        self.agent = agent
        self.task_snapshot = task_snapshot
        self.root_memory = root_memory
        self.config = config
        self.log_event = log_event
        self.persist_state = persist_state
        self.merge_completed = merge_completed
        self.budget_ledger = budget_ledger

    def run(self, node: SearchNode, parent: SearchNode) -> None:
        if self._agent_budget_exhausted("executor"):
            self._mark_budget_pruned(node)
            return
        node.status = "running"
        self.task.create_sandbox(node, parent)
        self._prepare_execution_surface(node, parent)
        self.persist_state()

        branch_iterations = self._branch_iteration_limit(parent, node)
        if branch_iterations <= 1:
            self._run_single_step(node, parent)
            return

        self._run_local_loop(node, parent, branch_iterations)

    def _run_single_step(self, node: SearchNode, parent: SearchNode) -> None:
        node.execution_result = self.agent.execute(
            self.task,
            self.task_snapshot,
            self.root_memory,
            parent,
            node,
        )
        self.log_event(
            "execution_completed",
            {
                "node_id": node.id,
                "parent_id": parent.id,
                "status": node.execution_result.status,
                "changed": node.execution_result.changed,
            },
        )
        if node.execution_result.status.lower() != "completed":
            node.status = "failed"
            node.error = node.execution_result.summary
            return

        node.evaluation_result = self._evaluate_node(node)
        self.log_event(
            "evaluation_completed",
            {
                "node_id": node.id,
                "accepted": node.evaluation_result.accepted,
                "score": node.evaluation_result.score,
                "summary": node.evaluation_result.summary,
            },
        )
        node.insight = self._reflect(node, parent)
        self.log_event(
            "reflection_completed",
            {
                "node_id": node.id,
                "polarity": node.insight.polarity,
                "summary": node.insight.summary,
            },
        )
        node.status = "completed"
        self.merge_completed(node)

    def _run_local_loop(self, node: SearchNode, parent: SearchNode, branch_iterations: int) -> None:
        branch_best_score = (
            parent.evaluation_result.score
            if parent.evaluation_result is not None and parent.evaluation_result.accepted
            else float("-inf")
        )
        best_candidate_path = candidate_checkpoint_path(node, "best_candidate")
        checkpoint_candidate(self.task, node, best_candidate_path)
        best_evaluation = None
        best_execution = None
        last_evaluation = None
        last_execution = None
        local_history: list[dict[str, object]] = []

        for iteration in range(1, branch_iterations + 1):
            if self._agent_budget_exhausted("executor"):
                local_history.append(
                    {
                        "iteration": iteration,
                        "status": "skipped",
                        "changed": False,
                        "summary": "Agent budget exhausted before this local iteration.",
                    }
                )
                if best_evaluation is None or best_execution is None:
                    node.execution_result = ExecutionResult(
                        status="skipped",
                        summary="Agent budget exhausted before the branch produced an improvement.",
                        changed=False,
                        candidate_path=node.candidate_path,
                        metadata={"local_history": local_history, "budget_exhausted": True},
                    )
                    node.status = "pruned"
                    return
                break
            node.planner_context["local_iteration"] = iteration
            node.planner_context["local_iteration_limit"] = branch_iterations
            node.planner_context["current_branch_best_score"] = (
                None if branch_best_score == float("-inf") else branch_best_score
            )
            node.planner_context["local_history"] = local_history[-3:]
            self._prepare_execution_surface(node, parent)

            execution_result = self.agent.execute(
                self.task,
                self.task_snapshot,
                self.root_memory,
                parent,
                node,
            )
            self.log_event(
                "execution_completed",
                {
                    "node_id": node.id,
                    "parent_id": parent.id,
                    "local_iteration": iteration,
                    "status": execution_result.status,
                    "changed": execution_result.changed,
                },
            )

            if execution_result.status.lower() != "completed":
                local_history.append(
                    {
                        "iteration": iteration,
                        "status": execution_result.status,
                        "changed": execution_result.changed,
                        "summary": execution_result.summary,
                    }
                )
                if best_evaluation is None or best_execution is None:
                    node.execution_result = ExecutionResult(
                        status="failed",
                        summary=f"Branch loop stopped at iteration {iteration}: {execution_result.summary}",
                        changed=False,
                        candidate_path=node.candidate_path,
                        metadata={"local_history": local_history},
                    )
                    node.status = "failed"
                    node.error = node.execution_result.summary
                    return
                break

            evaluation_result = self._evaluate_node(node, iteration=iteration)
            last_evaluation = evaluation_result
            last_execution = execution_result
            self.log_event(
                "evaluation_completed",
                {
                    "node_id": node.id,
                    "local_iteration": iteration,
                    "accepted": evaluation_result.accepted,
                    "score": evaluation_result.score,
                    "summary": evaluation_result.summary,
                },
            )
            local_history.append(
                {
                    "iteration": iteration,
                    "status": execution_result.status,
                    "changed": execution_result.changed,
                    "accepted": evaluation_result.accepted,
                    "score": evaluation_result.score,
                    "summary": evaluation_result.summary,
                }
            )

            if evaluation_result.accepted and evaluation_result.score > branch_best_score:
                checkpoint_candidate(self.task, node, best_candidate_path)
                branch_best_score = evaluation_result.score
                best_evaluation = evaluation_result
                best_execution = execution_result
            else:
                restore_candidate(self.task, node, best_candidate_path)
                self._prepare_execution_surface(node, parent)

            if not execution_result.changed:
                break

        node.planner_context["local_history"] = local_history[-5:]
        if best_evaluation is None or best_execution is None:
            if last_evaluation is not None and last_execution is not None:
                restored_evaluation = self._evaluate_node(node)
                self.log_event(
                    "evaluation_completed",
                    {
                        "node_id": node.id,
                        "branch_outcome": "exhausted_restored",
                        "accepted": restored_evaluation.accepted,
                        "score": restored_evaluation.score,
                        "summary": restored_evaluation.summary,
                    },
                )
                node.execution_result = ExecutionResult(
                    status="completed",
                    summary=(
                        f"Branch exhausted after {len(local_history)} local iterations with no accepted "
                        "improvement; restored the previous candidate and pruned this branch."
                    ),
                    changed=False,
                    candidate_path=node.candidate_path,
                    raw_log_paths=last_execution.raw_log_paths,
                    metadata={
                        **last_execution.metadata,
                        "local_history": local_history,
                        "branch_iterations": len(local_history),
                        "branch_outcome": "exhausted",
                    },
                )
                node.evaluation_result = restored_evaluation
                self._prepare_execution_surface(node, parent)
                node.status = "pruned"
                return
            node.execution_result = ExecutionResult(
                status="failed",
                summary=f"No accepted improvement after {len(local_history)} local iterations.",
                changed=False,
                candidate_path=node.candidate_path,
                metadata={"local_history": local_history},
            )
            node.status = "failed"
            node.error = node.execution_result.summary
            return

        restore_candidate(self.task, node, best_candidate_path)
        self._prepare_execution_surface(node, parent)
        node.execution_result = ExecutionResult(
            status="completed",
            summary=(
                f"Completed {len(local_history)} local iterations and kept the best accepted improvement "
                f"at score {best_evaluation.score:.3f}."
            ),
            changed=best_execution.changed,
            candidate_path=node.candidate_path,
            raw_log_paths=best_execution.raw_log_paths,
            metadata={
                **best_execution.metadata,
                "local_history": local_history,
                "branch_iterations": len(local_history),
            },
        )
        node.evaluation_result = best_evaluation
        node.insight = self._reflect(node, parent)
        self.log_event(
            "reflection_completed",
            {
                "node_id": node.id,
                "polarity": node.insight.polarity,
                "summary": node.insight.summary,
            },
        )
        node.status = "completed"
        self.merge_completed(node)

    def _prepare_execution_surface(self, node: SearchNode, parent: SearchNode) -> None:
        prepare_surface = getattr(self.task, "prepare_execution_surface", None)
        if callable(prepare_surface):
            prepare_surface(node, parent)

    def _evaluate_node(self, node: SearchNode, *, iteration: int | None = None):
        evaluation_result = self.task.evaluate(node)
        if iteration is None:
            return evaluation_result
        snapped_paths: dict[str, str] = {}
        for label, raw_path in evaluation_result.raw_log_paths.items():
            source = Path(raw_path)
            if not source.exists():
                continue
            suffix = source.suffix or ".log"
            destination = Path(node.node_dir) / f"bench.iter-{iteration:02d}.{label}{suffix}"
            shutil.copy2(source, destination)
            snapped_paths[label] = str(destination)
        evaluation_result.raw_log_paths = snapped_paths or evaluation_result.raw_log_paths
        return evaluation_result

    def _branch_iteration_limit(self, parent: SearchNode, node: SearchNode) -> int:
        if not role_uses_agent(self.config, "executor"):
            return 1
        planner_limit = node.planner_context.get("branch_budget_rounds")
        planner_limit = int(planner_limit) if planner_limit is not None else None
        provider = getattr(self.task, "branch_iteration_limit", None)
        if not callable(provider):
            return max(1, planner_limit) if planner_limit is not None else 1
        try:
            limit = int(provider(parent, node))
        except TypeError:
            limit = int(provider())
        if planner_limit is not None:
            limit = min(limit, planner_limit)
        remaining_agent_calls = (
            self.budget_ledger.remaining_agent_calls()
            if self.budget_ledger is not None
            else None
        )
        if remaining_agent_calls is not None:
            limit = min(limit, max(1, remaining_agent_calls))
        return max(1, limit)

    def _reflect(self, node: SearchNode, parent: SearchNode) -> Insight:
        if self._agent_budget_exhausted("reflector"):
            self.log_event(
                "reflection_skipped",
                {
                    "node_id": node.id,
                    "reason": "agent_budget_exhausted",
                },
            )
            return Insight(
                polarity="neutral",
                summary="Reflection skipped because the agent budget was exhausted.",
                evidence=[],
                source_node_id=node.id,
            )
        return self.agent.reflect(
            self.task,
            self.task_snapshot,
            self.root_memory,
            parent,
            node,
        )

    def _agent_budget_exhausted(self, role: str) -> bool:
        return (
            self.budget_ledger is not None
            and role_uses_agent(self.config, role)
            and not self.budget_ledger.can_start_agent_step()
        )

    def _mark_budget_pruned(self, node: SearchNode) -> None:
        node.execution_result = ExecutionResult(
            status="skipped",
            summary="Agent budget exhausted before execution.",
            changed=False,
            candidate_path=node.candidate_path,
            metadata={"budget_exhausted": True},
        )
        node.status = "pruned"
