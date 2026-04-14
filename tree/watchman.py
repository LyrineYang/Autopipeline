from __future__ import annotations

from dataclasses import asdict, dataclass, field
import inspect
from pathlib import Path
from typing import Any

from .command import run_json_command
from .prompts import build_watchman_payload, build_watchman_prompt, build_watchman_telemetry
from .types import (
    BranchBudgetHint,
    BottleneckCard,
    Hypothesis,
    RegionCard,
    RegionGraph,
    ResourceHint,
    RootMemory,
    SearchNode,
    SearchPolicyHint,
    TaskSnapshot,
)


@dataclass(slots=True)
class WatchmanPacket:
    static_prior: dict[str, Any] = field(default_factory=dict)
    hardware_context: dict[str, Any] = field(default_factory=dict)
    benchmark_context: dict[str, Any] = field(default_factory=dict)
    history_context: dict[str, Any] = field(default_factory=dict)
    region_graph: RegionGraph | None = None
    bottleneck_card: BottleneckCard | None = None
    search_policy: SearchPolicyHint | None = None
    branch_cards: list[Hypothesis] = field(default_factory=list)
    frozen_decisions: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


class PlannerStepError(RuntimeError):
    """Raised when the planner command fails structurally."""


class Watchman:
    """High-level planning agent for branch identities and search policy."""

    def __init__(self, *, planner_command: str | None = None) -> None:
        self.planner_command = planner_command

    def build_packet(
        self,
        task: Any,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        upper_bounds: dict[str, int] | None = None,
    ) -> WatchmanPacket:
        context_provider = getattr(task, "watchman_context", None)
        if not callable(context_provider):
            return self._fallback_packet(
                task=task,
                task_snapshot=task_snapshot,
                root=root,
                parent=parent,
                raw_context={},
                limit=limit,
                upper_bounds=upper_bounds or {},
            )

        if _accepts_keyword(context_provider, "upper_bounds"):
            raw_context = context_provider(parent, root, limit, upper_bounds=upper_bounds or {})
        else:
            raw_context = context_provider(parent, root, limit)

        if self.planner_command:
            return self._planner_packet(
                task=task,
                task_snapshot=task_snapshot,
                root=root,
                parent=parent,
                raw_context=raw_context,
                limit=limit,
                upper_bounds=upper_bounds or {},
            )

        return self._fallback_packet(
            task=task,
            task_snapshot=task_snapshot,
            root=root,
            parent=parent,
            raw_context=raw_context,
            limit=limit,
            upper_bounds=upper_bounds or {},
        )

    def _planner_packet(
        self,
        *,
        task: Any,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        raw_context: dict[str, Any],
        limit: int,
        upper_bounds: dict[str, int],
    ) -> WatchmanPacket:
        prompt = build_watchman_prompt(task_snapshot, root, parent, limit, raw_context, upper_bounds)
        payload = build_watchman_payload(task_snapshot, root, parent, limit, raw_context, upper_bounds)
        telemetry = build_watchman_telemetry(task_snapshot, root, parent, raw_context, upper_bounds)
        logs_dir = Path(parent.node_dir) / "agent" / "watchman"
        cwd = Path(getattr(task, "repo_root", Path(task_snapshot.target_path).resolve().parent))
        raw = run_json_command(
            self.planner_command,
            cwd=cwd,
            prompt=prompt,
            payload=payload,
            telemetry=telemetry,
            logs_dir=logs_dir,
            error_cls=PlannerStepError,
        )
        return self._packet_from_output(
            task=task,
            task_snapshot=task_snapshot,
            root=root,
            parent=parent,
            raw_context=raw_context,
            planner_output=raw,
            limit=limit,
            upper_bounds=upper_bounds,
        )

    def _fallback_packet(
        self,
        *,
        task: Any,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        raw_context: dict[str, Any],
        limit: int,
        upper_bounds: dict[str, int],
    ) -> WatchmanPacket:
        planner_output = raw_context.get("planner_output")
        return self._packet_from_output(
            task=task,
            task_snapshot=task_snapshot,
            root=root,
            parent=parent,
            raw_context=raw_context,
            planner_output=planner_output,
            limit=limit,
            upper_bounds=upper_bounds,
        )

    def _packet_from_output(
        self,
        *,
        task: Any,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        raw_context: dict[str, Any],
        planner_output: Any,
        limit: int,
        upper_bounds: dict[str, int],
    ) -> WatchmanPacket:
        defaults = task.seed_hypotheses(parent, root, limit)
        normalized_output = planner_output if isinstance(planner_output, dict) else {}
        raw_branches = (
            {"branch_cards": planner_output}
            if isinstance(planner_output, list)
            else normalized_output or raw_context
        )
        branch_cards = self._build_branch_cards(raw_branches, limit, defaults)
        if not branch_cards:
            branch_cards = defaults
        static_prior = _dict_value(raw_context.get("static_prior"))
        if not static_prior and not raw_context:
            static_prior = {"source": "seed_hypotheses"}
        region_graph = self._build_region_graph(raw_context, task_snapshot)
        bottleneck_card = self._build_bottleneck_card(
            normalized_output or raw_context,
            task_snapshot,
            branch_cards,
            region_graph,
            raw_context,
        )
        return WatchmanPacket(
            static_prior=static_prior,
            hardware_context=_dict_value(raw_context.get("hardware_context")),
            benchmark_context=_dict_value(raw_context.get("benchmark_context")),
            history_context=_dict_value(raw_context.get("history_context")) or _history_context(root),
            region_graph=region_graph,
            bottleneck_card=bottleneck_card,
            search_policy=self._build_search_policy(
                normalized_output or raw_context,
                task_snapshot,
                branch_cards,
                limit,
                upper_bounds,
                raw_context,
            ),
            branch_cards=branch_cards,
            frozen_decisions=_string_list(
                (normalized_output or raw_context).get(
                    "frozen_decisions",
                    [
                        "Treat planner output as the frozen high-level decision layer for this branch.",
                        "Executor may adapt locally but should not reopen the high-level direction search.",
                    ],
                )
            ),
        )

    def _build_branch_cards(
        self,
        raw_context: dict[str, Any],
        limit: int,
        defaults: list[Hypothesis],
    ) -> list[Hypothesis]:
        cards: list[Hypothesis] = []
        seen_families: set[str] = set()
        raw_cards = raw_context.get("branch_cards")
        if not isinstance(raw_cards, list):
            raw_cards = raw_context.get("hypotheses")
        if not isinstance(raw_cards, list):
            raw_cards = raw_context.get("branch_families")
        if not isinstance(raw_cards, list):
            raw_cards = []

        for index, item in enumerate(raw_cards[:limit]):
            default = defaults[index] if index < len(defaults) else None
            hypothesis = _normalize_hypothesis(item, index, default=default)
            family = hypothesis.family.strip() or f"family-{index + 1}"
            if family in seen_families:
                continue
            seen_families.add(family)
            hypothesis.family = family
            cards.append(hypothesis)
        return cards

    def _build_bottleneck_card(
        self,
        raw_context: dict[str, Any],
        task_snapshot: TaskSnapshot,
        branch_cards: list[Hypothesis],
        region_graph: RegionGraph,
        planner_facts: dict[str, Any],
    ) -> BottleneckCard:
        raw = raw_context.get("bottleneck_card")
        if isinstance(raw, dict):
            return BottleneckCard(
                scope=str(raw.get("scope", "kernel")),
                target=str(raw.get("target", task_snapshot.target_path)),
                symptoms=_string_list(raw.get("symptoms")),
                diagnoses=_string_list(raw.get("diagnoses")),
                response_families=_string_list(raw.get("response_families"))
                or [card.family for card in branch_cards],
                avoid_families=_string_list(raw.get("avoid_families")),
                rationale=str(raw.get("rationale", "")),
            )

        rules = _dict_value(planner_facts.get("planner_rules"))
        focus = next(
            (region for region in region_graph.regions if region.id == region_graph.focus_region_id),
            region_graph.regions[0],
        )
        symptoms = _string_list(planner_facts.get("symptoms")) or list(focus.signals)
        diagnoses = _string_list(planner_facts.get("profile_hints"))
        if not diagnoses:
            metrics = _dict_value(raw_context.get("benchmark_context")).get("metrics", {})
            if metrics.get("bottleneck"):
                diagnoses = [str(metrics.get("bottleneck"))]
        return BottleneckCard(
            scope=_string_list(rules.get("allowed_scopes"))[0] if _string_list(rules.get("allowed_scopes")) else focus.scope or task_snapshot.task_type,
            target=focus.label or task_snapshot.target_path,
            symptoms=symptoms,
            diagnoses=diagnoses or ["unknown_bottleneck"],
            response_families=[card.family for card in branch_cards],
            avoid_families=_string_list(rules.get("avoid_families")),
            rationale=str(planner_facts.get("problem_summary", "Use the current branch cards as the high-level response set.")),
        )

    def _build_search_policy(
        self,
        raw_context: dict[str, Any],
        task_snapshot: TaskSnapshot,
        branch_cards: list[Hypothesis],
        limit: int,
        upper_bounds: dict[str, int],
        planner_facts: dict[str, Any],
    ) -> SearchPolicyHint:
        raw = raw_context.get("search_policy")
        if isinstance(raw, dict):
            raw_budget = _dict_value(raw.get("branch_budget"))
            scope = str(raw.get("recommended_scope", task_snapshot.task_type))
            return SearchPolicyHint(
                recommended_scope=scope,
                scope_options=_string_list(raw.get("scope_options")) or [scope],
                exploration_bias=str(raw.get("exploration_bias", "balanced")),
                branch_budget=BranchBudgetHint(
                    children=_optional_int(raw_budget.get("children", raw.get("suggested_children"))),
                    rounds=_optional_int(raw_budget.get("rounds", raw.get("suggested_rounds"))),
                    beam_width=_optional_int(raw_budget.get("beam_width", raw.get("suggested_beam_width"))),
                ),
                resource_hint=ResourceHint(
                    tier=str(_dict_value(raw.get("resource_hint")).get("tier", raw.get("resource_tier", "balanced"))),
                    notes=_string_list(_dict_value(raw.get("resource_hint")).get("notes", raw.get("resource_notes"))),
                ),
                stop_conditions=_string_list(raw.get("stop_conditions")),
            )

        rules = _dict_value(planner_facts.get("planner_rules"))
        allowed_scopes = _string_list(rules.get("allowed_scopes")) or [task_snapshot.task_type]
        suggested_children = min(limit, len(branch_cards)) if branch_cards else None
        return SearchPolicyHint(
            recommended_scope=allowed_scopes[0],
            scope_options=allowed_scopes,
            exploration_bias="balanced",
            branch_budget=BranchBudgetHint(
                children=suggested_children,
                beam_width=upper_bounds.get("beam_width"),
            ),
            resource_hint=ResourceHint(
                tier="balanced",
                notes=["No planner decision available; keep a neutral local search budget."],
            ),
        )

    def _build_region_graph(
        self,
        raw_context: dict[str, Any],
        task_snapshot: TaskSnapshot,
    ) -> RegionGraph:
        raw = raw_context.get("region_graph")
        if isinstance(raw, dict):
            raw_regions = raw.get("regions", [])
            regions: list[RegionCard] = []
            if isinstance(raw_regions, list):
                for index, item in enumerate(raw_regions):
                    if not isinstance(item, dict):
                        continue
                    region_id = str(item.get("id", f"region-{index + 1}"))
                    regions.append(
                        RegionCard(
                            id=region_id,
                            label=str(item.get("label", region_id)),
                            scope=str(item.get("scope", "kernel")),
                            summary=str(item.get("summary", "")),
                            signals=_string_list(item.get("signals")),
                            adjacent=_string_list(item.get("adjacent")),
                        )
                    )
            if regions:
                focus = str(raw.get("focus_region_id", regions[0].id))
                return RegionGraph(focus_region_id=focus, regions=regions)

        return RegionGraph(
            focus_region_id="current-target",
            regions=[
                RegionCard(
                    id="current-target",
                    label=task_snapshot.target_path,
                    scope=task_snapshot.task_type,
                    summary="Use the current target as the active optimization region.",
                )
            ],
        )


def _history_context(root: RootMemory) -> dict[str, Any]:
    return {
        "best_score": root.best_score,
        "best_node_id": root.best_node_id,
        "rounds_completed": root.rounds_completed,
        "explored_hypotheses": root.explored_hypotheses[-8:],
        "positive_insights": root.positive_insights[-5:],
        "negative_insights": root.negative_insights[-5:],
        "neutral_insights": root.neutral_insights[-5:],
    }


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _string_list_or_default(item: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = item.get(key)
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(entry) for entry in value]
    return list(default)


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
        raise PlannerStepError("Each planned branch must be a JSON object or string.")

    hints = item.get("editable_path_hints")
    if hints is not None and not isinstance(hints, list):
        raise PlannerStepError("editable_path_hints must be a JSON list when present.")

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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _accepts_keyword(func: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters
