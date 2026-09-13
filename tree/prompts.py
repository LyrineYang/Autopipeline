from __future__ import annotations

from typing import TYPE_CHECKING

from .types import RootMemory, SearchNode, TaskSnapshot

if TYPE_CHECKING:
    from .planner import PlannerPacket


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
                "idea_id": node.hypothesis.idea_id,
                "implementation_variant": node.hypothesis.implementation_variant,
                "implementation_steps": node.hypothesis.implementation_steps,
                "scope": node.hypothesis.scope,
            "target": node.hypothesis.target,
            "constraints_summary": node.hypothesis.constraints_summary,
            "family": node.hypothesis.family,
            "rationale": node.hypothesis.rationale,
            "target_signals": node.hypothesis.target_signals,
            "verification_intent": node.hypothesis.verification_intent,
            "supporting_evidence": node.hypothesis.supporting_evidence,
            "disallowed_directions": node.hypothesis.disallowed_directions,
            "work_unit": node.hypothesis.work_unit,
        },
        "planner_context": node.planner_context,
        "candidate_path": node.candidate_path,
        "evaluation": evaluation,
        "insight": insight,
    }


def _telemetry_guidance(*, rich: bool) -> str:
    if rich:
        return "Use TREE_TELEMETRY_PATH for rich environment context.\n"
    return "Use TREE_TELEMETRY_PATH if you need the richer environment snapshot.\n"


def _sensing_guidance(*, allow_probes: bool) -> str:
    guidance = (
        "Use TREE_SENSING_PATH for passive snapshots, recent_probe_results, and verifier fields like verdict, evidence, regressions, and uncertainty.\n"
    )
    if allow_probes:
        guidance += (
            "If you need more evidence, write diagnostic probe requests to TREE_PROBE_REQUESTS_PATH and read the results from TREE_PROBE_RESULTS_PATH.\n"
            "Use built-in read/list/grep probes for simple inspection, command/python probes for small generic experiments, theoretical_limit_inputs when you need stable facts for a roofline/headroom calculation, and task-specific probes for domain tools like compile facts, ncu/nsys, or runtime breakdowns.\n"
        )
    return guidance


def build_planner_payload(
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
            "target_kind": task_snapshot.target_kind,
            "surface_type": task_snapshot.surface_type,
            "objective_signals": task_snapshot.objective_signals,
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
                    "symptoms": ["profile_bottlenecks=memory_bandwidth_pressure"],
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
                        "idea_id": "shared-natural-language-idea",
                        "implementation_variant": "one short realization hint",
                        "implementation_steps": ["only in detailed handoff mode"],
                        "scope": "pipeline",
                        "target": task_snapshot.target_path,
                        "constraints_summary": "what must stay true",
                        "editable_path_hints": ["pipeline.yaml", "configs/"],
                        "family": "runtime_policy",
                        "rationale": "why this family is worth trying now",
                        "target_signals": ["end_to_end_ms"],
                        "verification_intent": ["lower_step_time_ms", "improve_overlap"],
                        "supporting_evidence": ["bottleneck=memory"],
                        "disallowed_directions": ["do not switch into tensorization"],
                        "work_unit": {
                            "type": "bottleneck_region",
                            "region_id": "pipeline-critical-path",
                            "scope": "pipeline",
                            "artifact_path": "regions/node-0001.region.json",
                        },
                    },
                ],
                "frozen_decisions": ["short line that locks the high-level direction"],
            },
        },
    }


