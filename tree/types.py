from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InsightPolarity = Literal["positive", "negative", "neutral"]
NodeStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "pruned",
    "promoted",
]


@dataclass(slots=True)
class Hypothesis:
    id: str
    title: str
    core_hypothesis: str
    constraints_summary: str = ""
    editable_path_hints: list[str] = field(default_factory=list)
    family: str = "unspecified"
    rationale: str = ""
    target_signals: list[str] = field(default_factory=list)
    supporting_evidence: list[str] = field(default_factory=list)
    disallowed_directions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BottleneckCard:
    scope: str = "kernel"
    target: str = ""
    symptoms: list[str] = field(default_factory=list)
    diagnoses: list[str] = field(default_factory=list)
    response_families: list[str] = field(default_factory=list)
    avoid_families: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass(slots=True)
class RegionCard:
    id: str
    label: str
    scope: str = "kernel"
    summary: str = ""
    signals: list[str] = field(default_factory=list)
    adjacent: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RegionGraph:
    focus_region_id: str = ""
    regions: list[RegionCard] = field(default_factory=list)


@dataclass(slots=True)
class SearchPolicyHint:
    recommended_scope: str = ""
    exploration_bias: str = "balanced"
    suggested_children: int | None = None
    suggested_rounds: int | None = None
    suggested_beam_width: int | None = None
    stop_conditions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionResult:
    status: str
    summary: str
    changed: bool = False
    candidate_path: str = ""
    raw_log_paths: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EvaluationResult:
    accepted: bool
    score: float
    metrics: dict[str, Any]
    summary: str
    raw_log_paths: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Insight:
    polarity: InsightPolarity
    summary: str
    evidence: list[str] = field(default_factory=list)
    source_node_id: str = ""


@dataclass(slots=True)
class TaskSnapshot:
    task_type: str
    objective: str
    target_path: str
    editable_paths: list[str]
    constraints: list[str]
    reference_paths: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchNode:
    id: str
    parent_id: str | None
    depth: int
    hypothesis: Hypothesis
    node_dir: str = ""
    sandbox_path: str = ""
    candidate_path: str = ""
    execution_result: ExecutionResult | None = None
    evaluation_result: EvaluationResult | None = None
    insight: Insight | None = None
    status: NodeStatus = "queued"
    error: str | None = None


@dataclass(slots=True)
class RootMemory:
    objective: str
    baseline_score: float | None = None
    best_score: float | None = None
    best_node_id: str | None = None
    explored_hypotheses: list[str] = field(default_factory=list)
    positive_insights: list[str] = field(default_factory=list)
    negative_insights: list[str] = field(default_factory=list)
    neutral_insights: list[str] = field(default_factory=list)
    rounds_completed: int = 0


@dataclass(slots=True)
class SearchConfig:
    beam_width: int
    max_rounds: int
    children_per_expansion: int
    sandbox_backend: str = "copy"
    planner_command: str | None = None
    executor_command: str | None = None
    reflector_command: str | None = None
    verify_command: str | None = None
    enable_final_verify: bool = False


@dataclass(slots=True)
class SearchResult:
    run_dir: str
    best_node_id: str | None
    promoted_path: str | None
    final_verify: dict[str, Any] | None
    frontier_node_ids: list[str]
    total_nodes: int
