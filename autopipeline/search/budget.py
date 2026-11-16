from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any

from .types import SearchNode


@dataclass(slots=True)
class BudgetLedger:
    max_agent_calls: int | None = None
    max_probe_calls: int | None = None
    agent_calls: int = 0
    api_calls: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    agent_seconds: float = 0.0
    command_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    role_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    probe_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    family_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def can_start_agent_step(self) -> bool:
        with self._lock:
            return self.max_agent_calls is None or self.agent_calls < self.max_agent_calls

    def can_start_probe(self) -> bool:
        with self._lock:
            return self.max_probe_calls is None or self.probe_calls < self.max_probe_calls

    @property
    def probe_calls(self) -> int:
        with self._lock:
            return sum(int(item.get("calls", 0)) for item in self.probe_stats.values())

    def remaining_agent_calls(self) -> int | None:
        with self._lock:
            if self.max_agent_calls is None:
                return None
            return max(0, self.max_agent_calls - self.agent_calls)

    def remaining_probe_calls(self) -> int | None:
        with self._lock:
            if self.max_probe_calls is None:
                return None
            return max(0, self.max_probe_calls - self.probe_calls)

    def record_command(
        self,
        *,
        action: str,
        status: str,
        duration_seconds: float,
        output: Any = None,
        role: str | None = None,
        failure_class: str = "",
    ) -> None:
        with self._lock:
            action_key = action or "agent"
            role_key = role or action_key
            usage = _extract_usage(output)
            self._record_command_stats(
                self.command_stats,
                action_key,
                status=status,
                duration_seconds=duration_seconds,
                usage=usage,
                failure_class=failure_class,
            )
            self._record_command_stats(
                self.role_stats,
                role_key,
                status=status,
                duration_seconds=duration_seconds,
                usage=usage,
                failure_class=failure_class,
            )

            self.agent_calls += 1
            self.agent_seconds = round(self.agent_seconds + duration_seconds, 3)
            self.api_calls += int(usage.get("api_calls", 1))
            self.total_tokens += int(usage.get("total_tokens", 0))
            self.prompt_tokens += int(usage.get("prompt_tokens", 0))
            self.completion_tokens += int(usage.get("completion_tokens", 0))
            self.cost_usd = round(self.cost_usd + float(usage.get("cost_usd", 0.0)), 6)
            self._append_recent(
                {
                    "kind": "agent_command",
                    "action": action_key,
                    "role": role_key,
                    "status": status,
                    "failure_class": failure_class,
                    "duration_seconds": duration_seconds,
                }
            )

    def record_probe(self, *, action: str, result: dict[str, Any]) -> None:
        with self._lock:
            if result.get("budget_rejected"):
                return
            kind = str(result.get("kind") or "unknown")
            stats = self.probe_stats.setdefault(
                kind,
                {"calls": 0, "failures": 0, "rejected": 0, "useful_signals": 0, "seconds": 0.0},
            )
            status = str(result.get("status", ""))
            stats["calls"] += 1
            stats["seconds"] = round(
                float(stats["seconds"]) + float(result.get("duration_seconds") or 0.0),
                3,
            )
            if status == "failed":
                stats["failures"] += 1
            if status == "rejected":
                stats["rejected"] += 1
            stats["useful_signals"] += _count_useful_signals(result.get("data"))
            self._append_recent(
                {
                    "kind": "probe",
                    "action": action,
                    "probe": kind,
                    "status": status,
                }
            )

    def record_node(self, *, node: SearchNode, parent: SearchNode | None) -> None:
        with self._lock:
            if node.hypothesis.id == "baseline":
                return
            family = node.hypothesis.family.strip() or "unspecified"
            stats = self.family_stats.setdefault(
                family,
                {
                    "attempts": 0,
                    "accepted": 0,
                    "rejected": 0,
                    "failed": 0,
                    "compile_failures": 0,
                    "score_delta_total": 0.0,
                },
            )
            stats["attempts"] += 1
            accepted = bool(node.evaluation_result and node.evaluation_result.accepted)
            if node.status == "failed":
                stats["failed"] += 1
            elif accepted:
                stats["accepted"] += 1
            else:
                stats["rejected"] += 1
            if _looks_like_compile_failure(node):
                stats["compile_failures"] += 1

            if node.evaluation_result is not None and parent is not None and parent.evaluation_result is not None:
                stats["score_delta_total"] = round(
                    float(stats["score_delta_total"])
                    + (node.evaluation_result.score - parent.evaluation_result.score),
                    6,
                )
            self._append_recent(
                {
                    "kind": "node",
                    "node_id": node.id,
                    "family": family,
                    "status": node.status,
                    "accepted": accepted,
                }
            )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            family_stats = {
                family: {
                    **stats,
                    "avg_score_delta": _average_delta(stats),
                }
                for family, stats in sorted(self.family_stats.items())
            }
            return {
                "agent_usage": {
                    "agent_calls": self.agent_calls,
                    "api_calls": self.api_calls,
                    "total_tokens": self.total_tokens,
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "cost_usd": self.cost_usd,
                    "agent_seconds": self.agent_seconds,
                    "remaining_agent_calls": self.remaining_agent_calls(),
                },
                "probe_usage": {
                    "probe_calls": self.probe_calls,
                    "remaining_probe_calls": self.remaining_probe_calls(),
                },
                "command_stats": self.command_stats,
                "role_stats": self.role_stats,
                "probe_stats": self.probe_stats,
                "family_stats": family_stats,
                "recommendations": self._recommendations(family_stats),
                "recent_events": list(self.recent_events),
            }

    def planner_context(self) -> dict[str, Any]:
        with self._lock:
            family_items = list(sorted(self.family_stats.items()))
            probe_items = list(sorted(self.probe_stats.items()))
            agent_usage = {
                "agent_calls": self.agent_calls,
                "api_calls": self.api_calls,
                "total_tokens": self.total_tokens,
                "cost_usd": self.cost_usd,
                "remaining_agent_calls": self.remaining_agent_calls(),
            }
            probe_usage = {
                "probe_calls": self.probe_calls,
                "remaining_probe_calls": self.remaining_probe_calls(),
            }
            role_stats = dict(self.role_stats)

        low_yield_families: list[str] = []
        compile_cooldown_families: list[str] = []
        for family, stats in family_items:
            attempts = int(stats.get("attempts", 0))
            accepted = int(stats.get("accepted", 0))
            compile_failures = int(stats.get("compile_failures", 0))
            if attempts >= 2 and accepted == 0:
                low_yield_families.append(family)
            if compile_failures >= 2:
                compile_cooldown_families.append(family)

        low_value_probes: list[str] = []
        high_failure_probes: list[str] = []
        for kind, stats in probe_items:
            calls = int(stats.get("calls", 0))
            useful = int(stats.get("useful_signals", 0))
            failures = int(stats.get("failures", 0))
            if calls >= 2 and useful == 0:
                low_value_probes.append(kind)
            if calls >= 2 and failures / max(calls, 1) >= 0.5:
                high_failure_probes.append(kind)

        return {
            "agent_usage": {
                **agent_usage,
            },
            "probe_usage": probe_usage,
            "role_stats": role_stats,
            "family_guidance": {
                "low_yield": low_yield_families[:4],
                "compile_cooldown": compile_cooldown_families[:4],
            },
            "probe_guidance": {
                "low_value": low_value_probes[:4],
                "high_failure": high_failure_probes[:4],
            },
            "recommendations": self._recommendations(
                {
                    family: {
                        **stats,
                        "avg_score_delta": _average_delta(stats),
                    }
                    for family, stats in family_items
                }
            )[:4],
        }

    def _record_command_stats(
        self,
        bucket: dict[str, dict[str, Any]],
        key: str,
        *,
        status: str,
        duration_seconds: float,
        usage: dict[str, Any],
        failure_class: str,
    ) -> None:
        stats = bucket.setdefault(
            key,
            {
                "calls": 0,
                "failures": 0,
                "timeouts": 0,
                "cancelled": 0,
                "seconds": 0.0,
                "api_calls": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "failure_classes": {},
            },
        )
        stats["calls"] += 1
        stats["seconds"] = round(float(stats["seconds"]) + duration_seconds, 3)
        stats["api_calls"] += int(usage.get("api_calls", 1))
        stats["total_tokens"] += int(usage.get("total_tokens", 0))
        stats["cost_usd"] = round(float(stats["cost_usd"]) + float(usage.get("cost_usd", 0.0)), 6)
        if status != "completed":
            stats["failures"] += 1
        if status == "timed_out":
            stats["timeouts"] += 1
        if status == "cancelled":
            stats["cancelled"] += 1
        if failure_class:
            classes = stats["failure_classes"]
            classes[failure_class] = int(classes.get(failure_class, 0)) + 1

    def _recommendations(self, family_stats: dict[str, dict[str, Any]]) -> list[str]:
        recommendations: list[str] = []
        if self.remaining_agent_calls() == 0:
            recommendations.append("agent call budget exhausted; stop launching agent-backed branches")
        elif self.remaining_agent_calls() is not None and self.remaining_agent_calls() <= 2:
            recommendations.append("agent call budget is low; prefer the highest-confidence branch only")
        if self.remaining_probe_calls() == 0:
            recommendations.append("probe budget exhausted; rely on existing sensing evidence")

        for family, stats in family_stats.items():
            attempts = int(stats.get("attempts", 0))
            accepted = int(stats.get("accepted", 0))
            compile_failures = int(stats.get("compile_failures", 0))
            if attempts >= 2 and accepted == 0:
                recommendations.append(f"reduce {family} branches; recent attempts produced no accepted result")
            if compile_failures >= 2:
                recommendations.append(f"cool down {family}; compile failures are recurring")

        with self._lock:
            probe_items = list(sorted(self.probe_stats.items()))
        for kind, stats in probe_items:
            calls = int(stats.get("calls", 0))
            useful = int(stats.get("useful_signals", 0))
            failures = int(stats.get("failures", 0))
            if calls >= 2 and useful == 0:
                recommendations.append(f"avoid repeated {kind} probes unless uncertainty remains high")
            if calls >= 2 and failures / max(calls, 1) >= 0.5:
                recommendations.append(f"prefer cheaper evidence before another {kind} probe")

        return recommendations[:6]

    def _append_recent(self, event: dict[str, Any]) -> None:
        self.recent_events.append(event)
        del self.recent_events[:-8]


