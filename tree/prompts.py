from __future__ import annotations

from .watchman import WatchmanPacket
from .types import RootMemory, SearchNode, TaskSnapshot


def _tail(items: list[str], limit: int = 5) -> list[str]:
    if len(items) <= limit:
        return items
    return items[-limit:]


def summarize_root_memory(root: RootMemory) -> dict[str, object]:
    return {
        "objective": root.objective,
        "baseline_score": root.baseline_score,
        "best_score": root.best_score,
        "best_node_id": root.best_node_id,
        "rounds_completed": root.rounds_completed,
        "explored_hypotheses": _tail(root.explored_hypotheses, 8),
        "positive_insights": _tail(root.positive_insights, 5),
        "negative_insights": _tail(root.negative_insights, 5),
        "neutral_insights": _tail(root.neutral_insights, 5),
    }


def summarize_node(node: SearchNode | None) -> dict[str, object] | None:
    if node is None:
        return None

    evaluation = None
    if node.evaluation_result is not None:
        evaluation = {
            "accepted": node.evaluation_result.accepted,
            "score": node.evaluation_result.score,
            "summary": node.evaluation_result.summary,
            "metrics": node.evaluation_result.metrics,
        }

    insight = None
    if node.insight is not None:
        insight = {
            "polarity": node.insight.polarity,
            "summary": node.insight.summary,
            "evidence": node.insight.evidence,
        }

    return {
        "id": node.id,
        "depth": node.depth,
        "hypothesis": {
            "id": node.hypothesis.id,
            "title": node.hypothesis.title,
            "core_hypothesis": node.hypothesis.core_hypothesis,
            "constraints_summary": node.hypothesis.constraints_summary,
            "family": node.hypothesis.family,
            "rationale": node.hypothesis.rationale,
            "target_signals": node.hypothesis.target_signals,
            "supporting_evidence": node.hypothesis.supporting_evidence,
            "disallowed_directions": node.hypothesis.disallowed_directions,
        },
        "candidate_path": node.candidate_path,
        "evaluation": evaluation,
        "insight": insight,
    }


