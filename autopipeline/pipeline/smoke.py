"""Synthetic harness fixtures; never evidence of model correctness or speedup."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autopipeline.search.agent import AgentAdapter
from autopipeline.search.engine import TreeSearchEngine
from autopipeline.search.task import OptimizationTask
from autopipeline.search.types import (
    ExecutionResult, Hypothesis, Insight, RootMemory, SearchConfig,
    SearchNode, SearchResult, TaskSnapshot,
)
from .task import PipelineTask


def _reduce_slowest_stage(config: dict[str, Any], *, factor: float) -> None:
    stages = config.get("stages")
    if not isinstance(stages, list) or not stages:
        return
    slowest = max(
        (stage for stage in stages if isinstance(stage, dict)),
        key=lambda stage: float(stage.get("latency_ms", 0.0)),
        default=None,
    )
    if slowest is None:
        return
    slowest["latency_ms"] = round(float(slowest.get("latency_ms", 0.0)) * factor, 3)

class PipelineSmokeAgent(AgentAdapter):
    def propose(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        limit: int,
        planner_packet=None,
    ) -> list[Hypothesis]:
        if planner_packet is not None and planner_packet.branch_cards:
            return planner_packet.branch_cards[:limit]
        return task.seed_hypotheses(parent, root, limit)

    def execute(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        config_path = Path(node.candidate_path)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        family = node.hypothesis.family or "pipeline_latency"
        if family == "stage_balance":
            _reduce_slowest_stage(config, factor=0.72)
            summary = "Smoke agent reduced the diagnosed slowest stage latency."
        elif family == "overlap":
            config["communication_ms"] = round(float(config.get("communication_ms", 0.0)) * 0.65, 3)
            config["sync_ms"] = round(float(config.get("sync_ms", 0.0)) * 0.7, 3)
            config["overlap_ratio"] = min(0.95, round(float(config.get("overlap_ratio", 0.0)) + 0.25, 3))
            summary = "Smoke agent improved communication/synchronization overlap."
        else:
            for stage in config.get("stages", []):
                stage["latency_ms"] = round(float(stage.get("latency_ms", 0.0)) * 0.86, 3)
            config["communication_ms"] = round(float(config.get("communication_ms", 0.0)) * 0.75, 3)
            config["sync_ms"] = round(float(config.get("sync_ms", 0.0)) * 0.75, 3)
            config["memcpy_ms"] = round(float(config.get("memcpy_ms", 0.0)) * 0.85, 3)
            config["overlap_ratio"] = min(0.95, round(float(config.get("overlap_ratio", 0.0)) + 0.12, 3))
            summary = "Smoke agent applied a combined end-to-end latency treatment."
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return ExecutionResult(
            status="completed",
            summary=summary,
            changed=True,
            candidate_path=str(config_path),
            metadata={
                "treatment_family": family,
                "diagnosis": list(node.hypothesis.supporting_evidence),
            },
        )

    def reflect(
        self,
        task: OptimizationTask,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        parent: SearchNode | None,
        node: SearchNode,
    ) -> Insight:
        return Insight(
            polarity="positive" if node.evaluation_result and node.evaluation_result.accepted else "negative",
            summary=node.evaluation_result.summary if node.evaluation_result else "No evaluation result.",
            evidence=["pipeline smoke path completed"],
            source_node_id=node.id,
        )

def create_pipeline_smoke_fixture(base_dir: Path) -> Path:
    fixture_dir = base_dir.resolve()
    workspace = fixture_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "pipeline_config.json"
    config_path.write_text(
        json.dumps(
            {
                "stages": [
                    {"name": "prefill", "latency_ms": 40.0},
                    {"name": "decode", "latency_ms": 55.0},
                    {"name": "postprocess", "latency_ms": 15.0},
                ],
                "communication_ms": 18.0,
                "sync_ms": 8.0,
                "memcpy_ms": 6.0,
                "overlap_ratio": 0.42,
                "memory_headroom": 0.38,
                "hardware_fit": 0.70,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    evaluator_path = workspace / "evaluate_pipeline.py"
    evaluator_path.write_text(_SMOKE_EVALUATOR_SOURCE, encoding="utf-8")
    spec_path = fixture_dir / "pipeline_task.json"
    spec_path.write_text(
        json.dumps(
            {
                "name": "pipeline-smoke",
                "objective": "Reduce end-to-end latency while preserving pipeline correctness.",
                "workspace_source": "workspace",
                "target_path": "pipeline_config.json",
                "editable_paths": ["pipeline_config.json"],
                "objective_signals": [
                    "end_to_end_ms",
                    "throughput",
                    "stage_imbalance",
                    "communication_ms",
                    "sync_ms",
                    "memcpy_ms",
                    "overlap_ratio",
                    "memory_headroom",
                    "hardware_fit",
                ],
                "evaluate_command": ["{python}", "evaluate_pipeline.py"],
                "metrics_path": "metrics.json",
                "score_metric": "throughput",
                "score_direction": "higher",
                "metadata": {
                    "workload": "pipeline-smoke-fixture",
                    "hardware_context": {
                        "device": "local-fixture",
                        "gpu_required": False,
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return spec_path

def run_pipeline_smoke(*, repo_root: Path, run_dir: Path) -> SearchResult:
    spec_path = create_pipeline_smoke_fixture(run_dir / "fixture")
    task = PipelineTask(repo_root=repo_root, spec_path=spec_path)
    engine = TreeSearchEngine(
        task=task,
        agent=PipelineSmokeAgent(),
        config=SearchConfig(beam_width=2, max_rounds=1, children_per_expansion=3),
        run_dir=run_dir / "tree_run",
    )
    return engine.run()

_SMOKE_EVALUATOR_SOURCE = """from __future__ import annotations

