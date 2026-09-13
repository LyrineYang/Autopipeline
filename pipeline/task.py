from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tree.agent import AgentAdapter
from tree.search import TreeSearchEngine
from tree.task import OptimizationTask
from tree.types import (
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

from .verification_backend import (
    ExecutableRunRequest,
    ExecutableVerificationBackend,
    compact_verification_report,
)
from .region import build_pipeline_region_contract, region_work_unit


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
        from .validation import validate_spec_data
        validate_spec_data(data, spec_path)
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
        tolerances = (self.spec.metadata or {}).get("numerical_tolerances", {})
        self.verification_backend = ExecutableVerificationBackend(
            numerical_atol=float(tolerances.get("atol", 1e-5)),
            numerical_rtol=float(tolerances.get("rtol", 1e-4)),
        )
        self.initial_candidate: Path | None = None
        self.measurement_only_baseline = False

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
        source = Path(parent.sandbox_path) if parent is not None else (self.initial_candidate or self.spec.workspace_source)
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
        from .validation import changed_protected_files
        violations = changed_protected_files(self.spec, sandbox)
        if violations:
            return EvaluationResult(
                accepted=False, score=float("-inf"),
                metrics={"correctness": "FAIL", "verification_failure_reason": "protected_files_changed",
                         "protected_files_changed": violations},
                summary="Rejected edits outside editable_paths: " + ", ".join(violations),
            )
        artifact_dir = node_dir / "verification"
        reference_workspace = self._prepare_reference_workspace(artifact_dir)
        reference_command = self._resolve_command(
            self.spec.evaluate_command,
            sandbox=reference_workspace,
            node_dir=node_dir,
        )
        candidate_command = self._resolve_command(self.spec.evaluate_command, sandbox=sandbox, node_dir=node_dir)
        bottleneck_region = _node_bottleneck_region(node)
        candidate_modifications = _candidate_modifications(
            reference_workspace=reference_workspace,
            candidate_workspace=sandbox,
            editable_paths=self.spec.editable_paths or [self.spec.target_path],
            metrics_path=self.spec.metrics_path,
        )
        report = self.verification_backend.verify(
            require_no_regression=not (self.measurement_only_baseline and node.parent_id is None),
            reference=ExecutableRunRequest(
                label="reference",
                workspace=reference_workspace,
                command=reference_command,
                metrics_path=self.spec.metrics_path,
                env=self._command_env(
                    sandbox=reference_workspace,
                    node_dir=node_dir,
                    reference_workspace=reference_workspace,
                    candidate_workspace=sandbox,
                    role="reference",
                ),
                timeout_seconds=self.spec.timeout_seconds,
                shell=isinstance(reference_command, str),
            ),
            candidate=ExecutableRunRequest(
                label="candidate",
                workspace=sandbox,
                command=candidate_command,
                metrics_path=self.spec.metrics_path,
                env=self._command_env(
                    sandbox=sandbox,
                    node_dir=node_dir,
                    reference_workspace=reference_workspace,
                    candidate_workspace=sandbox,
                    role="candidate",
                ),
                timeout_seconds=self.spec.timeout_seconds,
                shell=isinstance(candidate_command, str),
            ),
            artifact_dir=artifact_dir,
            score_metric=self.spec.score_metric,
            score_direction=self.spec.score_direction,
            metadata={
                "task": self.spec.name,
                "node_id": node.id,
                "hypothesis_id": node.hypothesis.id,
                "family": node.hypothesis.family,
                "target": node.hypothesis.target,
                "workspace_source": str(self.spec.workspace_source),
                "bottleneck_region": bottleneck_region,
                "candidate_modifications": candidate_modifications,
            },
        )
        metrics = dict(report.get("candidate", {}).get("metrics", {}))
        performance = report.get("performance", {}) if isinstance(report.get("performance"), dict) else {}
        metrics["returncode"] = report.get("candidate", {}).get("returncode")
        metrics["score_metric"] = self.spec.score_metric
        score = _optional_float(performance.get("candidate_score_value"))
        if score is None:
            score = float("-inf")
        metrics["score_metric"] = self.spec.score_metric
        metrics["score_value"] = score
        metrics["verification_backend"] = report.get("backend")
        metrics["verification_verdict"] = report.get("verdict")
        metrics["verification_failure_reason"] = report.get("failure_reason")
        metrics["verification_report_path"] = report.get("artifact_paths", {}).get("report")
        metrics["verification"] = compact_verification_report(report)
        if not report.get("correctness", {}).get("passed"):
            metrics["correctness"] = "FAIL"
        elif "correctness" not in metrics:
            metrics["correctness"] = "PASS"
        if performance.get("speedup") is not None:
            metrics["end_to_end_speedup"] = performance.get("speedup")
        accepted = bool(report.get("accepted"))
        self._publish_evaluation_artifacts(node, report, metrics)
        return EvaluationResult(
            accepted=accepted,
            score=score,
            metrics=metrics,
            summary=_pipeline_summary(metrics, accepted),
            raw_log_paths=_report_log_paths(report),
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
        from .validation import changed_protected_files
        violations = changed_protected_files(self.spec, self._last_promoted_path)
        if violations:
            return {"accepted": False, "failure_reason": "protected_files_changed",
                    "summary": "Protected files changed: " + ", ".join(violations), "metrics": {}}
        artifact_dir = self.run_dir / "final_verification"
        reference_workspace = self._prepare_reference_workspace(artifact_dir)
        reference_command = self._resolve_command(
            self.spec.evaluate_command,
            sandbox=reference_workspace,
            node_dir=self.run_dir,
        )
        command = self._resolve_command(
            self.spec.final_verify_command,
            sandbox=self._last_promoted_path,
            node_dir=self.run_dir,
        )
        report = self.verification_backend.verify(
            require_no_regression=not self.measurement_only_baseline or bool(_candidate_modifications(
                reference_workspace=self.spec.workspace_source,
                candidate_workspace=self._last_promoted_path,
                editable_paths=self.spec.editable_paths or [self.spec.target_path],
                metrics_path=self.spec.metrics_path,
            )),
            reference=ExecutableRunRequest(
                label="reference",
                workspace=reference_workspace,
                command=reference_command,
                metrics_path=self.spec.metrics_path,
                env=self._command_env(
                    sandbox=reference_workspace,
                    node_dir=self.run_dir,
                    reference_workspace=reference_workspace,
                    candidate_workspace=self._last_promoted_path,
                    role="reference",
                ),
                timeout_seconds=self.spec.timeout_seconds,
                shell=isinstance(reference_command, str),
            ),
            candidate=ExecutableRunRequest(
                label="candidate",
                workspace=self._last_promoted_path,
                command=command,
                metrics_path=self.spec.metrics_path,
                env=self._command_env(
                    sandbox=self._last_promoted_path,
                    node_dir=self.run_dir,
                    reference_workspace=reference_workspace,
                    candidate_workspace=self._last_promoted_path,
                ),
                timeout_seconds=self.spec.timeout_seconds,
                shell=isinstance(command, str),
            ),
            artifact_dir=artifact_dir,
            score_metric=self.spec.score_metric,
            score_direction=self.spec.score_direction,
            metadata={"task": self.spec.name, "stage": "final_verify"},
        )
        violations = changed_protected_files(self.spec, self._last_promoted_path)
        if violations:
            report.update(accepted=False, verdict="reject", failure_reason="protected_files_changed",
                          protected_files_changed=violations)
            _write_json(Path(report["artifact_paths"]["report"]), report)
        metrics = report["candidate"]["metrics"]
        accepted = bool(report["accepted"])
        return {
            "accepted": accepted,
            "failure_reason": report["failure_reason"],
            "summary": _pipeline_summary(metrics, accepted),
            "metrics": metrics,
            "verification": compact_verification_report(report),
            "raw_log_paths": _report_log_paths(report),
        }

    def seed_hypotheses(self, parent: SearchNode, root: RootMemory, limit: int) -> list[Hypothesis]:
        candidates = _pipeline_branch_cards(
            spec=self.spec,
            observation={},
            diagnoses=["end_to_end_latency"],
            treatment_families=["pipeline_latency", "stage_balance", "overlap"],
            limit=limit,
        )
        if candidates:
            return candidates[:limit]
        return [
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
        ][:limit]

    def planner_context(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
        *,
        upper_bounds: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        parent_metrics = parent.evaluation_result.metrics if parent.evaluation_result is not None else {}
        observation = _pipeline_observation(parent_metrics)
        diagnoses = _pipeline_diagnoses(observation)
        treatment_families = _pipeline_treatment_families(diagnoses)
        branch_limit = max(1, int(limit or 1))
        branch_cards = _pipeline_branch_cards(
            spec=self.spec,
            observation=observation,
            diagnoses=diagnoses,
            treatment_families=treatment_families,
            limit=branch_limit,
        )
        symptoms = _pipeline_symptoms(observation)
        focus_target = str(observation.get("slowest_stage") or self.spec.target_path)
        region_graph = _pipeline_region_graph(self.spec, observation, symptoms)
        bottleneck_card = {
            "scope": "pipeline",
            "target": focus_target,
            "symptoms": symptoms,
            "diagnoses": diagnoses,
            "response_families": treatment_families,
            "avoid_families": [],
            "rationale": _pipeline_rationale(observation, diagnoses, treatment_families),
        }
        hardware_context = _pipeline_hardware_context(self.spec)
        region_artifact = self._region_artifact_path(parent)
        bottleneck_region = build_pipeline_region_contract(
            task_name=self.spec.name,
            workspace_source=self.spec.workspace_source,
            target_path=self.spec.target_path,
            editable_paths=self.spec.editable_paths,
            reference_paths=self.spec.reference_paths,
            evaluate_command=self.spec.evaluate_command,
            metrics_path=self.spec.metrics_path,
            score_metric=self.spec.score_metric,
            score_direction=self.spec.score_direction,
            objective_signals=snapshot.objective_signals,
            region_graph=region_graph,
            bottleneck_card=bottleneck_card,
            observation=observation,
            hardware_context=hardware_context,
            artifact_path=region_artifact,
        )
        branch_cards = [
            _with_region_work_unit(card, bottleneck_region.to_dict(), region_artifact)
            for card in branch_cards
        ]
        region_artifact = self._write_region_artifact(
            parent=parent,
            region_graph=region_graph,
            bottleneck_card=bottleneck_card,
            bottleneck_region=bottleneck_region.to_dict(),
            observation=observation,
            branch_cards=branch_cards,
        )
        branch_budget = {
            "children": min(branch_limit, len(branch_cards)) if branch_cards else branch_limit,
            "beam_width": min(int((upper_bounds or {}).get("beam_width", branch_limit)), max(1, len(branch_cards))),
            "rounds": 1,
        }
        return {
            "task_kind": "pipeline",
            "pipeline_spec": str(self.spec.path),
            "editable_paths": list(self.spec.editable_paths),
            "objective_signals": snapshot.objective_signals,
            "metrics_path": self.spec.metrics_path,
            "score_metric": self.spec.score_metric,
            "score_direction": self.spec.score_direction,
            "static_prior": {
                "task_kind": "pipeline",
                "fixed_workload": self.spec.name,
                "pipeline_spec": str(self.spec.path),
                "score_metric": self.spec.score_metric,
                "score_direction": self.spec.score_direction,
            },
            "hardware_context": hardware_context,
            "history_context": {
                "best_score": root.best_score,
                "best_node_id": root.best_node_id,
                "rounds_completed": root.rounds_completed,
                "positive_insights": root.positive_insights[-5:],
                "negative_insights": root.negative_insights[-5:],
            },
            "pipeline_observation": observation,
            "profiler": parent_metrics.get("profiler", {"status": "unavailable", "reason": "Evaluator did not report a profiler"}),
            "symptoms": symptoms,
            "region_graph": region_graph,
            "bottleneck_card": bottleneck_card,
            "bottleneck_region": bottleneck_region.to_dict(),
            "branch_cards": [_hypothesis_to_context(card) for card in branch_cards],
            "artifact_paths": {
                "region": region_artifact,
            },
            "planner_rules": {
                "allowed_scopes": ["pipeline"],
                "allowed_families": treatment_families,
                "editable_paths": list(self.spec.editable_paths or [self.spec.target_path]),
            },
            "search_policy": {
                "recommended_scope": "pipeline",
                "scope_options": ["pipeline"],
                "exploration_bias": "diagnosis-guided",
                "branch_budget": branch_budget,
                "resource_hint": {
                    "tier": "lean",
                    "notes": [
                        "Evaluate each candidate in its own copied workspace.",
                        "Use declared workload device and profiler evidence; serialize GPU measurements.",
                    ],
                },
                "stop_conditions": ["stop when no branch improves the end-to-end score"],
            },
            "frozen_decisions": [
                "Use the diagnosed pipeline bottleneck as the branch identity source.",
                "Executor may edit only declared pipeline workspace files.",
                "Evaluator metrics decide keep or reject; agent self-report is diagnostic only.",
            ],
            "agent_role": "modify declared pipeline workspace files; do not reinterpret raw traces manually",
        }

    def prepare_execution_surface(self, node: SearchNode, parent: SearchNode | None) -> None:
        work_unit = node.hypothesis.work_unit or node.planner_context.get("work_unit", {})
        region = work_unit.get("region") if isinstance(work_unit, dict) else {}
        node.planner_context.setdefault("effective_scope", "pipeline")
        node.planner_context.setdefault("effective_editable_paths", list(self.spec.editable_paths))
        if isinstance(region, dict) and region:
            node.planner_context["bottleneck_region"] = region
            node.planner_context["work_unit"] = work_unit
            node.planner_context["effective_scope"] = str(region.get("scope", "pipeline"))
            node.planner_context["focus_target"] = str(region.get("label", self.spec.target_path))
            node.planner_context["scope_summary"] = str(region.get("summary", ""))
            context_path = Path(node.sandbox_path) / "bottleneck_region.json"
            _write_json(context_path, region)
            node.planner_context["scope_context_files"] = [
                *node.planner_context.get("scope_context_files", []),
                context_path.name,
            ]

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
                "profiler": metrics.get("profiler", {"status": "unavailable", "reason": "Evaluator did not report a profiler"}),
                "operator_summary": ((cwd / ".autopipeline" / "operators.txt").read_text(encoding="utf-8")[:20000]
                                     if (cwd / ".autopipeline" / "operators.txt").is_file() else None),
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

    def _command_env(
        self,
        *,
        sandbox: Path,
        node_dir: Path,
        reference_workspace: Path | None = None,
        candidate_workspace: Path | None = None,
        role: str = "candidate",
    ) -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")])),
            "PIPELINE_WORKSPACE": str(sandbox),
            "PIPELINE_NODE_DIR": str(node_dir),
            "PIPELINE_METRICS_PATH": str(sandbox / self.spec.metrics_path),
            "PIPELINE_REFERENCE_WORKSPACE": str(reference_workspace or self.spec.workspace_source),
            "PIPELINE_CANDIDATE_WORKSPACE": str(candidate_workspace or sandbox),
            "PIPELINE_VERIFICATION_ROLE": role,
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

    def _prepare_reference_workspace(self, artifact_dir: Path) -> Path:
        reference_workspace = artifact_dir / "reference_workspace"
        if reference_workspace.exists():
            shutil.rmtree(reference_workspace)
        shutil.copytree(self.spec.workspace_source, reference_workspace)
        return reference_workspace

    def _publish_evaluation_artifacts(
        self,
        node: SearchNode,
        report: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        if self.run_dir is None:
            return
        report_path = Path(str(report.get("artifact_paths", {}).get("report", "")))
        target_dir = (
            self.run_dir / "baseline"
            if node.hypothesis.id == "baseline" or node.parent_id is None
            else self.run_dir / "trials" / node.id
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            shutil.copy2(report_path, target_dir / "verification_report.json")
        _write_json(target_dir / "metrics.json", metrics)
        compact = metrics.get("verification") if isinstance(metrics.get("verification"), dict) else {}
        _write_json(
            target_dir / "trial.json",
            {
                "node_id": node.id,
                "parent_id": node.parent_id,
                "hypothesis": _hypothesis_to_context(node.hypothesis),
                "bottleneck_region": compact.get("bottleneck_region", {}),
                "candidate_modifications": compact.get("candidate_modifications", []),
                "verification_report": str(target_dir / "verification_report.json"),
                "metrics": str(target_dir / "metrics.json"),
            },
        )
        profile_dir = self.run_dir / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            profile_dir / f"{node.id}.profile_probe.json",
            {
                "node_id": node.id,
                "source": "verification_backend",
                "bottleneck_region": compact.get("bottleneck_region", {}),
                "performance": report.get("performance", {}),
                "stage_summary": metrics.get("stage_summary", {}),
                "artifact_paths": report.get("artifact_paths", {}),
            },
        )

    def _write_region_artifact(
        self,
        *,
        parent: SearchNode,
        region_graph: dict[str, Any],
        bottleneck_card: dict[str, Any],
        bottleneck_region: dict[str, Any],
        observation: dict[str, Any],
        branch_cards: list[Hypothesis],
    ) -> str:
        raw_path = self._region_artifact_path(parent)
        if not raw_path:
            return ""
        path = Path(raw_path)
        _write_json(
            path,
            {
                "parent_id": parent.id,
                "bottleneck_region": bottleneck_region,
                "region_graph": region_graph,
                "bottleneck_card": bottleneck_card,
                "observation": observation,
                "branch_cards": [_hypothesis_to_context(card) for card in branch_cards],
            },
        )
        return str(path)

    def _region_artifact_path(self, parent: SearchNode) -> str:
        if self.run_dir is None:
            return ""
        return str(self.run_dir / "regions" / f"{parent.id}.region.json")

    def augment_verifier_summary(
        self,
        *,
        task_snapshot: TaskSnapshot,
        root: RootMemory,
        recent_probe_results: list[dict[str, Any]],
        parent: SearchNode | None,
        node: SearchNode | None,
    ) -> dict[str, Any]:
        if node is None or node.evaluation_result is None:
            return {}
        verification = _verification_from_metrics(node.evaluation_result.metrics)
        if not verification:
            return {}
        verdict = str(verification.get("verdict") or "need_more_evidence")
        failure_reason = verification.get("failure_reason")
        performance = verification.get("performance", {}) if isinstance(verification.get("performance"), dict) else {}
        numerical = verification.get("numerical_error", {}) if isinstance(verification.get("numerical_error"), dict) else {}
        report_path = verification.get("report_path") or verification.get("artifact_paths", {}).get("report")
        evidence: list[str] = []
        regressions: list[str] = []
        line = (
            "Verification backend "
            f"{verdict}: speedup={performance.get('speedup')} "
            f"max_abs_error={numerical.get('max_abs_error')}"
        )
        if verdict == "keep":
            evidence.append(line)
        else:
            regressions.append(line)
        if report_path:
            evidence.append(f"Verification report: {report_path}")
        if failure_reason:
            regressions.append(f"Verification failure reason: {failure_reason}")
        return {
            "status": "healthy" if verdict == "keep" else "warning",
            "verdict": "keep" if verdict == "keep" else "reject",
            "evidence": evidence,
            "regressions": regressions,
            "uncertainty": [],
        }


def _pipeline_observation(metrics: dict[str, Any]) -> dict[str, Any]:
    stage_summary = metrics.get("stage_summary")
    stage_metrics = stage_summary if isinstance(stage_summary, dict) else {}
    observation: dict[str, Any] = {
        "source": "parent_evaluation" if metrics else "missing_parent_metrics",
    }
    for key in (
        "end_to_end_ms",
        "throughput",
        "stage_imbalance",
        "communication_ms",
        "sync_ms",
        "memcpy_ms",
        "overlap_ratio",
        "memory_headroom",
        "hardware_fit",
        "output_value",
        "score_metric",
        "score_value",
    ):
        if key in metrics:
            observation[key] = metrics[key]
    for key in ("slowest_stage", "slowest_stage_ms", "communication_ms", "sync_ms", "memcpy_ms", "overlap_ratio"):
        if key in stage_metrics and key not in observation:
            observation[key] = stage_metrics[key]
    return observation


def _report_log_paths(report: dict[str, Any]) -> dict[str, str]:
    paths = report.get("artifact_paths")
    if not isinstance(paths, dict):
        return {}
    return {
        "stdout": str(paths.get("candidate_stdout", "")),
        "stderr": str(paths.get("candidate_stderr", "")),
        "metrics": str(paths.get("candidate_metrics", "")),
        "reference_stdout": str(paths.get("reference_stdout", "")),
        "reference_stderr": str(paths.get("reference_stderr", "")),
        "reference_metrics": str(paths.get("reference_metrics", "")),
        "verification_report": str(paths.get("report", "")),
    }


def _verification_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    verification = metrics.get("verification")
    if isinstance(verification, dict):
        return verification
    report_path = metrics.get("verification_report_path")
    if not report_path:
        return {}
    try:
        report = json.loads(Path(str(report_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return compact_verification_report(report) if isinstance(report, dict) else {}


def _node_bottleneck_region(node: SearchNode) -> dict[str, Any]:
    work_unit = node.hypothesis.work_unit or node.planner_context.get("work_unit", {})
    if isinstance(work_unit, dict):
        region = work_unit.get("region")
        if isinstance(region, dict):
            return region
    region = node.planner_context.get("bottleneck_region")
    return region if isinstance(region, dict) else {}


def _candidate_modifications(
    *,
    reference_workspace: Path,
    candidate_workspace: Path,
    editable_paths: list[str],
    metrics_path: str = "metrics.json",
) -> list[dict[str, Any]]:
    modifications: list[dict[str, Any]] = []
    for raw_path in editable_paths:
        relative = Path(raw_path)
        if relative.is_absolute():
            continue
        reference = reference_workspace / relative
        candidate = candidate_workspace / relative
        ignored_paths = set()
        for artifact in (metrics_path, "bottleneck_region.json"):
            try:
                ignored_paths.add(Path(artifact).relative_to(relative))
            except ValueError:
                pass
        status = _path_modification_status(reference, candidate, ignored_paths=ignored_paths)
        if status == "unchanged":
            continue
        modifications.append(
            {
                "path": str(relative),
                "status": status,
                "reference_path": str(reference),
                "candidate_path": str(candidate),
            }
        )
    return modifications


def _path_modification_status(reference: Path, candidate: Path, *, ignored_paths: set[Path] | None = None) -> str:
    if reference.exists() and not candidate.exists():
        return "deleted"
    if candidate.exists() and not reference.exists():
        return "added"
    if not reference.exists() and not candidate.exists():
        return "missing"
    if reference.is_dir() or candidate.is_dir():
        if not reference.is_dir() or not candidate.is_dir():
            return "modified"
        def files(directory: Path) -> dict[str, bytes]:
            return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*")
                    if p.is_file() and not {"__pycache__", ".autopipeline"}.intersection(p.relative_to(directory).parts)
                    and p.relative_to(directory) not in (ignored_paths or set())}
        return "unchanged" if files(reference) == files(candidate) else "modified"
    try:
        return "unchanged" if reference.read_bytes() == candidate.read_bytes() else "modified"
    except OSError:
        return "unknown"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _pipeline_diagnoses(observation: dict[str, Any]) -> list[str]:
    diagnoses: list[str] = []
    end_to_end = _optional_float(observation.get("end_to_end_ms")) or 0.0
    stage_imbalance = _optional_float(observation.get("stage_imbalance")) or 0.0
    communication = _optional_float(observation.get("communication_ms")) or 0.0
    sync = _optional_float(observation.get("sync_ms")) or 0.0
    overlap = _optional_float(observation.get("overlap_ratio"))
    memory_headroom = _optional_float(observation.get("memory_headroom"))

    if stage_imbalance >= 0.20:
        diagnoses.append("stage_imbalance")
    if end_to_end > 0 and communication / end_to_end >= 0.08:
        diagnoses.append("communication_stall")
    if end_to_end > 0 and sync / end_to_end >= 0.04:
        diagnoses.append("synchronization_stall")
    if overlap is not None and overlap < 0.60 and (communication > 0 or sync > 0):
        diagnoses.append("pipeline_overlap_failure")
    if memory_headroom is not None and memory_headroom < 0.20:
        diagnoses.append("memory_pressure")
    if not diagnoses and end_to_end > 0:
        diagnoses.append("end_to_end_latency")
    return diagnoses


def _pipeline_treatment_families(diagnoses: list[str]) -> list[str]:
    families: list[str] = ["pipeline_latency"]
    if "stage_imbalance" in diagnoses:
        families.append("stage_balance")
    if any(item in diagnoses for item in ("communication_stall", "synchronization_stall", "pipeline_overlap_failure")):
        families.append("overlap")
    if "memory_pressure" in diagnoses:
        families.append("memory_headroom")
    return _dedupe_strings([family for family in families if family in _SUPPORTED_PIPELINE_FAMILIES])


def _pipeline_symptoms(observation: dict[str, Any]) -> list[str]:
    symptoms: list[str] = []
    if observation.get("end_to_end_ms") is not None:
        symptoms.append(f"end_to_end_ms={observation['end_to_end_ms']}")
    if observation.get("slowest_stage") is not None:
        symptoms.append(f"slowest_stage={observation['slowest_stage']}:{observation.get('slowest_stage_ms', 'unknown')}ms")
    for key in ("stage_imbalance", "communication_ms", "sync_ms", "overlap_ratio", "memory_headroom", "hardware_fit"):
        if observation.get(key) is not None:
            symptoms.append(f"{key}={observation[key]}")
    return symptoms


def _pipeline_branch_cards(
    *,
    spec: PipelineSpec,
    observation: dict[str, Any],
    diagnoses: list[str],
    treatment_families: list[str],
    limit: int,
) -> list[Hypothesis]:
    symptoms = _pipeline_symptoms(observation)
    slowest_stage = str(observation.get("slowest_stage") or "slowest stage")
    editable_paths = list(spec.editable_paths or [spec.target_path])
    cards: list[Hypothesis] = []
    for family in treatment_families:
        if family == "pipeline_latency":
            cards.append(
                Hypothesis(
                    id="reduce-end-to-end",
                    title="Reduce end-to-end latency",
                    core_hypothesis="Reduce the measured end-to-end latency while preserving the evaluator contract.",
                    idea_id="end-to-end-latency",
                    implementation_variant="combined-stage-overlap-reduction",
                    scope="pipeline",
                    target=spec.target_path,
                    constraints_summary="Edit only declared pipeline workspace files and keep the metrics contract intact.",
                    editable_path_hints=editable_paths,
                    family="pipeline_latency",
                    rationale=_pipeline_rationale(observation, diagnoses, treatment_families),
                    target_signals=[*symptoms[:3], "end_to_end_ms", "throughput"],
                    verification_intent=["end_to_end_ms", "throughput"],
                    supporting_evidence=symptoms,
                    disallowed_directions=_pipeline_disallowed_directions(),
                )
            )
        elif family == "stage_balance":
            cards.append(
                Hypothesis(
                    id="balance-stages",
                    title="Balance the slowest stage",
                    core_hypothesis=f"Reduce latency in {slowest_stage} so the critical path is less stage-bound.",
                    idea_id="stage-balance",
                    implementation_variant="slowest-stage-latency-reduction",
                    scope="pipeline",
                    target=slowest_stage,
                    constraints_summary="Keep the pipeline runner and metrics schema unchanged.",
                    editable_path_hints=editable_paths,
                    family="stage_balance",
                    rationale="Stage imbalance indicates one region dominates the fixed workload.",
                    target_signals=[*symptoms[:3], "stage_imbalance", "slowest_stage_ms"],
                    verification_intent=["stage_imbalance", "slowest_stage_ms", "end_to_end_ms"],
                    supporting_evidence=symptoms,
                    disallowed_directions=_pipeline_disallowed_directions(),
                )
            )
        elif family == "overlap":
            cards.append(
                Hypothesis(
                    id="improve-overlap",
                    title="Improve overlap around communication and sync",
                    core_hypothesis="Reduce exposed communication and synchronization time by increasing useful overlap.",
                    idea_id="pipeline-overlap",
                    implementation_variant="communication-sync-overlap",
                    scope="pipeline",
                    target="communication-sync-boundary",
                    constraints_summary="Do not replace the evaluator with a profiler-only signal.",
                    editable_path_hints=editable_paths,
                    family="overlap",
                    rationale="Communication or synchronization exposure plus low overlap suggests a scheduling/overlap treatment.",
                    target_signals=[*symptoms[:3], "overlap_ratio", "communication_ms", "sync_ms"],
                    verification_intent=["overlap_ratio", "communication_ms", "sync_ms", "end_to_end_ms"],
                    supporting_evidence=symptoms,
                    disallowed_directions=_pipeline_disallowed_directions(),
                )
            )
    return cards[:limit]


def _pipeline_region_graph(spec: PipelineSpec, observation: dict[str, Any], symptoms: list[str]) -> dict[str, Any]:
    slowest_stage = str(observation.get("slowest_stage") or "unknown-stage")
    regions = [
        {
            "id": "pipeline-critical-path",
            "label": spec.target_path,
            "scope": "pipeline",
            "summary": "Fixed-workload end-to-end objective region from the pipeline spec.",
            "signals": symptoms,
            "adjacent": ["slowest-stage", "communication-sync-boundary"],
        },
        {
            "id": "slowest-stage",
            "label": slowest_stage,
            "scope": "stage",
            "summary": "Stage-level region with the largest observed latency.",
            "signals": [
                item
                for item in symptoms
                if item.startswith("slowest_stage") or item.startswith("stage_imbalance")
            ],
            "adjacent": ["pipeline-critical-path"],
        },
        {
            "id": "communication-sync-boundary",
            "label": "communication/synchronization boundary",
            "scope": "pipeline",
            "summary": "Boundary region for communication, synchronization, and overlap signals.",
            "signals": [
                item
                for item in symptoms
                if item.startswith("communication_ms") or item.startswith("sync_ms") or item.startswith("overlap_ratio")
            ],
            "adjacent": ["pipeline-critical-path"],
        },
    ]
    return {"focus_region_id": "pipeline-critical-path", "regions": regions}


def _pipeline_rationale(
    observation: dict[str, Any],
    diagnoses: list[str],
    treatment_families: list[str],
) -> str:
    if not observation:
        return "No baseline metrics are available yet; start with conservative pipeline families."
    return (
        "Baseline metrics show "
        f"{', '.join(_pipeline_symptoms(observation)[:4])}; "
        f"diagnosis={', '.join(diagnoses) or 'none'}; "
        f"treatments={', '.join(treatment_families) or 'none'}."
    )


def _pipeline_hardware_context(spec: PipelineSpec) -> dict[str, Any]:
    metadata = spec.metadata or {}
    hardware = metadata.get("hardware_context")
    if isinstance(hardware, dict):
        return {**hardware, "scope": "fixed"}
    return {
        "scope": "fixed",
        "source": "pipeline_spec_or_local_fixture",
        "note": "No production hardware profiler output is attached to this spec.",
    }


def _hypothesis_to_context(hypothesis: Hypothesis) -> dict[str, Any]:
    return {
        "id": hypothesis.id,
        "title": hypothesis.title,
        "core_hypothesis": hypothesis.core_hypothesis,
        "idea_id": hypothesis.idea_id,
        "implementation_variant": hypothesis.implementation_variant,
        "implementation_steps": list(hypothesis.implementation_steps),
        "scope": hypothesis.scope,
        "target": hypothesis.target,
        "constraints_summary": hypothesis.constraints_summary,
        "editable_path_hints": list(hypothesis.editable_path_hints),
        "family": hypothesis.family,
        "rationale": hypothesis.rationale,
        "target_signals": list(hypothesis.target_signals),
        "verification_intent": list(hypothesis.verification_intent),
        "supporting_evidence": list(hypothesis.supporting_evidence),
        "disallowed_directions": list(hypothesis.disallowed_directions),
        "work_unit": dict(hypothesis.work_unit),
    }


def _with_region_work_unit(
    hypothesis: Hypothesis,
    bottleneck_region: dict[str, Any],
    artifact_path: str,
) -> Hypothesis:
    hypothesis.work_unit = region_work_unit(
        bottleneck_region,
        family=hypothesis.family,
        artifact_path=artifact_path,
    )
    return hypothesis


def _pipeline_disallowed_directions() -> list[str]:
    return [
        "Do not claim production trace analysis without trace input.",
        "Do not introduce cross-kernel fusion or layout propagation unless the spec exposes editable files for it.",
        "Do not change evaluator correctness or metrics semantics.",
    ]


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


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


_SUPPORTED_PIPELINE_FAMILIES = {
    "pipeline_latency",
    "stage_balance",
    "overlap",
    "memory_headroom",
}


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


def setup_pipeline_workspace(spec_path: Path, output_dir: Path) -> dict[str, str]:
    spec = PipelineSpec.load(spec_path)
    output_dir = output_dir.resolve()
    if output_dir == spec.workspace_source or spec.workspace_source in output_dir.parents:
        raise ValueError("Destination must be outside workspace_source")
    if output_dir.exists():
        raise FileExistsError(f"Destination already exists: {output_dir}. Choose a new directory.")
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
