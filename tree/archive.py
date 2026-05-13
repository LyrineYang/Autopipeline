from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import RootMemory, SearchNode, TaskSnapshot


class RunArchive:
    def __init__(self, run_dir: Path, *, task_snapshot: TaskSnapshot) -> None:
        self.run_dir = run_dir
        self.task_snapshot = task_snapshot
        self.archive_dir = run_dir / "archive"
        self.motifs_path = self.archive_dir / "motifs.jsonl"
        self.family_summary_path = self.archive_dir / "family_summary.json"
        self.planner_prior_path = self.archive_dir / "planner_prior.json"
        self._seen_node_ids = self._load_seen_node_ids()

    def record_node(
        self,
        *,
        node: SearchNode,
        parent: SearchNode | None,
        root: RootMemory,
        budget_summary: dict[str, Any],
    ) -> None:
        if node.hypothesis.id == "baseline" or node.id in self._seen_node_ids:
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        record = self._motif_record(node=node, parent=parent, root=root, budget_summary=budget_summary)
        with self.motifs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._seen_node_ids.add(node.id)
        self._refresh_summaries()

    def planner_prior(self) -> dict[str, Any]:
        if not self.planner_prior_path.exists():
            return {
                "top_positive_motifs": [],
                "cooldown_families": [],
                "avoid_repeats": [],
            }
        try:
            return json.loads(self.planner_prior_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "top_positive_motifs": [],
                "cooldown_families": [],
                "avoid_repeats": [],
            }

    def _motif_record(
        self,
        *,
        node: SearchNode,
        parent: SearchNode | None,
        root: RootMemory,
        budget_summary: dict[str, Any],
    ) -> dict[str, Any]:
        evaluation = node.evaluation_result
        parent_eval = parent.evaluation_result if parent is not None else None
        score_delta = None
        if evaluation is not None and parent_eval is not None:
            score_delta = round(evaluation.score - parent_eval.score, 6)
        return {
            "run_id": self.run_dir.name,
            "node_id": node.id,
            "parent_id": node.parent_id,
            "task_type": self.task_snapshot.task_type,
            "scope": node.hypothesis.scope,
            "target_kind": self.task_snapshot.target_kind,
            "target": node.hypothesis.target or self.task_snapshot.target_path,
            "family": node.hypothesis.family,
            "idea_id": node.hypothesis.idea_id,
            "implementation_variant": node.hypothesis.implementation_variant,
            "symptoms": list(node.hypothesis.target_signals),
            "before": _metrics_summary(parent_eval.metrics if parent_eval is not None else {}),
            "after": _metrics_summary(evaluation.metrics if evaluation is not None else {}),
            "outcome": _outcome(node, parent),
            "score_delta": score_delta,
            "status": node.status,
            "accepted": bool(evaluation and evaluation.accepted),
            "score": evaluation.score if evaluation is not None else None,
            "summary": evaluation.summary if evaluation is not None else node.error or "",
            "reflection": node.insight.summary if node.insight is not None else "",
            "root_best_score": root.best_score,
            "cost": _cost_summary(budget_summary),
        }

    def _refresh_summaries(self) -> None:
        records = self._load_records()
        family_summary = _family_summary(records)
        planner_prior = _planner_prior(records, family_summary)
        _write_json_atomic(self.family_summary_path, family_summary)
        _write_json_atomic(self.planner_prior_path, planner_prior)

    def _load_seen_node_ids(self) -> set[str]:
        return {
            str(record.get("node_id"))
            for record in self._load_records()
            if record.get("node_id")
        }

    def _load_records(self) -> list[dict[str, Any]]:
        if not self.motifs_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.motifs_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records