import json
from pathlib import Path

config = json.loads(Path("pipeline_config.json").read_text(encoding="utf-8"))
stages = config["stages"]
stage_latencies = [float(stage["latency_ms"]) for stage in stages]
slowest = max(stages, key=lambda item: float(item["latency_ms"]))
communication_ms = float(config.get("communication_ms", 0.0))
sync_ms = float(config.get("sync_ms", 0.0))
memcpy_ms = float(config.get("memcpy_ms", 0.0))
overlap_ratio = float(config.get("overlap_ratio", 0.0))
output_value = float(config.get("output_value", 1.0))
raw_total = sum(stage_latencies) + communication_ms + sync_ms + memcpy_ms
end_to_end_ms = raw_total * (1.0 - min(max(overlap_ratio, 0.0), 0.95) * 0.25)
stage_imbalance = (max(stage_latencies) - min(stage_latencies)) / max(sum(stage_latencies), 1.0)
throughput = 1000.0 / max(end_to_end_ms, 1e-6)
metrics = {
    "accepted": True,
    "correctness": "PASS",
    "output_value": output_value,
    "end_to_end_ms": round(end_to_end_ms, 3),
    "throughput": round(throughput, 3),
    "stage_imbalance": round(stage_imbalance, 3),
    "communication_ms": communication_ms,
    "sync_ms": sync_ms,
    "memcpy_ms": memcpy_ms,
    "overlap_ratio": overlap_ratio,
    "memory_headroom": float(config.get("memory_headroom", 0.0)),
    "hardware_fit": float(config.get("hardware_fit", 0.0)),
    "stage_summary": {
        "slowest_stage": slowest["name"],
        "slowest_stage_ms": float(slowest["latency_ms"]),
        "communication_ms": communication_ms,
        "sync_ms": sync_ms,
        "memcpy_ms": memcpy_ms,
        "overlap_ratio": overlap_ratio,
    },
}
Path("metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
print(json.dumps(metrics))
"""
