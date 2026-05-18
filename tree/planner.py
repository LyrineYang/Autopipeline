from __future__ import annotations

from dataclasses import asdict, dataclass, field
import inspect
from pathlib import Path
from typing import Any

from .agent_backend import AgentRunRequest, role_uses_app_server, run_agent_role
from .codex_session import registry_path_for_node_dir
from .prompts import build_planner_payload, build_planner_prompt, build_planner_telemetry
from .roles import normalize_role_configs, role_config_for
from .sensing import SensingRuntime
from .types import (
    AgentRoleConfig,
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
class PlannerPacket:
    static_prior: dict[str, Any] = field(default_factory=dict)
    hardware_context: dict[str, Any] = field(default_factory=dict)
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


class Planner:
    """High-level planning agent for branch identities and search policy."""

    def __init__(
        self,
        *,
        planner_command: str | None = None,
        command_timeout_seconds: float | None = 600,
        budget_ledger: Any | None = None,
        planner_handoff_mode: str = "variant",
        role_configs: dict[str, AgentRoleConfig] | None = None,
        motif_archive: Any | None = None,
    ) -> None:
        self.planner_command = planner_command
        self.command_timeout_seconds = command_timeout_seconds
        self.budget_ledger = budget_ledger
        self.planner_handoff_mode = _normalize_planner_handoff_mode(planner_handoff_mode)
        self.role_configs = normalize_role_configs(role_configs)
        self.motif_archive = motif_archive

    def build_packet(
        self,
        task: Any,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        upper_bounds: dict[str, int] | None = None,
    ) -> PlannerPacket:
        context_provider = getattr(task, "planner_context", None)
        if not callable(context_provider):
            return self._fallback_packet(
                task=task,
                task_snapshot=task_snapshot,
                root=root,
                parent=parent,
                raw_context=self._prepare_context({}),
                limit=limit,
                upper_bounds=upper_bounds or {},
            )

        if _accepts_keyword(context_provider, "upper_bounds"):
            raw_context = context_provider(parent, root, limit, upper_bounds=upper_bounds or {})
        else:
            raw_context = context_provider(parent, root, limit)
        raw_context = self._prepare_context(raw_context)

        role_config = role_config_for(self.role_configs, "planner")
        command = role_config.command or self.planner_command
        if command or role_uses_app_server(role_config, command):
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
    ) -> PlannerPacket:
        if self.budget_ledger is not None and not self.budget_ledger.can_start_agent_step():
            return self._fallback_packet(
                task=task,
                task_snapshot=task_snapshot,
                root=root,
                parent=parent,
                raw_context=raw_context,
                limit=limit,
                upper_bounds=upper_bounds,
            )
        prompt = build_planner_prompt(task_snapshot, root, parent, limit, raw_context, upper_bounds)
        payload = build_planner_payload(task_snapshot, root, parent, limit, raw_context, upper_bounds)
        telemetry = build_planner_telemetry(task_snapshot, root, parent, raw_context, upper_bounds)
        logs_dir = Path(parent.node_dir) / "agent" / "planner"
        cwd = Path(getattr(task, "repo_root", Path(task_snapshot.target_path).resolve().parent))
        sensing_cwd = Path(parent.sandbox_path) if parent.sandbox_path else cwd
        sensing = SensingRuntime(
            base_dir=Path(parent.node_dir),
            cwd=sensing_cwd,
            task_snapshot=task_snapshot,
            root=root,
            parent=parent,
            task=task,
            budget_ledger=self.budget_ledger,
        )
        role_config = role_config_for(self.role_configs, "planner")
        command = role_config.command or self.planner_command
        use_app_server = role_uses_app_server(role_config, command)
        raw = run_agent_role(
            AgentRunRequest(
                command=command,
                cwd=cwd,
                prompt=prompt,
                payload=payload,
                telemetry=telemetry,
                logs_dir=logs_dir,
                action="plan",
                role="planner",
                role_config=role_config,
                sensing=sensing,
                timeout_seconds=self.command_timeout_seconds,
                budget_ledger=self.budget_ledger,
                session_key=f"{parent.id}:planner",
                session_registry_path=registry_path_for_node_dir(Path(parent.node_dir)),
                output_mode="json_message" if use_app_server else "json_file",
                error_cls=PlannerStepError,
            )
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
    ) -> PlannerPacket:
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
    ) -> PlannerPacket:
        defaults = task.seed_hypotheses(parent, root, limit)
        normalized_output = planner_output if isinstance(planner_output, dict) else {}
        planner_rules = _dict_value(raw_context.get("planner_rules"))
        raw_branches = (
            {"branch_cards": planner_output}
            if isinstance(planner_output, list)
            else normalized_output or raw_context
        )
        branch_cards = self._build_branch_cards(raw_branches, limit, defaults, planner_rules)
        if not branch_cards:
            branch_cards = self._build_branch_cards(
                {"branch_cards": defaults},
                limit,
                defaults,
                planner_rules,
            ) or defaults
        branch_cards = [self._apply_handoff_mode(card) for card in branch_cards]
        static_prior = _dict_value(raw_context.get("static_prior"))
        if not static_prior and not _has_task_context(raw_context):
            static_prior = {"source": "seed_hypotheses"}
        region_graph = self._build_region_graph(raw_context, task_snapshot)
        bottleneck_card = self._build_bottleneck_card(
            normalized_output or raw_context,
            task_snapshot,
            branch_cards,
            region_graph,
            raw_context,
        )
        return PlannerPacket(
            static_prior=static_prior,
            hardware_context=_dict_value(raw_context.get("hardware_context")),
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
                normalized_output.get(
                    "frozen_decisions",
                    _string_list(raw_context.get("frozen_decisions"))
                    or [
                        "Executor may adapt locally but should not reopen the high-level direction search.",
                    ],
                )
            ),
        )

    def _prepare_context(self, raw_context: Any) -> dict[str, Any]:
        context = _with_budget_context(raw_context, self.budget_ledger)
        prior_builder = getattr(self.motif_archive, "planner_prior", None)
        if callable(prior_builder):
            context = {**context, "motif_prior": prior_builder()}
        return {**context, "planner_handoff_mode": self.planner_handoff_mode}

    def _planner_command(self) -> str | None:
        role_config = role_config_for(self.role_configs, "planner")
        return role_config.command or self.planner_command

    def _apply_handoff_mode(self, hypothesis: Hypothesis) -> Hypothesis:
        if self.planner_handoff_mode == "detailed":
            return hypothesis
        if self.planner_handoff_mode == "variant":
            hypothesis.implementation_steps = []
            return hypothesis
        hypothesis.implementation_variant = ""
        hypothesis.implementation_steps = []
        return hypothesis

    def _build_branch_cards(
        self,
        raw_context: dict[str, Any],
        limit: int,
        defaults: list[Hypothesis],
        planner_rules: dict[str, Any],
    ) -> list[Hypothesis]:
        cards: list[Hypothesis] = []
        seen_branch_keys: set[str] = set()
        allowed_families = set(_string_list(planner_rules.get("allowed_families")))
        allowed_scopes = _string_list(planner_rules.get("allowed_scopes"))
        editable_paths = set(_string_list(planner_rules.get("editable_paths")))
        raw_cards = raw_context.get("branch_cards")
        if not isinstance(raw_cards, list):
            raw_cards = raw_context.get("hypotheses")
        if not isinstance(raw_cards, list):
            raw_cards = []

        for index, item in enumerate(raw_cards[:limit]):
            default = defaults[index] if index < len(defaults) else None
            hypothesis = _normalize_hypothesis(item, index, default=default)
            family = hypothesis.family.strip() or f"family-{index + 1}"
            if allowed_families and family not in allowed_families and family != "unspecified":
                continue
            if allowed_scopes:
                scope = hypothesis.scope.strip() or (default.scope if default is not None else "") or allowed_scopes[0]
                if scope not in allowed_scopes:
                    scope = allowed_scopes[0]
                hypothesis.scope = scope
            dedupe_key = (
                hypothesis.id.strip()
                or (
                    f"{hypothesis.scope.strip()}|{hypothesis.target.strip()}|"
                    f"{hypothesis.core_hypothesis.strip()}|{hypothesis.implementation_variant.strip()}"
                )
                or hypothesis.title.strip()
                or f"branch-{index + 1}"
            )
            if dedupe_key in seen_branch_keys:
                continue
            if editable_paths:
                hints = [hint for hint in hypothesis.editable_path_hints if hint in editable_paths]
                if not hints:
                    hints = [hint for hint in (default.editable_path_hints if default is not None else []) if hint in editable_paths]
                hypothesis.editable_path_hints = hints or sorted(editable_paths)
            seen_branch_keys.add(dedupe_key)
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
        rules = _dict_value(planner_facts.get("planner_rules"))
        allowed_scopes = _string_list(rules.get("allowed_scopes"))
        if isinstance(raw, dict):
            scope = str(raw.get("scope", allowed_scopes[0] if allowed_scopes else "kernel"))
            if allowed_scopes and scope not in allowed_scopes:
                scope = allowed_scopes[0]
            return BottleneckCard(
                scope=scope,
                target=str(raw.get("target", task_snapshot.target_path)),
                symptoms=_string_list(raw.get("symptoms")),
                diagnoses=_string_list(raw.get("diagnoses")),
                response_families=_string_list(raw.get("response_families"))
                or [card.family for card in branch_cards],
                avoid_families=_string_list(raw.get("avoid_families")),
                rationale=str(raw.get("rationale", "")),
            )

        focus = next(
            (region for region in region_graph.regions if region.id == region_graph.focus_region_id),
            region_graph.regions[0],
        )
        symptoms = _string_list(planner_facts.get("symptoms")) or list(focus.signals)
        return BottleneckCard(
            scope=allowed_scopes[0] if allowed_scopes else focus.scope or task_snapshot.task_type,
            target=focus.label or task_snapshot.target_path,
            symptoms=symptoms,
            diagnoses=[],
            response_families=[card.family for card in branch_cards],
            avoid_families=[],
            rationale="",
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
        rules = _dict_value(planner_facts.get("planner_rules"))
        allowed_scopes = _string_list(rules.get("allowed_scopes")) or [task_snapshot.task_type]
        if isinstance(raw, dict):
            raw_budget = _dict_value(raw.get("branch_budget"))
            scope = str(raw.get("recommended_scope", allowed_scopes[0]))
            if scope not in allowed_scopes:
                scope = allowed_scopes[0]
            scope_options = [item for item in _string_list(raw.get("scope_options")) if item in allowed_scopes]
            return SearchPolicyHint(
                recommended_scope=scope,
                scope_options=scope_options or [scope],
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

        return SearchPolicyHint(
            recommended_scope=allowed_scopes[0],
            scope_options=allowed_scopes,
            exploration_bias="balanced",
            branch_budget=BranchBudgetHint(),
            resource_hint=ResourceHint(),
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


def _with_budget_context(raw_context: Any, budget_ledger: Any | None) -> dict[str, Any]:
    context = raw_context if isinstance(raw_context, dict) else {}
    if budget_ledger is None:
        return context
    summary_builder = getattr(budget_ledger, "planner_context", None)
    summary = summary_builder() if callable(summary_builder) else budget_ledger.summary()
    return {
        **context,
        "budget_context": summary,
        "history_context": {
            **_dict_value(context.get("history_context")),
            "budget_context": summary,
        },
    }


def _normalize_planner_handoff_mode(value: str) -> str:
    mode = str(value or "variant").strip().lower()
    return mode if mode in {"idea", "variant", "detailed"} else "variant"


def _has_task_context(raw_context: dict[str, Any]) -> bool:
    keys = set(raw_context)
    keys.discard("planner_handoff_mode")
    keys.discard("budget_context")
    motif_prior = raw_context.get("motif_prior")
    if isinstance(motif_prior, dict) and not any(motif_prior.values()):
        keys.discard("motif_prior")
    if keys == {"history_context"}:
        history = _dict_value(raw_context.get("history_context"))
        if set(history) <= {"budget_context"}:
            keys.clear()
    return bool(keys)


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
    if isinstance(item, Hypothesis):
        return Hypothesis(
            id=item.id,
            title=item.title,
            core_hypothesis=item.core_hypothesis,
            idea_id=item.idea_id,
            implementation_variant=item.implementation_variant,
            implementation_steps=list(item.implementation_steps),
            scope=item.scope,
            target=item.target,
            constraints_summary=item.constraints_summary,
            editable_path_hints=list(item.editable_path_hints),
            family=item.family,
            rationale=item.rationale,
            target_signals=list(item.target_signals),
            verification_intent=list(item.verification_intent),
            supporting_evidence=list(item.supporting_evidence),
            disallowed_directions=list(item.disallowed_directions),
        )

    if isinstance(item, str):
        return Hypothesis(
            id=default.id if default is not None else f"hypothesis-{index + 1}",
            title=default.title if default is not None else f"Hypothesis {index + 1}",
            core_hypothesis=item.strip(),
            idea_id=default.idea_id if default is not None else "",
            implementation_variant=default.implementation_variant if default is not None else "",
            implementation_steps=list(default.implementation_steps) if default is not None else [],
            scope=default.scope if default is not None else "",
            target=default.target if default is not None else "",
            constraints_summary=default.constraints_summary if default is not None else "",
            editable_path_hints=list(default.editable_path_hints) if default is not None else [],
            family=default.family if default is not None else "unspecified",
            rationale=default.rationale if default is not None else "",
            target_signals=list(default.target_signals) if default is not None else [],
            verification_intent=list(default.verification_intent) if default is not None else [],
            supporting_evidence=list(default.supporting_evidence) if default is not None else [],
            disallowed_directions=list(default.disallowed_directions) if default is not None else [],
        )

    if not isinstance(item, dict):
        raise PlannerStepError("Each planned branch must be a JSON object or string.")

    hints = item.get("editable_path_hints")
    if hints is not None and not isinstance(hints, list):
        raise PlannerStepError("editable_path_hints must be a JSON list when present.")
    implementation_steps = item.get("implementation_steps")
    if implementation_steps is not None and not isinstance(implementation_steps, list):
        raise PlannerStepError("implementation_steps must be a JSON list when present.")

    return Hypothesis(
        id=str(item.get("id", default.id if default is not None else f"hypothesis-{index + 1}")),
        title=str(item.get("title", default.title if default is not None else f"Hypothesis {index + 1}")),
        core_hypothesis=str(
            item.get("core_hypothesis", default.core_hypothesis if default is not None else "")
        ).strip(),
        idea_id=str(item.get("idea_id", default.idea_id if default is not None else "")),
        implementation_variant=str(
            item.get(
                "implementation_variant",
                default.implementation_variant if default is not None else "",
            )
        ),
        implementation_steps=(
            [str(step) for step in implementation_steps]
            if implementation_steps is not None
            else list(default.implementation_steps) if default is not None else []
        ),
        scope=str(item.get("scope", default.scope if default is not None else "")),
        target=str(item.get("target", default.target if default is not None else "")),
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
        verification_intent=_string_list_or_default(
            item,
            "verification_intent",
            default.verification_intent if default is not None else [],
        ),
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
