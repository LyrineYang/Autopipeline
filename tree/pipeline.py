from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import AgentAdapter
from .search import TreeSearchEngine
from .task import OptimizationTask
from .types import (
    EvaluationResult,
    ExecutionResult,
    Hypothesis,
    Insight,
    RootMemory,
    SearchConfig,
    SearchNode,
    SearchResult,
    TaskSnapshot,
)


@dataclass(slots=True)
class PipelineSpec:
    path: Path
    name: str
    objective: str
    workspace_source: Path
    target_path: str
    editable_paths: list[str]
    reference_paths: list[str]
    constraints: list[str]
    objective_signals: list[str]
    evaluate_command: list[str] | str
    metrics_path: str = "metrics.json"
    score_metric: str = "throughput"
    score_direction: str = "higher"
    final_verify_command: list[str] | str | None = None
    timeout_seconds: float | None = 300
    metadata: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: Path) -> "PipelineSpec":
        spec_path = path.resolve()
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        base_dir = spec_path.parent
        workspace_source = Path(str(data.get("workspace_source", "")))
        if not workspace_source.is_absolute():
            workspace_source = (base_dir / workspace_source).resolve()
        return cls(
            path=spec_path,
            name=str(data.get("name") or spec_path.stem),
            objective=str(data.get("objective") or "Optimize pipeline objective metrics."),
            workspace_source=workspace_source,
            target_path=str(data.get("target_path") or data.get("target") or "."),
            editable_paths=[str(item) for item in data.get("editable_paths", [])],
            reference_paths=[str(item) for item in data.get("reference_paths", [])],
            constraints=[str(item) for item in data.get("constraints", [])],
            objective_signals=[str(item) for item in data.get("objective_signals", [])],
            evaluate_command=data.get("evaluate_command", []),
            metrics_path=str(data.get("metrics_path", "metrics.json")),
            score_metric=str(data.get("score_metric", "throughput")),
            score_direction=str(data.get("score_direction", "higher")),
            final_verify_command=data.get("final_verify_command"),
            timeout_seconds=_optional_float(data.get("timeout_seconds", 300)),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )


