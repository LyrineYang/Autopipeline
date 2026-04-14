from __future__ import annotations

from typing import TYPE_CHECKING

from .types import RootMemory, SearchNode, TaskSnapshot

if TYPE_CHECKING:
    from .watchman import WatchmanPacket


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


def build_watchman_payload(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    limit: int,
    raw_context: dict[str, object],
    upper_bounds: dict[str, int],
) -> dict[str, object]:
    return {
        "action": "plan",
        "children_upper_bound": limit,
        "upper_bounds": upper_bounds,
        "task": {
            "type": task_snapshot.task_type,
            "objective": task_snapshot.objective,
            "target_path": task_snapshot.target_path,
            "editable_paths": task_snapshot.editable_paths,
            "constraints": task_snapshot.constraints,
            "reference_paths": task_snapshot.reference_paths,
            "metadata": task_snapshot.metadata,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
        "facts": raw_context,
        "output_contract": {
            "type": "json",
            "schema_hint": {
                "bottleneck_card": {
                    "scope": "kernel",
                    "target": task_snapshot.target_path,
                    "symptoms": ["local_bottleneck=memory"],
                    "diagnoses": ["memory_bandwidth_pressure"],
                    "response_families": ["memory_hierarchy"],
                    "avoid_families": ["instruction_selection"],
                    "rationale": "why this diagnosis matches the current evidence",
                },
                "search_policy": {
                    "recommended_scope": "kernel",
                    "scope_options": ["kernel"],
                    "exploration_bias": "balanced",
                    "branch_budget": {"children": 2, "beam_width": 2, "rounds": 2},
                    "resource_hint": {"tier": "balanced", "notes": ["short resource note"]},
                    "stop_conditions": ["one short stopping rule"],
                },
                "branch_cards": [
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
                "frozen_decisions": ["short line that locks the high-level direction"],
            },
        },
    }


def build_watchman_prompt(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    limit: int,
    raw_context: dict[str, object],
    upper_bounds: dict[str, int],
) -> str:
    facts_summary = []
    planner_rules = raw_context.get("planner_rules")
    if isinstance(planner_rules, dict):
        allowed_scopes = planner_rules.get("allowed_scopes")
        allowed_families = planner_rules.get("allowed_families")
        if isinstance(allowed_scopes, list):
            facts_summary.append(f"Allowed scopes: {', '.join(str(item) for item in allowed_scopes)}")
        if isinstance(allowed_families, list):
            facts_summary.append(f"Allowed families: {', '.join(str(item) for item in allowed_families[:8])}")
    if raw_context.get("problem_summary"):
        facts_summary.append(f"Problem summary: {raw_context['problem_summary']}")
    return (
        "You are the high-level planner for one search node.\n"
        "Return JSON only via TREE_OUTPUT_PATH.\n"
        "Use TREE_TELEMETRY_PATH for rich environment context.\n"
        f"Task objective: {task_snapshot.objective}\n"
        f"Target path: {task_snapshot.target_path}\n"
        f"Children upper bound: {limit}\n"
        f"Search upper bounds: beam={upper_bounds.get('beam_width')}, rounds={upper_bounds.get('max_rounds')}, children={upper_bounds.get('children_per_expansion')}\n"
        f"Parent node: {parent.id}\n"
        f"Current best score: {root.best_score}\n"
        "Rules:\n"
        "- Consume facts, rules, history, and telemetry to decide the next high-level direction.\n"
        "- Freeze the decision for the executor: scope, branch identities, budget hints, and stop conditions.\n"
        "- Do not exceed the allowed edit surface or allowed scopes from the payload.\n"
        "- Prefer distinct branches before close variants, but do not force diversity when the evidence is strong.\n"
        "- Record only the key high-level decisions that the executor must not reopen.\n"
        f"{chr(10).join(facts_summary)}\n"
    )


def build_watchman_telemetry(
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode,
    raw_context: dict[str, object],
    upper_bounds: dict[str, int],
) -> dict[str, object]:
    telemetry = {
        "action": "plan",
        "task": {
            "type": task_snapshot.task_type,
            "objective": task_snapshot.objective,
            "target_path": task_snapshot.target_path,
            "editable_paths": task_snapshot.editable_paths,
            "constraints": task_snapshot.constraints,
            "reference_paths": task_snapshot.reference_paths,
            "metadata": task_snapshot.metadata,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
        "upper_bounds": upper_bounds,
        "facts": raw_context,
    }
    return telemetry


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
        "Use TREE_TELEMETRY_PATH if you need the richer environment snapshot.\n"
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
        "- You may tighten the local stopping rule if the telemetry shows the branch is exhausted.\n"
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
        "Use TREE_TELEMETRY_PATH if you need the richer environment snapshot.\n"
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


def build_agent_telemetry(
    *,
    action: str,
    task_snapshot: TaskSnapshot,
    root: RootMemory,
    parent: SearchNode | None,
    node: SearchNode | None = None,
    watchman_packet: WatchmanPacket | None = None,
) -> dict[str, object]:
    telemetry: dict[str, object] = {
        "action": action,
        "task": {
            "type": task_snapshot.task_type,
            "objective": task_snapshot.objective,
            "target_path": task_snapshot.target_path,
            "editable_paths": task_snapshot.editable_paths,
            "constraints": task_snapshot.constraints,
            "reference_paths": task_snapshot.reference_paths,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
    }
    if node is not None:
        telemetry["node"] = summarize_node(node)
    if watchman_packet is not None:
        focus_region = None
        nearby_regions: list[dict[str, object]] = []
        if watchman_packet.region_graph is not None and watchman_packet.region_graph.regions:
            graph = watchman_packet.region_graph
            focus = next(
                (region for region in graph.regions if region.id == graph.focus_region_id),
                graph.regions[0],
            )
            focus_region = {
                "id": focus.id,
                "label": focus.label,
                "scope": focus.scope,
                "summary": focus.summary,
                "signals": focus.signals,
            }
            nearby_regions = [
                {
                    "id": region.id,
                    "label": region.label,
                    "scope": region.scope,
                    "signals": region.signals,
                }
                for region in graph.regions
                if region.id != focus.id
            ][:2]
        telemetry["watchman"] = {
            "static_prior": watchman_packet.static_prior,
            "hardware_context": watchman_packet.hardware_context,
            "benchmark_context": watchman_packet.benchmark_context,
            "history_context": watchman_packet.history_context,
            "focus_region": focus_region,
            "nearby_regions": nearby_regions,
            "bottleneck_card": asdict(watchman_packet.bottleneck_card) if watchman_packet.bottleneck_card is not None else None,
            "search_policy": asdict(watchman_packet.search_policy) if watchman_packet.search_policy is not None else None,
            "branch_cards": [asdict(card) for card in watchman_packet.branch_cards[:4]],
        }
    return telemetry