def build_proposal_payload(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    limit: int,
    watchman_packet: WatchmanPacket | None = None,
) -> dict[str, object]:
    return {
        "action": "propose",
        "children_per_expansion": limit,
        "task": {
            "objective": task_snapshot.objective,
            "editable_paths": task_snapshot.editable_paths,
            "constraints": task_snapshot.constraints,
            "reference_paths": task_snapshot.reference_paths,
            "metadata": task_snapshot.metadata,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
        "watchman": watchman_packet.to_payload() if watchman_packet is not None else None,
        "output_contract": {
            "type": "json",
            "schema_hint": {
                "hypotheses": [
                    {
                        "id": "short-unique-id",
                        "title": "brief title",
                        "core_hypothesis": "single-sentence hypothesis",
                        "constraints_summary": "what must stay true",
                        "editable_path_hints": ["kernel.py"],
                        "family": "memory_hierarchy",
                        "rationale": "why this family is worth trying now",
                        "target_signals": ["bandwidth_gb_s"],
                        "supporting_evidence": ["bottleneck=memory"],
                        "disallowed_directions": ["do not switch into tensorization"],
                    },
                ],
            },
        },
    }


def build_proposal_prompt(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    limit: int,
    watchman_packet: WatchmanPacket | None = None,
) -> str:
    watchman_lines = ""
    if watchman_packet is not None:
        families = ", ".join(card.family for card in watchman_packet.branch_cards)
        policy_lines = ""
        if watchman_packet.search_policy is not None:
            policy = watchman_packet.search_policy
            policy_lines = (
                f"Recommended scope: {policy.recommended_scope or 'unspecified'}\n"
                f"Exploration bias: {policy.exploration_bias}\n"
                f"Suggested children: {policy.suggested_children if policy.suggested_children is not None else 'use upper bound'}\n"
                f"Suggested beam width: {policy.suggested_beam_width if policy.suggested_beam_width is not None else 'use upper bound'}\n"
                f"Suggested rounds: {policy.suggested_rounds if policy.suggested_rounds is not None else 'use upper bound'}\n"
                f"Stop conditions: {', '.join(policy.stop_conditions) if policy.stop_conditions else 'none'}\n"
            )
        region_lines = ""
        if watchman_packet.region_graph is not None and watchman_packet.region_graph.regions:
            graph = watchman_packet.region_graph
            focus = next(
                (region for region in graph.regions if region.id == graph.focus_region_id),
                graph.regions[0],
            )
            nearby_regions = [
                region
                for region in graph.regions
                if region.id in focus.adjacent and region.id != focus.id
            ]
            if not nearby_regions:
                nearby_regions = [
                    region for region in graph.regions if region.id != focus.id
                ][:2]
            nearby_summary = ", ".join(
                f"{region.label} ({region.scope})" for region in nearby_regions[:2]
            )
            region_lines = (
                f"Focus region: {focus.label} ({focus.scope})\n"
                f"Region summary: {focus.summary or 'none'}\n"
                f"Region signals: {', '.join(focus.signals) if focus.signals else 'none'}\n"
                f"Nearby regions: {nearby_summary or 'none'}\n"
            )
        bottleneck_lines = ""
        if watchman_packet.bottleneck_card is not None:
            bottleneck = watchman_packet.bottleneck_card
            bottleneck_lines = (
                f"Watchman bottleneck scope: {bottleneck.scope}\n"
                f"Watchman target: {bottleneck.target}\n"
                f"Diagnoses: {', '.join(bottleneck.diagnoses) if bottleneck.diagnoses else 'unknown'}\n"
                f"Preferred response families: {', '.join(bottleneck.response_families) if bottleneck.response_families else 'none'}\n"
                f"Avoid families: {', '.join(bottleneck.avoid_families) if bottleneck.avoid_families else 'none'}\n"
            )
        watchman_lines = (
            f"{region_lines}"
            f"{bottleneck_lines}"
            f"{policy_lines}"
            f"Watchman branch families: {families}\n"
            "Use the watchman branch cards from the payload as the frozen high-level decision layer.\n"
            "Prefer one child per distinct family before proposing close variants.\n"
        )
    return (
        "Propose distinct child hypotheses for one local optimization step.\n"
        "Return JSON only via TREE_OUTPUT_PATH.\n"
        f"Task objective: {task_snapshot.objective}\n"
        f"Target path: {task_snapshot.target_path}\n"
        f"Allowed editable paths: {', '.join(task_snapshot.editable_paths)}\n"
        f"Need up to {limit} hypotheses.\n"
        "Rules:\n"
        "- One core hypothesis per child.\n"
        "- Keep the hypotheses short and mutually distinct.\n"
        "- Do not propose modifying benchmark, verifier, or orchestrator code.\n"
        "- Use only the root-memory summary and parent summary from the payload.\n"
        "- Treat watchman output as the high-level branch-identity prior.\n"
        f"Current best score: {root.best_score}\n"
        f"Parent node: {parent.id}\n"
        f"{watchman_lines}"
    )


def build_execution_payload(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    node: SearchNode,
) -> dict[str, object]:
    return {
        "action": "execute",
        "task": {
            "objective": task_snapshot.objective,
            "editable_paths": task_snapshot.editable_paths,
            "constraints": task_snapshot.constraints,
            "reference_paths": task_snapshot.reference_paths,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
        "node": summarize_node(node),
        "sandbox_path": node.sandbox_path,
        "candidate_path": node.candidate_path,
        "output_contract": {
            "type": "json",
            "schema_hint": {
                "status": "completed",
                "summary": "brief execution summary",
                "changed": True,
                "metadata": {"notes": "optional"},
            },
        },
    }


def build_execution_prompt(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    node: SearchNode,
) -> str:
    return (
        "Execute one bounded experiment for the current node.\n"
        "Return JSON only via TREE_OUTPUT_PATH.\n"
        f"Task objective: {task_snapshot.objective}\n"
        f"Editable paths: {', '.join(task_snapshot.editable_paths)}\n"
        f"Candidate path: {node.candidate_path}\n"
        f"Sandbox path: {node.sandbox_path}\n"
        f"Parent node: {parent.id}\n"
        f"Node hypothesis: {node.hypothesis.core_hypothesis}\n"
        f"Mechanism family: {node.hypothesis.family}\n"
        f"High-level rationale: {node.hypothesis.rationale}\n"
        f"Target signals: {', '.join(node.hypothesis.target_signals) if node.hypothesis.target_signals else 'none'}\n"
        f"Disallowed directions: {', '.join(node.hypothesis.disallowed_directions) if node.hypothesis.disallowed_directions else 'none'}\n"
        "Rules:\n"
        "- Only modify allowed editable files inside the sandbox.\n"
        "- Do not run or alter benchmark / verifier logic unless the task explicitly says so.\n"
        "- Perform at most one local experiment aligned with the node hypothesis.\n"
        "- Stay within the mechanism family and guardrails frozen by watchman.\n"
        f"Current best score before this node: {root.best_score}\n"
    )


def build_reflection_payload(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode | None,
    node: SearchNode,
) -> dict[str, object]:
    return {
        "action": "reflect",
        "task": {
            "objective": task_snapshot.objective,
            "constraints": task_snapshot.constraints,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
        "node": summarize_node(node),
        "output_contract": {
            "type": "json",
            "schema_hint": {
                "polarity": "positive|negative|neutral",
                "summary": "single actionable insight",
                "evidence": ["short evidence string"],
            },
        },
    }


def build_reflection_prompt(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode | None,
    node: SearchNode,
) -> str:
    parent_id = parent.id if parent is not None else "none"
    return (
        "Compress the node outcome into one insight.\n"
        "Return JSON only via TREE_OUTPUT_PATH.\n"
        f"Task objective: {task_snapshot.objective}\n"
        f"Node id: {node.id}\n"
        f"Parent node: {parent_id}\n"
        f"Node hypothesis: {node.hypothesis.core_hypothesis}\n"
        f"Mechanism family: {node.hypothesis.family}\n"
        f"Current best score before merge: {root.best_score}\n"
        "Rules:\n"
        "- Keep the summary short and reusable by future proposal steps.\n"
        "- Base the insight on node evaluation metrics and hypothesis fit.\n"
        "- Do not restate the whole log.\n"
    )