def _outcome(node: SearchNode, parent: SearchNode | None) -> str:
    text = " ".join(
        item
        for item in [
            node.status,
            node.error or "",
            node.execution_result.summary if node.execution_result else "",
            node.evaluation_result.summary if node.evaluation_result else "",
        ]
        if item
    ).lower()
    if "budget" in text and node.status == "pruned":
        return "budget_pruned"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if node.status == "failed":
        if "compile" in text and ("fail" in text or "error" in text):
            return "rejected_compile"
        return "rejected_runtime"
    if node.evaluation_result is None:
        return "rejected_runtime"
    if not node.evaluation_result.accepted:
        correctness = str(node.evaluation_result.metrics.get("correctness", "")).upper()
        if correctness and correctness != "PASS":
            return "rejected_correctness"
        return "rejected_runtime"
    parent_score = parent.evaluation_result.score if parent is not None and parent.evaluation_result else None
    if parent_score is None:
        return "accepted_flat"
    if node.evaluation_result.score > parent_score:
        return "accepted_improved"
    if node.evaluation_result.score < parent_score:
        return "accepted_regressed"
    return "accepted_flat"


def _metrics_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "correctness",
        "score_metric",
        "score_value",
        "speedup",
        "speedup_vs_pytorch",
        "end_to_end_ms",
        "throughput",
        "tokens_per_second",
        "stage_imbalance",
        "overlap_ratio",
    ]
    return {key: metrics[key] for key in keys if key in metrics}


def _cost_summary(budget_summary: dict[str, Any]) -> dict[str, Any]:
    usage = budget_summary.get("agent_usage")
    if not isinstance(usage, dict):
        return {}
    return {
        "API 调用量": usage.get("api_calls"),
        "令牌使用量": usage.get("total_tokens"),
        "推理成本": usage.get("cost_usd"),
        "执行耗时": usage.get("agent_seconds"),
    }


def _family_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, dict[str, Any]] = {}
    for record in records:
        family = str(record.get("family") or "unspecified")
        stats = summary.setdefault(
            family,
            {
                "attempts": 0,
                "accepted": 0,
                "improved": 0,
                "compile_failures": 0,
                "score_delta_total": 0.0,
            },
        )
        stats["attempts"] += 1
        if record.get("accepted"):
            stats["accepted"] += 1
        if record.get("outcome") == "accepted_improved":
            stats["improved"] += 1
        if record.get("outcome") == "rejected_compile":
            stats["compile_failures"] += 1
        delta = record.get("score_delta")
        if isinstance(delta, (int, float)):
            stats["score_delta_total"] = round(float(stats["score_delta_total"]) + float(delta), 6)
    for stats in summary.values():
        attempts = max(1, int(stats["attempts"]))
        stats["avg_score_delta"] = round(float(stats["score_delta_total"]) / attempts, 6)
    return summary


def _planner_prior(
    records: list[dict[str, Any]],
    family_summary: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    positives = [
        record
        for record in records
        if record.get("outcome") == "accepted_improved"
    ]
    positives.sort(key=lambda item: float(item.get("score_delta") or 0.0), reverse=True)
    cooldown = [
        {
            "family": family,
            "reason": "recent attempts produced compile failures or no accepted result",
        }
        for family, stats in sorted(family_summary.items())
        if int(stats.get("attempts", 0)) >= 2
        and (int(stats.get("accepted", 0)) == 0 or int(stats.get("compile_failures", 0)) >= 2)
    ]
    return {
        "top_positive_motifs": [
            {
                "family": record.get("family"),
                "idea_id": record.get("idea_id"),
                "implementation_variant": record.get("implementation_variant"),
                "condition": ", ".join(str(item) for item in record.get("symptoms", [])[:3]),
                "expected_signal": record.get("summary"),
                "score_delta": record.get("score_delta"),
                "confidence": 0.7,
            }
            for record in positives[:5]
        ],
        "cooldown_families": cooldown[:5],
        "avoid_repeats": [
            f"{record.get('idea_id') or record.get('family')}:{record.get('implementation_variant') or record.get('target')}"
            for record in records[-8:]
        ],
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)