def _extract_usage(output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {}
    candidates = [output.get("llm_usage")]
    metadata = output.get("metadata")
    if isinstance(metadata, dict):
        candidates.append(metadata.get("llm_usage"))
        candidates.append(metadata.get("usage"))
    candidates.append(output.get("usage"))
    usage = next((item for item in candidates if isinstance(item, dict)), {})
    prompt_tokens = _int_value(
        usage.get("prompt_tokens", usage.get("input_tokens", usage.get("tokens_in", 0)))
    )
    completion_tokens = _int_value(
        usage.get("completion_tokens", usage.get("output_tokens", usage.get("tokens_out", 0)))
    )
    total_tokens = _int_value(usage.get("total_tokens", prompt_tokens + completion_tokens))
    return {
        "api_calls": _int_value(usage.get("api_calls", usage.get("requests", 1))),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": _float_value(usage.get("cost_usd", usage.get("cost", 0.0))),
    }


def _count_useful_signals(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    keys = set(data.keys())
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        keys.update(metrics.keys())
    stage_summary = data.get("stage_summary")
    if isinstance(stage_summary, dict):
        keys.update(stage_summary.keys())
    signal_keys = {
        "end_to_end_ms",
        "step_time_ms",
        "latency_ms",
        "throughput",
        "tokens_per_second",
        "cost_per_token",
        "stage_imbalance",
        "slowest_stage_ms",
        "communication_ms",
        "sync_ms",
        "memcpy_ms",
        "overlap_ratio",
        "memory_headroom",
        "memory_headroom_pct",
        "hardware_fit",
        "gpu_utilization",
    }
    return len(keys & signal_keys)


def _looks_like_compile_failure(node: SearchNode) -> bool:
    text = " ".join(
        item
        for item in [
            node.error or "",
            node.execution_result.summary if node.execution_result else "",
            node.evaluation_result.summary if node.evaluation_result else "",
        ]
        if item
    ).lower()
    return "compile" in text and ("fail" in text or "error" in text)


def _average_delta(stats: dict[str, Any]) -> float:
    attempts = max(1, int(stats.get("attempts", 0)))
    return round(float(stats.get("score_delta_total", 0.0)) / attempts, 6)


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
