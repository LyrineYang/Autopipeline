from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .types import (
    BottleneckCard,
    Hypothesis,
    RegionCard,
    RegionGraph,
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


class Watchman:
    """High-level search-prior builder for branch identities."""

    def build_packet(
        self,
        task: Any,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
    ) -> WatchmanPacket:
        context_provider = getattr(task, "watchman_context", None)
        if not callable(context_provider):
            seed_cards = task.seed_hypotheses(parent, root, limit)
            return WatchmanPacket(
                static_prior={
                    "source": "seed_hypotheses",
                    "task_type": task_snapshot.task_type,
                },
                history_context=_history_context(root),
                region_graph=RegionGraph(
                    focus_region_id="current-target",
                    regions=[
                        RegionCard(
                            id="current-target",
                            label=task_snapshot.target_path,
                            scope=task_snapshot.task_type,
                            summary="No task-specific region context is available.",
                        )
                    ],
                ),
                bottleneck_card=BottleneckCard(
                    scope=task_snapshot.task_type,
                    target=task_snapshot.target_path,
                    diagnoses=["unknown_bottleneck"],
                    response_families=[card.family for card in seed_cards],
                    rationale="No task-specific watchman context is available.",
                ),
                search_policy=SearchPolicyHint(
                    recommended_scope=task_snapshot.task_type,
                    exploration_bias="balanced",
                ),
                branch_cards=seed_cards,
                frozen_decisions=[
                    "No task-specific watchman context is available.",
                    "Use task.seed_hypotheses as the high-level branch source.",
                ],
            )

        raw_context = context_provider(parent, root, limit)
        branch_cards = self._build_branch_cards(raw_context, limit)
        if not branch_cards:
            branch_cards = task.seed_hypotheses(parent, root, limit)

        return WatchmanPacket(
            static_prior=_dict_value(raw_context.get("static_prior")),
            hardware_context=_dict_value(raw_context.get("hardware_context")),
            benchmark_context=_dict_value(raw_context.get("benchmark_context")),
            history_context=_dict_value(raw_context.get("history_context")) or _history_context(root),
            region_graph=self._build_region_graph(raw_context, task_snapshot),
            bottleneck_card=self._build_bottleneck_card(raw_context, task_snapshot, branch_cards),
            search_policy=self._build_search_policy(raw_context, task_snapshot, branch_cards, limit),
            branch_cards=branch_cards,
            frozen_decisions=_string_list(
                raw_context.get(
                    "frozen_decisions",
                    [
                        "Keep one branch per mechanism family before adding variants.",
                        "Treat branch cards as high-level decision boundaries for executor behavior.",
                    ],
                )
            ),
        )

    def _build_branch_cards(self, raw_context: dict[str, Any], limit: int) -> list[Hypothesis]:
        cards: list[Hypothesis] = []
        seen_families: set[str] = set()
        for index, item in enumerate(raw_context.get("branch_families", [])):
            if not isinstance(item, dict):
                continue
            family = str(item.get("family", f"family-{index + 1}")).strip() or f"family-{index + 1}"
            if family in seen_families:
                continue
            seen_families.add(family)
            cards.append(
                Hypothesis(
                    id=str(item.get("id", family)),
                    title=str(item.get("title", family.replace("_", " ").title())),
                    core_hypothesis=str(item.get("core_hypothesis", "")).strip(),
                    constraints_summary=str(item.get("constraints_summary", "")),
                    editable_path_hints=_string_list(item.get("editable_path_hints", ["kernel.py"])),
                    family=family,
                    rationale=str(item.get("rationale", "")),
                    target_signals=_string_list(item.get("target_signals", [])),
                    supporting_evidence=_string_list(item.get("supporting_evidence", [])),
                    disallowed_directions=_string_list(item.get("disallowed_directions", [])),
                )
            )
            if len(cards) >= limit:
                break
        return cards

    def _build_bottleneck_card(
        self,
        raw_context: dict[str, Any],
        task_snapshot: TaskSnapshot,
        branch_cards: list[Hypothesis],
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

        metrics = _dict_value(raw_context.get("benchmark_context")).get("metrics", {})
        diagnosis = str(metrics.get("bottleneck", "unknown_bottleneck"))
        return BottleneckCard(
            scope=task_snapshot.task_type,
            target=task_snapshot.target_path,
            diagnoses=[diagnosis],
            response_families=[card.family for card in branch_cards],
            rationale="Use the current watchman branch cards as the recommended response families.",
        )

    def _build_search_policy(
        self,
        raw_context: dict[str, Any],
        task_snapshot: TaskSnapshot,
        branch_cards: list[Hypothesis],
        limit: int,
    ) -> SearchPolicyHint:
        raw = raw_context.get("search_policy")
        if isinstance(raw, dict):
            return SearchPolicyHint(
                recommended_scope=str(raw.get("recommended_scope", task_snapshot.task_type)),
                exploration_bias=str(raw.get("exploration_bias", "balanced")),
                suggested_children=_optional_int(raw.get("suggested_children")),
                suggested_rounds=_optional_int(raw.get("suggested_rounds")),
                suggested_beam_width=_optional_int(raw.get("suggested_beam_width")),
                stop_conditions=_string_list(raw.get("stop_conditions")),
            )

        suggested_children = min(limit, len(branch_cards)) if branch_cards else None
        return SearchPolicyHint(
            recommended_scope=task_snapshot.task_type,
            exploration_bias="balanced",
            suggested_children=suggested_children,
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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
