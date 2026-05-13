from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import threading

from .agent import AgentAdapter, AgentStepError
from .archive import RunArchive
from .branch_engine import BranchEngine
from .budget import BudgetLedger
from .roles import normalize_role_configs, role_config_for
from .task import OptimizationTask, validate_sandbox_backend
from .types import (
    ExecutionResult,
    Hypothesis,
    RootMemory,
    SearchConfig,
    SearchNode,
    SearchResult,
)
from .planner import Planner, PlannerPacket


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
        self.config.role_configs = normalize_role_configs(self.config.role_configs)
        self.root_memory = RootMemory(objective=self.task_snapshot.objective)
        self.budget_ledger = BudgetLedger(
            max_agent_calls=config.max_agent_calls,
            max_probe_calls=config.max_probe_calls,
        )
        self.archive = RunArchive(self.run_dir, task_snapshot=self.task_snapshot)
        self._state_lock = threading.RLock()
        self.nodes: dict[str, SearchNode] = {}
        self.frontier_node_ids: list[str] = []
        self.final_result: SearchResult | None = None
        self._node_counter = 0
        self.planner = Planner(
            planner_command=config.planner_command,
            command_timeout_seconds=config.command_timeout_seconds,
            budget_ledger=self.budget_ledger,
            planner_handoff_mode=config.planner_handoff_mode,
            role_configs=config.role_configs,
            motif_archive=self.archive,
        )
        budget_setter = getattr(self.agent, "set_budget_ledger", None)
        if callable(budget_setter):
            budget_setter(self.budget_ledger)
        role_setter = getattr(self.agent, "set_role_configs", None)
        if callable(role_setter):
            role_setter(config.role_configs)
        self.branch_engine = BranchEngine(
            task=self.task,
            agent=self.agent,
            task_snapshot=self.task_snapshot,
            root_memory=self.root_memory,
            config=self.config,
            log_event=self._log_event,
            persist_state=self._persist_state,
            merge_completed=self._merge_completed_node,
            budget_ledger=self.budget_ledger,
        )

    def run(self) -> SearchResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.task.prepare_run(self.run_dir)

        baseline = self._initialize_baseline()
        self.frontier_node_ids = [baseline.id]

        for round_index in range(self.config.max_rounds):
            parent_packets = self._build_parent_packets()
            active_parent_packets = [
                (parent, packet)
                for parent, packet in parent_packets
                if self._should_expand_parent(parent, packet)
            ]
            if not active_parent_packets:
                break

            accepted_this_round: list[SearchNode] = []
            round_node_parents: list[tuple[SearchNode, SearchNode]] = []
            effective_beam_width = 1
            for parent, packet in active_parent_packets:
                effective_children = self._effective_children_limit(packet)
                effective_beam_width = min(
                    self.config.beam_width,
                    max(effective_beam_width, self._effective_beam_width(packet)),
                )
                proposals = self.agent.propose(
                    self.task,
                    self.task_snapshot,
                    self.root_memory,
                    parent,
                    effective_children,
                    planner_packet=packet,
                )
                self._log_event(
                    "proposal_completed",
                    {
                        "round": round_index + 1,
                        "parent_id": parent.id,
                        "effective_children": effective_children,
                        "recommended_scope": packet.search_policy.recommended_scope if packet.search_policy else "",
                        "exploration_bias": packet.search_policy.exploration_bias if packet.search_policy else "",
                        "resource_tier": packet.search_policy.resource_hint.tier if packet.search_policy else "",
                        "proposal_count": len(proposals),
                    },
                )
                pending_node_parents: list[tuple[SearchNode, SearchNode]] = []
                for hypothesis in proposals[:effective_children]:
                    if self._is_duplicate_hypothesis(hypothesis):
                        continue
                    node = self._create_node(parent, hypothesis)
                    node.planner_context = {
                        "recommended_scope": packet.search_policy.recommended_scope if packet.search_policy else "",
                        "scope_options": packet.search_policy.scope_options if packet.search_policy else [],
                        "exploration_bias": packet.search_policy.exploration_bias if packet.search_policy else "",
                        "branch_budget_rounds": packet.search_policy.branch_budget.rounds if packet.search_policy else None,
                        "stop_conditions": packet.search_policy.stop_conditions if packet.search_policy else [],
                        "frozen_decisions": list(packet.frozen_decisions),
                        "branch_scope": hypothesis.scope,
                        "branch_target": hypothesis.target,
                        "planner_handoff_mode": self.config.planner_handoff_mode,
                        "verification_intent": list(hypothesis.verification_intent),
                    }
                    self._record_hypothesis(node.hypothesis)
                    pending_node_parents.append((node, parent))
                completed_nodes = self._run_candidate_batch(pending_node_parents)
                round_node_parents.extend((node, parent) for node, parent in pending_node_parents)
                for node in completed_nodes:
                    if node.status == "completed" and node.evaluation_result and node.evaluation_result.accepted:
                        accepted_this_round.append(node)

            if accepted_this_round:
                accepted_this_round.sort(
                    key=lambda item: item.evaluation_result.score if item.evaluation_result else float("-inf"),
                    reverse=True,
                )
                next_frontier = [node.id for node in accepted_this_round[:effective_beam_width]]
                promoted = set(next_frontier)
                for node in accepted_this_round[effective_beam_width:]:
                    node.status = "pruned"
                self.frontier_node_ids = next_frontier
            else:
                promoted = set(self.frontier_node_ids)

            for node, parent in round_node_parents:
                self.archive.record_node(
                    node=node,
                    parent=parent,
                    root=self.root_memory,
                    budget_summary=self.budget_ledger.summary(),
                )

            self.root_memory.rounds_completed = round_index + 1
            self._log_event(
                "frontier_updated",
                {
                    "round": round_index + 1,
                    "frontier_node_ids": list(self.frontier_node_ids),
                    "accepted_node_ids": [node.id for node in accepted_this_round],
                    "retained_previous_frontier": not bool(accepted_this_round),
                    "retained_node_ids": sorted(promoted),
                    "effective_beam_width": effective_beam_width,
                    "active_parent_ids": [parent.id for parent, _ in active_parent_packets],
                },
            )
            self._persist_state()

        best_node = self.nodes.get(self.root_memory.best_node_id or "")
        promoted_path = None
        if best_node is not None:
            promoted_path = self.task.promote(best_node, self.root_memory)

        final_verify = None
        promotion_verified = True
        if self.config.enable_final_verify:
            final_verify = self.task.final_verify(self.root_memory)
            if isinstance(final_verify, dict) and final_verify.get("accepted") is False:
                promotion_verified = False

        if best_node is not None and promoted_path is not None:
            if promotion_verified:
                best_node.status = "promoted"
            else:
                _discard_promoted_artifact(promoted_path)
                promoted_path = None

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
                core_hypothesis="Record the unmodified baseline workspace.",
                scope=self.task_snapshot.task_type,
                target=self.task_snapshot.target_path,
                constraints_summary="No edits.",
                verification_intent=list(self.task_snapshot.objective_signals),
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
        if baseline.evaluation_result.accepted:
            self.root_memory.best_score = baseline.evaluation_result.score
            self.root_memory.best_node_id = baseline.id
        self._record_hypothesis(baseline.hypothesis)
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
        try:
            self.branch_engine.run(node, parent)
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
        self.budget_ledger.record_node(node=node, parent=parent)
        self._persist_state()

    def _run_candidate_batch(self, node_parents: list[tuple[SearchNode, SearchNode]]) -> list[SearchNode]:
        if not node_parents:
            return []
        parallel_limit = self._parallel_branch_limit(len(node_parents))
        if parallel_limit <= 1:
            completed: list[SearchNode] = []
            for node, parent in node_parents:
                self._run_candidate(node, parent)
                completed.append(node)
            return completed

        completed = []
        with ThreadPoolExecutor(max_workers=parallel_limit) as pool:
            future_to_node = {
                pool.submit(self._run_candidate, node, parent): node
                for node, parent in node_parents
            }
            for future in as_completed(future_to_node):
                future.result()
                completed.append(future_to_node[future])
        completed.sort(key=lambda item: item.id)
        return completed

    def _merge_completed_node(self, node: SearchNode) -> None:
        with self._state_lock:
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

    def _record_hypothesis(self, hypothesis: Hypothesis) -> None:
        fingerprint = _hypothesis_fingerprint(hypothesis)
        if fingerprint and fingerprint not in self.root_memory.explored_hypotheses:
            self.root_memory.explored_hypotheses.append(fingerprint)

    def _is_duplicate_hypothesis(self, hypothesis: Hypothesis) -> bool:
        return _hypothesis_fingerprint(hypothesis) in self.root_memory.explored_hypotheses

    def _persist_state(self) -> None:
        with self._state_lock:
            payload = {
                "task_snapshot": asdict(self.task_snapshot),
                "search_config": asdict(self.config),
                "root_memory": asdict(self.root_memory),
                "frontier_node_ids": list(self.frontier_node_ids),
                "nodes": {node_id: asdict(node) for node_id, node in self.nodes.items()},
                "final_result": asdict(self.final_result) if self.final_result else None,
                "budget": self.budget_ledger.summary(),
                "archive": {
                    "motifs": str(self.archive.motifs_path),
                    "family_summary": str(self.archive.family_summary_path),
                    "planner_prior": str(self.archive.planner_prior_path),
                },
            }
            self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _log_event(self, event_type: str, payload: dict[str, object]) -> None:
        with self._state_lock:
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

    def _build_parent_packets(self) -> list[tuple[SearchNode, PlannerPacket]]:
        packets: list[tuple[SearchNode, PlannerPacket]] = []
        for parent_id in list(self.frontier_node_ids):
            parent = self.nodes[parent_id]
            packet = self.planner.build_packet(
                self.task,
                self.task_snapshot,
                self.root_memory,
                parent,
                self.config.children_per_expansion,
                upper_bounds={
                    "children_per_expansion": self.config.children_per_expansion,
                    "beam_width": self.config.beam_width,
                    "max_rounds": self.config.max_rounds,
                },
            )
            packets.append((parent, packet))
        return packets

    def _effective_children_limit(self, packet: PlannerPacket) -> int:
        policy = packet.search_policy
        if policy is None or policy.branch_budget.children is None:
            return self.config.children_per_expansion
        return max(1, min(self.config.children_per_expansion, policy.branch_budget.children))

    def _effective_beam_width(self, packet: PlannerPacket) -> int:
        policy = packet.search_policy
        if policy is None or policy.branch_budget.beam_width is None:
            return self.config.beam_width
        return max(1, min(self.config.beam_width, policy.branch_budget.beam_width))

    def _should_expand_parent(self, parent: SearchNode, packet: PlannerPacket) -> bool:
        if (
            self._uses_agent_commands()
            and not self.budget_ledger.can_start_agent_step()
        ):
            return False
        policy = packet.search_policy
        if policy is None or policy.branch_budget.rounds is None:
            return True
        return parent.depth < min(self.config.max_rounds, policy.branch_budget.rounds)

    def _parallel_branch_limit(self, pending_count: int) -> int:
        configured = max(1, int(self.config.max_parallel_branches or 1))
        if str(self.config.branch_resource_policy).lower() == "serial":
            return 1
        if self._uses_agent_commands() and (
            self.config.max_agent_calls is not None or self.config.max_probe_calls is not None
        ):
            return 1
        return max(1, min(configured, pending_count))

    def _uses_agent_commands(self) -> bool:
        return bool(
            self.config.planner_command
            or self.config.executor_command
            or self.config.reflector_command
            or role_config_for(self.config.role_configs, "planner").command
            or role_config_for(self.config.role_configs, "executor").command
            or role_config_for(self.config.role_configs, "reflector").command
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _discard_promoted_artifact(promoted_path: str) -> None:
    try:
        path = Path(promoted_path)
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _hypothesis_fingerprint(hypothesis: Hypothesis) -> str:
    hypothesis_id = hypothesis.id.strip()
    if hypothesis_id:
        return hypothesis_id
    scope = hypothesis.scope.strip() or "unspecified"
    target = hypothesis.target.strip() or "current-target"
    core = hypothesis.core_hypothesis.strip()
    variant = hypothesis.implementation_variant.strip()
    steps = "|".join(step.strip() for step in hypothesis.implementation_steps if step.strip())
    return f"{scope}|{target}|{core}|{variant}|{steps}" if core else ""
