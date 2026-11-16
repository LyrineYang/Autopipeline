from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import autopipeline

from autopipeline.search.task import OptimizationTask
from autopipeline.search.types import (
    EvaluationResult,
    Hypothesis,
    RootMemory,
    SearchNode,
    TaskSnapshot,
)

from .verification_backend import (
    ExecutableRunRequest,
    ExecutableVerificationBackend,
    compact_verification_report,
)
from .planning import build_planner_context, seed_hypotheses
from .spec import PipelineSpec


class PipelineTask(OptimizationTask):
    """Generic pipeline workspace task driven by a JSON spec."""

    def __init__(
        self, *, repo_root: Path, spec_path: Path, run_name: str | None = None,
        initial_candidate: Path | None = None, measurement_only_baseline: bool = False,
    ) -> None:
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
        self.initial_candidate = initial_candidate
        self.measurement_only_baseline = measurement_only_baseline

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
        report = self._verify_pair(
            sandbox,
            node_dir=node_dir,
            artifact_dir=node_dir / "verification",
            candidate_command=self.spec.evaluate_command,
            require_no_regression=not (self.measurement_only_baseline and node.parent_id is None),
            metadata={
                "task": self.spec.name,
                "node_id": node.id,
                "hypothesis_id": node.hypothesis.id,
                "family": node.hypothesis.family,
                "target": node.hypothesis.target,
                "workspace_source": str(self.spec.workspace_source),
                "bottleneck_region": _node_bottleneck_region(node),
                "candidate_modifications": self._modifications(sandbox),
            },
        )
        metrics = dict(report.get("candidate", {}).get("metrics", {}))
        performance = report.get("performance", {}) if isinstance(report.get("performance"), dict) else {}
        metrics["returncode"] = report.get("candidate", {}).get("returncode")
        metrics["score_metric"] = self.spec.score_metric
        score = performance.get("ranking_score")
        if score is None:
            score = float("-inf")
        metrics["ranking_score"] = score
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
        report = self._verify_pair(
            self._last_promoted_path,
            node_dir=self.run_dir,
            artifact_dir=self.run_dir / "final_verification",
            candidate_command=self.spec.final_verify_command,
            require_no_regression=not self.measurement_only_baseline or bool(self._modifications(self._last_promoted_path)),
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

    def _modifications(self, candidate: Path) -> list[dict[str, Any]]:
        return _candidate_modifications(
            reference_workspace=self.spec.workspace_source,
            candidate_workspace=candidate,
            editable_paths=self.spec.editable_paths or [self.spec.target_path],
            metrics_path=self.spec.metrics_path,
        )

    def _verify_pair(
        self,
        candidate: Path,
        *,
        node_dir: Path,
        artifact_dir: Path,
        candidate_command: list[str] | str,
        require_no_regression: bool,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        reference = self._prepare_reference_workspace(artifact_dir)

        def request(role: str, workspace: Path, command: list[str] | str) -> ExecutableRunRequest:
            resolved = self._resolve_command(command, sandbox=workspace, node_dir=node_dir)
            return ExecutableRunRequest(
                label=role,
                workspace=workspace,
                command=resolved,
                metrics_path=self.spec.metrics_path,
                env=self._command_env(
                    sandbox=workspace,
                    node_dir=node_dir,
                    reference_workspace=reference,
                    candidate_workspace=candidate,
                    role=role,
                ),
                timeout_seconds=self.spec.timeout_seconds,
                shell=isinstance(resolved, str),
            )

        return self.verification_backend.verify(
            reference=request("reference", reference, self.spec.evaluate_command),
            candidate=request("candidate", candidate, candidate_command),
            artifact_dir=artifact_dir,
            score_metric=self.spec.score_metric,
            score_direction=self.spec.score_direction,
            require_no_regression=require_no_regression,
            rank_by_speedup=self.spec.is_native and self.spec.score_metric in {"forward_latency_ms", "end_to_end_ms"},
            metadata=metadata,
        )

    def seed_hypotheses(self, parent: SearchNode, root: RootMemory, limit: int) -> list[Hypothesis]:
        return seed_hypotheses(self.spec, parent, root, limit)

    def planner_context(
        self, parent: SearchNode, root: RootMemory, limit: int,
        *, upper_bounds: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return build_planner_context(
            self.spec, self.snapshot(), parent, root, limit,
            run_dir=self.run_dir, upper_bounds=upper_bounds,
        )

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
            "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(autopipeline.__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", "")])),
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
                "hypothesis": asdict(node.hypothesis),
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

    def augment_agent_feedback(
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


def default_pipeline_run_dir(repo_root: Path, name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return repo_root / "workspace" / "pipeline_runs" / f"{stamp}-{name}"


def _format_command_part(part: str, *, sandbox: Path, node_dir: Path) -> str:
    return part.format(
        python=sys.executable,
        workspace=str(sandbox),
        node_dir=str(node_dir),
    )


def _pipeline_summary(metrics: dict[str, Any], accepted: bool) -> str:
    parts = [f"accepted={accepted}"]
    for key in ("end_to_end_ms", "throughput", "stage_imbalance", "overlap_ratio"):
        if key in metrics:
            parts.append(f"{key}={metrics[key]}")
    return " ".join(parts)


def __getattr__(name: str):
    # Preserve old imports without loading synthetic fixtures in the product path.
    if name in {"PipelineSmokeAgent", "create_pipeline_smoke_fixture", "run_pipeline_smoke"}:
        from . import smoke
        return getattr(smoke, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