class PipelineTask(OptimizationTask):
    """Generic pipeline workspace task driven by a JSON spec."""

    def __init__(self, *, repo_root: Path, spec_path: Path, run_name: str | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.spec = PipelineSpec.load(spec_path)
        self.run_name = run_name or self.spec.name
        self.run_dir: Path | None = None
        self._last_promoted_path: Path | None = None

    def snapshot(self) -> TaskSnapshot:
        editable = self.spec.editable_paths or [self.spec.target_path]
        return TaskSnapshot(
            task_type="pipeline",
            objective=self.spec.objective,
            target_path=str(self.spec.workspace_source / self.spec.target_path),
            editable_paths=editable,
            constraints=self.spec.constraints
            or [
                "Only edit declared pipeline workspace files.",
                "Preserve the pipeline runner and metrics contract.",
                "Use sensing evidence as read-only input for decisions.",
            ],
            reference_paths=self.spec.reference_paths,
            target_kind="pipeline",
            surface_type="workspace",
            objective_signals=self.spec.objective_signals
            or ["end_to_end_ms", "throughput", "stage_imbalance", "overlap_ratio"],
            metadata={
                "repo_root": str(self.repo_root),
                "pipeline_spec": str(self.spec.path),
                "metrics_path": self.spec.metrics_path,
                "score_metric": self.spec.score_metric,
                **(self.spec.metadata or {}),
            },
        )

    def prepare_run(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def create_sandbox(self, node: SearchNode, parent: SearchNode | None) -> None:
        source = Path(parent.sandbox_path) if parent is not None else self.spec.workspace_source
        sandbox_dir = Path(node.node_dir) / "sandbox"
        if sandbox_dir.exists():
            shutil.rmtree(sandbox_dir)
        shutil.copytree(source, sandbox_dir)
        node.sandbox_path = str(sandbox_dir)
        node.candidate_path = str(sandbox_dir / self.spec.target_path)

    def evaluate(self, node: SearchNode) -> EvaluationResult:
        sandbox = Path(node.sandbox_path)
        if not sandbox.exists():
            raise RuntimeError("Pipeline sandbox must exist before evaluation.")
        node_dir = Path(node.node_dir)
        stdout_path = node_dir / "pipeline_eval.stdout.log"
        stderr_path = node_dir / "pipeline_eval.stderr.log"
        command = self._resolve_command(self.spec.evaluate_command, sandbox=sandbox, node_dir=node_dir)
        result = subprocess.run(
            command,
            cwd=str(sandbox),
            env=self._command_env(sandbox=sandbox, node_dir=node_dir),
            capture_output=True,
            text=True,
            timeout=self.spec.timeout_seconds,
            shell=isinstance(command, str),
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        metrics = self._read_metrics(sandbox, result.stdout)
        metrics["returncode"] = result.returncode
        accepted = _is_accepted(result.returncode, metrics)
        score = _score_from_metrics(metrics, self.spec.score_metric, self.spec.score_direction)
        metrics["score_metric"] = self.spec.score_metric
        metrics["score_value"] = score
        return EvaluationResult(
            accepted=accepted,
            score=score,
            metrics=metrics,
            summary=_pipeline_summary(metrics, accepted),
            raw_log_paths={
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "metrics": str(sandbox / self.spec.metrics_path),
            },
        )

    def promote(self, node: SearchNode, root: RootMemory) -> str | None:
        if self.run_dir is None:
            return None
        promoted_path = self.run_dir / "promoted_pipeline"
        if promoted_path.exists():
            shutil.rmtree(promoted_path)
        shutil.copytree(Path(node.sandbox_path), promoted_path)
        self._last_promoted_path = promoted_path
        return str(promoted_path)

    def final_verify(self, root: RootMemory) -> dict[str, Any] | None:
        if self.spec.final_verify_command is None or self._last_promoted_path is None or self.run_dir is None:
            return None
        stdout_path = self.run_dir / "pipeline_final_verify.stdout.log"
        stderr_path = self.run_dir / "pipeline_final_verify.stderr.log"
        command = self._resolve_command(
            self.spec.final_verify_command,
            sandbox=self._last_promoted_path,
            node_dir=self.run_dir,
        )
        result = subprocess.run(
            command,
            cwd=str(self._last_promoted_path),
            env=self._command_env(sandbox=self._last_promoted_path, node_dir=self.run_dir),
            capture_output=True,
            text=True,
            timeout=self.spec.timeout_seconds,
            shell=isinstance(command, str),
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        metrics = self._read_metrics(self._last_promoted_path, result.stdout)
        accepted = _is_accepted(result.returncode, metrics)
        return {
            "accepted": accepted,
            "summary": _pipeline_summary(metrics, accepted),
            "metrics": metrics,
            "raw_log_paths": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
        }

    def seed_hypotheses(self, parent: SearchNode, root: RootMemory, limit: int) -> list[Hypothesis]:
        candidates = [
            Hypothesis(
                id="reduce-end-to-end",
                title="Reduce end-to-end latency",
                core_hypothesis="Reduce the dominant pipeline latency without changing the runner contract.",
                scope="pipeline",
                target=self.spec.target_path,
                editable_path_hints=list(self.spec.editable_paths),
                family="pipeline_latency",
                verification_intent=["end_to_end_ms", "throughput"],
            ),
            Hypothesis(
                id="balance-stages",
                title="Balance stages",
                core_hypothesis="Reduce stage imbalance by shifting work away from the slowest stage.",
                scope="pipeline",
                target=self.spec.target_path,
                editable_path_hints=list(self.spec.editable_paths),
                family="stage_balance",
                verification_intent=["stage_imbalance", "slowest_stage_ms"],
            ),
            Hypothesis(
                id="improve-overlap",
                title="Improve overlap",
                core_hypothesis="Increase useful overlap and reduce communication or synchronization exposure.",
                scope="pipeline",
                target=self.spec.target_path,
                editable_path_hints=list(self.spec.editable_paths),
                family="overlap",
                verification_intent=["overlap_ratio", "communication_ms", "sync_ms"],
            ),
        ]
        return candidates[:limit]

    def planner_context(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
        *,
        upper_bounds: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "task_kind": "pipeline",
            "pipeline_spec": str(self.spec.path),
            "editable_paths": list(self.spec.editable_paths),
            "objective_signals": self.snapshot().objective_signals,
            "metrics_path": self.spec.metrics_path,
            "score_metric": self.spec.score_metric,
            "agent_role": "modify declared pipeline workspace files; do not reinterpret raw traces manually",
        }

    def prepare_execution_surface(self, node: SearchNode, parent: SearchNode | None) -> None:
        node.planner_context.setdefault("effective_scope", "pipeline")
        node.planner_context.setdefault("effective_editable_paths", list(self.spec.editable_paths))

    def handle_probe(
        self,
        request: dict[str, Any],
        *,
        cwd: Path,
        node: SearchNode | None,
        parent: SearchNode | None,
    ) -> dict[str, Any]:
        kind = str(request.get("kind", ""))
        if kind not in {"stage_summary", "runtime_breakdown", "pipeline_metrics"}:
            return {"status": "rejected", "summary": f"Unsupported pipeline probe: {kind}", "data": None}
        metrics = self._read_metrics(cwd, "")
        return {
            "status": "completed",
            "summary": f"{kind} returned pipeline metrics from {self.spec.metrics_path}.",
            "data": {
                "metrics": metrics,
                "stage_summary": metrics.get("stage_summary", {}),
            },
        }

    def checkpoint_candidate(self, node: SearchNode, checkpoint_path: Path) -> None:
        if checkpoint_path.exists():
            if checkpoint_path.is_dir():
                shutil.rmtree(checkpoint_path)
            else:
                checkpoint_path.unlink()
        shutil.copytree(Path(node.sandbox_path), checkpoint_path)

    def restore_candidate(self, node: SearchNode, checkpoint_path: Path) -> None:
        sandbox_path = Path(node.sandbox_path)
        if sandbox_path.exists():
            shutil.rmtree(sandbox_path)
        shutil.copytree(checkpoint_path, sandbox_path)
        node.candidate_path = str(sandbox_path / self.spec.target_path)

    def _resolve_command(
        self,
        raw_command: list[str] | str,
        *,
        sandbox: Path,
        node_dir: Path,
    ) -> list[str] | str:
        if isinstance(raw_command, str):
            return _format_command_part(raw_command, sandbox=sandbox, node_dir=node_dir)
        if not isinstance(raw_command, list) or not raw_command:
            raise ValueError("Pipeline spec requires a non-empty evaluate_command.")
        return [_format_command_part(str(part), sandbox=sandbox, node_dir=node_dir) for part in raw_command]

    def _command_env(self, *, sandbox: Path, node_dir: Path) -> dict[str, str]:
        return {
            **os.environ,
            "PIPELINE_WORKSPACE": str(sandbox),
            "PIPELINE_NODE_DIR": str(node_dir),
            "PIPELINE_METRICS_PATH": str(sandbox / self.spec.metrics_path),
        }

    def _read_metrics(self, workspace: Path, stdout: str) -> dict[str, Any]:
        metrics_path = workspace / self.spec.metrics_path
        if metrics_path.exists():
            return json.loads(metrics_path.read_text(encoding="utf-8"))
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


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
        for stage in config.get("stages", []):
            stage["latency_ms"] = round(float(stage.get("latency_ms", 0.0)) * 0.82, 3)
        config["communication_ms"] = round(float(config.get("communication_ms", 0.0)) * 0.7, 3)
        config["sync_ms"] = round(float(config.get("sync_ms", 0.0)) * 0.75, 3)
        config["memcpy_ms"] = round(float(config.get("memcpy_ms", 0.0)) * 0.8, 3)
        config["overlap_ratio"] = min(0.95, round(float(config.get("overlap_ratio", 0.0)) + 0.18, 3))
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return ExecutionResult(
            status="completed",
            summary="Smoke agent reduced stage and communication costs in the declared pipeline config.",
            changed=True,
            candidate_path=str(config_path),
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


def setup_pipeline_workspace(spec_path: Path, output_dir: Path) -> dict[str, str]:
    spec = PipelineSpec.load(spec_path)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(spec.workspace_source, output_dir)
    metadata_path = output_dir / "pipeline_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "name": spec.name,
                "objective": spec.objective,
                "spec_path": str(spec.path),
                "target_path": spec.target_path,
                "editable_paths": spec.editable_paths,
                "objective_signals": spec.objective_signals,
                "metrics_path": spec.metrics_path,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "workspace": str(output_dir),
        "metadata": str(metadata_path),
        "target": str(output_dir / spec.target_path),
    }


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
        config=SearchConfig(beam_width=1, max_rounds=1, children_per_expansion=1),
        run_dir=run_dir / "tree_run",
    )
    return engine.run()


def default_pipeline_run_dir(repo_root: Path, name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return repo_root / "workspace" / "pipeline_runs" / f"{stamp}-{name}"


def _format_command_part(part: str, *, sandbox: Path, node_dir: Path) -> str:
    return part.format(
        python=sys.executable,
        workspace=str(sandbox),
        node_dir=str(node_dir),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_accepted(returncode: int, metrics: dict[str, Any]) -> bool:
    if returncode != 0:
        return False
    if metrics.get("accepted") is False:
        return False
    correctness = str(metrics.get("correctness", "PASS")).upper()
    return correctness not in {"FAIL", "FAILED", "FALSE"}


def _score_from_metrics(metrics: dict[str, Any], score_metric: str, direction: str) -> float:
    value = _optional_float(metrics.get(score_metric))
    if value is not None:
        return value if direction != "lower" else -value
    for key in ("throughput", "tokens_per_second"):
        value = _optional_float(metrics.get(key))
        if value is not None:
            return value
    latency = _optional_float(metrics.get("end_to_end_ms"))
    if latency and latency > 0:
        return 1.0 / latency
    return 1.0 if metrics else 0.0


def _pipeline_summary(metrics: dict[str, Any], accepted: bool) -> str:
    parts = [f"accepted={accepted}"]
    for key in ("end_to_end_ms", "throughput", "stage_imbalance", "overlap_ratio"):
        if key in metrics:
            parts.append(f"{key}={metrics[key]}")
    return " ".join(parts)


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
raw_total = sum(stage_latencies) + communication_ms + sync_ms + memcpy_ms
end_to_end_ms = raw_total * (1.0 - min(max(overlap_ratio, 0.0), 0.95) * 0.25)
stage_imbalance = (max(stage_latencies) - min(stage_latencies)) / max(sum(stage_latencies), 1.0)
throughput = 1000.0 / max(end_to_end_ms, 1e-6)
metrics = {
    "accepted": True,
    "correctness": "PASS",
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