def build_planner_prompt(
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
    handoff_mode = str(raw_context.get("planner_handoff_mode", "variant"))
    facts_summary.append(f"Planner handoff mode: {handoff_mode}")
    optimization_prior = raw_context.get("optimization_prior")
    if isinstance(optimization_prior, dict):
        families = optimization_prior.get("families")
        if isinstance(families, list):
            family_ids = [
                str(item.get("id"))
                for item in families
                if isinstance(item, dict) and item.get("id")
            ][:8]
            if family_ids:
                facts_summary.append(f"Optimization prior families: {', '.join(family_ids)}")
    budget_context = raw_context.get("budget_context")
    if isinstance(budget_context, dict):
        agent_usage = budget_context.get("agent_usage")
        if isinstance(agent_usage, dict):
            facts_summary.append(
                "Agent budget: "
                f"calls={agent_usage.get('agent_calls')}, "
                f"remaining={agent_usage.get('remaining_agent_calls')}, "
                f"tokens={agent_usage.get('total_tokens')}"
            )
        recommendations = budget_context.get("recommendations")
        if isinstance(recommendations, list) and recommendations:
            facts_summary.append(
                "Budget recommendations: "
                + "; ".join(str(item) for item in recommendations[:4])
            )
    return (
        "You are the high-level planner for one search node.\n"
        "Return JSON only via TREE_OUTPUT_PATH.\n"
        f"{_telemetry_guidance(rich=True)}"
        f"{_sensing_guidance(allow_probes=True)}"
        f"Task objective: {task_snapshot.objective}\n"
        f"Target path: {task_snapshot.target_path}\n"
        f"Target kind: {task_snapshot.target_kind}\n"
        f"Surface type: {task_snapshot.surface_type}\n"
        f"Objective signals: {', '.join(task_snapshot.objective_signals) if task_snapshot.objective_signals else 'none'}\n"
        f"Children upper bound: {limit}\n"
        f"Search upper bounds: beam={upper_bounds.get('beam_width')}, rounds={upper_bounds.get('max_rounds')}, children={upper_bounds.get('children_per_expansion')}\n"
        f"Parent node: {parent.id}\n"
        f"Current best score: {root.best_score}\n"
        "Rules:\n"
        "- Consume facts, rules, history, and telemetry to decide the next high-level direction.\n"
        "- Read the sensing verifier first; start from its verdict, evidence, regressions, and uncertainty before asking for more diagnostics.\n"
        "- Freeze the decision for the executor: scope, branch identities, budget hints, and stop conditions.\n"
        "- Do not exceed the allowed edit surface or allowed scopes from the payload.\n"
        "- Prefer distinct branches before close variants, but do not force diversity when the evidence is strong.\n"
        "- Use idea_id to group multiple implementation variants of the same natural-language optimization idea.\n"
        "- Match the planner handoff mode: idea=idea only, variant=short implementation variant, detailed=variant plus brief implementation steps.\n"
        "- Record only the key high-level decisions that the executor must not reopen.\n"
        f"{chr(10).join(facts_summary)}\n"
    )


def build_planner_telemetry(
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
            "target_kind": task_snapshot.target_kind,
            "surface_type": task_snapshot.surface_type,
            "objective_signals": task_snapshot.objective_signals,
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
    editable_paths = node.planner_context.get("effective_editable_paths", task_snapshot.editable_paths)
    if not isinstance(editable_paths, list):
        editable_paths = task_snapshot.editable_paths
    return {
        "action": "execute",
        "task": {
            "objective": task_snapshot.objective,
            "target_kind": task_snapshot.target_kind,
            "surface_type": task_snapshot.surface_type,
            "objective_signals": task_snapshot.objective_signals,
            "editable_paths": editable_paths,
            "constraints": task_snapshot.constraints,
            "reference_paths": task_snapshot.reference_paths,
        },
        "root_memory": summarize_root_memory(root),
        "parent": summarize_node(parent),
        "node": summarize_node(node),
        "sandbox_path": node.sandbox_path,
        "candidate_path": node.candidate_path,
        "planner_context": node.planner_context,
        "effective_scope": node.planner_context.get("effective_scope", node.planner_context.get("recommended_scope")),
        "focus_target": node.planner_context.get("focus_target"),
        "scope_summary": node.planner_context.get("scope_summary", ""),
        "scope_context_files": node.planner_context.get("scope_context_files", []),
        "local_iteration": node.planner_context.get("local_iteration"),
        "local_iteration_limit": node.planner_context.get("local_iteration_limit"),
        "current_branch_best_score": node.planner_context.get("current_branch_best_score"),
        "local_history": node.planner_context.get("local_history", []),
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
    editable_paths = node.planner_context.get("effective_editable_paths", task_snapshot.editable_paths)
    if not isinstance(editable_paths, list):
        editable_paths = task_snapshot.editable_paths
    scope_context_files = node.planner_context.get("scope_context_files", [])
    if not isinstance(scope_context_files, list):
        scope_context_files = []
    implementation_steps = node.hypothesis.implementation_steps
    steps_text = "; ".join(str(item) for item in implementation_steps) if implementation_steps else "none"
    return (
        "Execute one local edit step inside the current branch-local optimization loop.\n"
        "Return JSON only via TREE_OUTPUT_PATH.\n"
        f"{_telemetry_guidance(rich=False)}"
        f"{_sensing_guidance(allow_probes=True)}"
        f"Task objective: {task_snapshot.objective}\n"
        f"Target kind: {task_snapshot.target_kind}\n"
        f"Surface type: {task_snapshot.surface_type}\n"
        f"Objective signals: {', '.join(task_snapshot.objective_signals) if task_snapshot.objective_signals else 'none'}\n"
        f"Editable surface entries: {', '.join(str(item) for item in editable_paths)}\n"
        f"Candidate path: {node.candidate_path}\n"
        f"Sandbox path: {node.sandbox_path}\n"
        f"Parent node: {parent.id}\n"
        f"Node hypothesis: {node.hypothesis.core_hypothesis}\n"
        f"Idea id: {node.hypothesis.idea_id or 'none'}\n"
        f"Implementation variant: {node.hypothesis.implementation_variant or 'none'}\n"
        f"Implementation steps: {steps_text}\n"
        f"Branch scope: {node.hypothesis.scope or node.planner_context.get('branch_scope', 'unspecified')}\n"
        f"Branch target: {node.hypothesis.target or node.planner_context.get('branch_target', task_snapshot.target_path)}\n"
        f"Mechanism family: {node.hypothesis.family}\n"
        f"High-level rationale: {node.hypothesis.rationale}\n"
        f"Target signals: {', '.join(node.hypothesis.target_signals) if node.hypothesis.target_signals else 'none'}\n"
        f"Verification intent: {', '.join(node.hypothesis.verification_intent) if node.hypothesis.verification_intent else 'none'}\n"
        f"Disallowed directions: {', '.join(node.hypothesis.disallowed_directions) if node.hypothesis.disallowed_directions else 'none'}\n"
        f"Local iteration: {node.planner_context.get('local_iteration', 1)} / {node.planner_context.get('local_iteration_limit', 1)}\n"
        f"Current branch best score: {node.planner_context.get('current_branch_best_score', 'none')}\n"
        f"Recent local history entries: {len(node.planner_context.get('local_history', []))}\n"
        f"Effective scope: {node.planner_context.get('effective_scope', node.planner_context.get('recommended_scope', 'unspecified'))}\n"
        f"Focus target: {node.planner_context.get('focus_target', task_snapshot.target_path)}\n"
        f"Scope summary: {node.planner_context.get('scope_summary', 'none')}\n"
        f"Scope context files: {', '.join(str(item) for item in scope_context_files) if scope_context_files else 'none'}\n"
        f"Stop conditions: {', '.join(node.planner_context.get('stop_conditions', [])) if node.planner_context.get('stop_conditions') else 'none'}\n"
        f"Frozen decisions: {', '.join(node.planner_context.get('frozen_decisions', [])) if node.planner_context.get('frozen_decisions') else 'none'}\n"
        "Rules:\n"
        "- Only modify allowed editable files inside the sandbox.\n"
        "- Keep this step aligned with the current branch identity; do not reopen the high-level route.\n"
        "- This step is part of a keep/revert local optimization loop, so make one focused edit that can be evaluated immediately.\n"
        "- Do not edit benchmark / verifier logic or other control files.\n"
        "- Stay within the mechanism family and guardrails frozen by the planner.\n"
        "- You may stop early by returning changed=false if the branch looks exhausted.\n"
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
        f"{_telemetry_guidance(rich=False)}"
        f"{_sensing_guidance(allow_probes=False)}"
        f"Task objective: {task_snapshot.objective}\n"
        f"Node id: {node.id}\n"
        f"Parent node: {parent_id}\n"
        f"Node hypothesis: {node.hypothesis.core_hypothesis}\n"
        f"Branch scope: {node.hypothesis.scope or node.planner_context.get('branch_scope', 'unspecified')}\n"
        f"Branch target: {node.hypothesis.target or node.planner_context.get('branch_target', task_snapshot.target_path)}\n"
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
    planner_packet: PlannerPacket | None = None,
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
    if planner_packet is not None:
        focus_region = None
        nearby_regions: list[dict[str, object]] = []
        if planner_packet.region_graph is not None and planner_packet.region_graph.regions:
            graph = planner_packet.region_graph
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
        telemetry["planner"] = {
            "static_prior": planner_packet.static_prior,
            "hardware_context": planner_packet.hardware_context,
            "history_context": planner_packet.history_context,
            "focus_region": focus_region,
            "nearby_regions": nearby_regions,
            "bottleneck_card": asdict(planner_packet.bottleneck_card) if planner_packet.bottleneck_card is not None else None,
            "search_policy": asdict(planner_packet.search_policy) if planner_packet.search_policy is not None else None,
            "branch_cards": [asdict(card) for card in planner_packet.branch_cards[:4]],
        }
    return telemetry
