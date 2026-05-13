from __future__ import annotations

import ast
import json
import re
import shlex
import shutil
import os
import subprocess
from pathlib import Path
from typing import Any

from .autokernel_probes import detect_hardware_context, run_autokernel_probe
from .prior import kernel_optimization_prior, kernel_prior_family_ids
from .task import OptimizationTask, create_copy_sandbox
from .types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot

_KERNEL_TYPE_RE = re.compile(r'^\s*KERNEL_TYPE\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_BACKEND_RE = re.compile(r'^\s*BACKEND\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_MODEL_SHAPES_RE = re.compile(r"^\s*MODEL_SHAPES\s*=\s*(.+)$", re.MULTILINE)
_EXTRACTED_KERNEL_RE = re.compile(r"^kernel_(?P<kernel_type>.+)_(?P<rank>\d+)$")
_RANK_DOC_RE = re.compile(r"Rank:\s*(?P<rank>\d+)\s*\((?P<pct_total>[\d.]+)% of GPU time\)")
_MODEL_SHAPE_DOC_RE = re.compile(r"Model shape:\s*(?P<shape>.+)")
_PROFILE_SCOPE_DOC_RE = re.compile(r"(?:Optimization|Profile) scope:\s*(?P<scope>.+)")
_PIPELINE_REGION_DOC_RE = re.compile(r"(?:Pipeline|Focus) region:\s*(?P<region>.+)")
_UPSTREAM_REGIONS_DOC_RE = re.compile(r"Upstream regions:\s*(?P<regions>.+)")
_DOWNSTREAM_REGIONS_DOC_RE = re.compile(r"Downstream regions:\s*(?P<regions>.+)")
_PROFILE_BOTTLENECKS_DOC_RE = re.compile(r"(?:Profile bottlenecks|Bottleneck hints):\s*(?P<hints>.+)")

_KERNEL_LOCAL_FAMILIES = {
    *kernel_prior_family_ids(include_aliases=True),
    "memory_hierarchy",
    "parallelization",
    "occupancy_launch",
    "latency_hiding",
    "instruction_selection",
    "control_flow_specialization",
}


def parse_bench_output(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "correctness": _match_string(text, "correctness") or "UNKNOWN",
        "bottleneck": _match_string(text, "bottleneck") or "unknown",
        "throughput_tflops": _match_float(text, "throughput_tflops"),
        "bandwidth_gb_s": _match_float(text, "bandwidth_gb_s"),
        "speedup_vs_pytorch": _match_float(text, "speedup_vs_pytorch"),
        "pct_peak_compute": _match_float(text, "pct_peak_compute"),
        "pct_peak_bandwidth": _match_float(text, "pct_peak_bandwidth"),
        "peak_vram_mb": _match_float(text, "peak_vram_mb"),
        "bench_time_seconds": _match_float(text, "bench_time_seconds"),
    }
    return metrics


def parse_verify_output(text: str) -> dict[str, Any]:
    return {
        "correctness": _match_string(text, "correctness") or "UNKNOWN",
        "end_to_end_speedup": _match_float(text, "end_to_end_speedup"),
    }


def extract_kernel_type(path: Path) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"

    match = _KERNEL_TYPE_RE.search(source)
    return match.group(1) if match else "unknown"


class AutoKernelTask(OptimizationTask):
    def __init__(
        self,
        *,
        repo_root: Path,
        kernel_file: Path,
        bench_args: list[str] | None = None,
        verify_command: str | None = None,
        run_name: str | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.kernel_file = kernel_file.resolve()
        self.bench_args = list(bench_args or [])
        self.verify_command = verify_command
        self.run_name = run_name or self.kernel_file.stem
        self.kernel_type = extract_kernel_type(self.kernel_file)
        self.profile_prior = extract_profile_prior(self.kernel_file)
        self.run_dir: Path | None = None
        self.run_workspace: Path | None = None
        self._last_promoted_path: Path | None = None
        self._hardware_context_cache: dict[str, Any] | None = None

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            task_type="autokernel",
            objective=(
                f"Optimize {self.kernel_file.name} while preserving correctness and improving "
                "speedup_vs_pytorch."
            ),
            target_path=str(self.kernel_file),
            target_kind="kernel_file",
            surface_type="single-file-copy",
            objective_signals=["speedup_vs_pytorch", "correctness"],
            editable_paths=["kernel.py"],
            constraints=[
                "Do not modify bench.py, verify.py, orchestrate.py, reference.py, or other repo control files.",
                "Keep code changes inside the generated sandbox surface for the current branch.",
                "Treat benchmark and verifier output as read-only evidence.",
            ],
            reference_paths=[
                str(self.repo_root / "program.md"),
                str(self.repo_root / "bench.py"),
                str(self.repo_root / "verify.py"),
                str(self.repo_root / "reference.py"),
            ],
            metadata={
                "repo_root": str(self.repo_root),
                "kernel_type": self.kernel_type,
                "run_name": self.run_name,
            },
        )

    def prepare_run(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.run_workspace = self.run_dir / "workspace"
        self.run_workspace.mkdir(parents=True, exist_ok=True)

    def create_sandbox(
        self,
        node: SearchNode,
        parent: SearchNode | None,
    ) -> None:
        source_candidate = Path(parent.candidate_path) if parent is not None else self.kernel_file
        sandbox_dir = Path(node.node_dir) / "sandbox"
        sandbox_path, candidate_path = create_copy_sandbox(
            source_candidate,
            sandbox_dir,
            candidate_name="kernel.py",
        )
        node.sandbox_path = sandbox_path
        node.candidate_path = candidate_path

    def evaluate(self, node: SearchNode) -> EvaluationResult:
        if not node.sandbox_path:
            raise RuntimeError("Sandbox must be created before evaluation.")

        node_dir = Path(node.node_dir)
        stdout_path = node_dir / "bench.stdout.log"
        stderr_path = node_dir / "bench.stderr.log"
        command = [
            "uv",
            "run",
            "--project",
            str(self.repo_root),
            "--directory",
            node.sandbox_path,
            "--no-sync",
            str(self.repo_root / "bench.py"),
            *self.bench_args,
        ]
        result = subprocess.run(
            command,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        metrics = parse_bench_output(result.stdout + ("\n" + result.stderr if result.stderr else ""))
        metrics["returncode"] = result.returncode
        score = float(metrics.get("speedup_vs_pytorch") or 0.0)
        metrics["score_metric"] = "speedup_vs_pytorch"
        metrics["score_value"] = score
        accepted = result.returncode == 0 and metrics.get("correctness") == "PASS"
        summary = (
            f"correctness={metrics['correctness']} "
            f"speedup_vs_pytorch={score:.3f}x "
            f"throughput_tflops={float(metrics.get('throughput_tflops') or 0.0):.3f}"
        )
        return EvaluationResult(
            accepted=accepted,
            score=score,
            metrics=metrics,
            summary=summary,
            raw_log_paths={
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
        )

    def promote(self, node: SearchNode, root: RootMemory) -> str | None:
        promoted_path = self.kernel_file.with_name(f"{self.kernel_file.stem}_optimized.py")
        shutil.copy2(node.candidate_path, promoted_path)
        self._last_promoted_path = promoted_path
        return str(promoted_path)

    def final_verify(self, root: RootMemory) -> dict | None:
        if not self.verify_command or not self.run_workspace or not self.run_dir:
            return None
        if self._last_promoted_path is None:
            return None

        verify_candidate = self._prepare_verify_workspace(self._last_promoted_path)
        command = self._build_verify_command(self.run_workspace, verify_candidate)
        stdout_path = self.run_dir / "final_verify.stdout.log"
        stderr_path = self.run_dir / "final_verify.stderr.log"
        env = {
            **os.environ,
            "TREE_WORKSPACE_PATH": str(self.run_workspace),
            "TREE_OPTIMIZED_PATH": str(verify_candidate),
        }
        result = subprocess.run(
            command,
            cwd=str(self.repo_root),
            shell=True,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        metrics = parse_verify_output(result.stdout + ("\n" + result.stderr if result.stderr else ""))
        metrics["returncode"] = result.returncode
        accepted = result.returncode == 0 and metrics.get("correctness") == "PASS"
        return {
            "accepted": accepted,
            "correctness": metrics.get("correctness"),
            "end_to_end_speedup": float(metrics.get("end_to_end_speedup") or 0.0),
            "summary": (
                f"correctness={metrics.get('correctness')} "
                f"end_to_end_speedup={float(metrics.get('end_to_end_speedup') or 0.0):.3f}x"
            ),
            "raw_log_paths": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
        }

    def seed_hypotheses(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
    ) -> list[Hypothesis]:
        kernel_type = self.kernel_type if self.kernel_type != "unknown" else "kernel"
        hypotheses = [
            Hypothesis(
                id="tune-launch",
                title="Tune launch geometry",
                core_hypothesis=(
                    "Adjust launch, block, or tile parameters to better match the current "
                    "bottleneck without changing semantics."
                ),
                scope="kernel",
                target=self.kernel_file.name,
                constraints_summary="Keep interfaces, numerics, and output shape unchanged.",
                editable_path_hints=["kernel.py"],
                verification_intent=["speedup_vs_pytorch", "correctness"],
            ),
            Hypothesis(
                id="memory-access",
                title="Improve memory access",
                core_hypothesis=(
                    "Change staging, vectorization, or data movement to improve memory access "
                    "efficiency for this candidate."
                ),
                scope="kernel",
                target=self.kernel_file.name,
                constraints_summary="Do not change the reference interface or benchmark logic.",
                editable_path_hints=["kernel.py"],
                verification_intent=["speedup_vs_pytorch", "correctness"],
            ),
            Hypothesis(
                id="reduce-overhead",
                title="Reduce local overhead",
                core_hypothesis=(
                    "Reduce synchronization, temporary state, or register pressure in the "
                    "current implementation while preserving behavior."
                ),
                scope="kernel",
                target=self.kernel_file.name,
                constraints_summary="Keep the algorithm local to the current candidate.",
                editable_path_hints=["kernel.py"],
                verification_intent=["speedup_vs_pytorch", "correctness"],
            ),
            Hypothesis(
                id="algorithm-local",
                title="Try one local algorithmic variant",
                core_hypothesis=(
                    f"Try one local {kernel_type}-specific algorithmic variant aligned to the "
                    "current bottleneck signal."
                ),
                scope="kernel",
                target=self.kernel_file.name,
                constraints_summary="Only one focused algorithmic change for this branch.",
                editable_path_hints=["kernel.py"],
                verification_intent=["speedup_vs_pytorch", "correctness"],
            ),
        ]
        return hypotheses[:limit]

    def prepare_execution_surface(self, node: SearchNode, parent: SearchNode | None) -> None:
        scope = _normalize_scope(node.planner_context.get("recommended_scope"))
        region_graph = _build_region_graph(
            kernel_file=self.kernel_file,
            kernel_type=self.kernel_type,
            static_prior=self.profile_prior,
            hardware_context=self._hardware_context(),
        )
        focus_region = next(
            (region for region in region_graph["regions"] if region["id"] == region_graph["focus_region_id"]),
            region_graph["regions"][0],
        )
        scope_summary = _execution_scope_summary(scope, focus_region)
        context_files: list[str] = []
        if scope in {"region", "pipeline"}:
            context_payload = {
                "scope": scope,
                "focus_region": focus_region,
                "adjacent_regions": [
                    region
                    for region in region_graph["regions"]
                    if region["id"] in set(focus_region.get("adjacent", []))
                ],
                "profile_hints": _profile_bottleneck_hints(self.profile_prior),
                "problem_summary": _problem_summary(
                    kernel_file=self.kernel_file,
                    kernel_type=self.kernel_type,
                    static_prior=self.profile_prior,
                ),
            }
            context_path = Path(node.sandbox_path) / f"{scope}_context.json"
            context_path.write_text(json.dumps(context_payload, indent=2), encoding="utf-8")
            summary_path = Path(node.sandbox_path) / f"{scope}_context.md"
            summary_path.write_text(scope_summary + "\n", encoding="utf-8")
            context_files = [context_path.name, summary_path.name]

        node.planner_context.update(
            {
                "effective_scope": scope,
                "effective_editable_paths": ["kernel.py"],
                "focus_target": focus_region["label"],
                "scope_summary": scope_summary,
                "scope_context_files": context_files,
            }
        )

    def planner_context(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
        *,
        upper_bounds: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        hardware_context = self._hardware_context()
        evaluator_prior = _evaluator_prior(parent)
        history_context = {
            "explored_hypotheses": root.explored_hypotheses[-8:],
            "positive_insights": root.positive_insights[-5:],
            "negative_insights": root.negative_insights[-5:],
            "neutral_insights": root.neutral_insights[-5:],
        }
        region_graph = _build_region_graph(
            kernel_file=self.kernel_file,
            kernel_type=self.kernel_type,
            static_prior=self.profile_prior,
            hardware_context=hardware_context,
            evaluator_prior=evaluator_prior,
        )
        return {
            "static_prior": self.profile_prior,
            "optimization_prior": kernel_optimization_prior(),
            "hardware_context": hardware_context,
            "evaluator_prior": evaluator_prior,
            "history_context": history_context,
            "region_graph": region_graph,
            "symptoms": _symptoms_from_context(
                static_prior=self.profile_prior,
                hardware_context=hardware_context,
                evaluator_prior=evaluator_prior,
                history_context=history_context,
            ),
            "profile_hints": _profile_bottleneck_hints(self.profile_prior),
            "problem_summary": _problem_summary(
                kernel_file=self.kernel_file,
                kernel_type=self.kernel_type,
                static_prior=self.profile_prior,
            ),
            "planner_rules": {
                "allowed_scopes": ["kernel", "region", "pipeline"],
                "allowed_families": sorted(_allowed_branch_families_for_task()),
                "editable_paths": ["kernel.py"],
                "avoid_families": [],
                "resource_ceiling": upper_bounds or {},
            },
            "frozen_decisions": [
                "Current AutoKernelTask keeps code edits inside the branch-local sandbox surface.",
                "Region or pipeline scope may widen execution context, but direct code edits still land in the generated sandbox surface.",
                "Beam, round, and child counts are ceilings; the planner may tighten them but must not exceed them.",
                "Planner output should freeze the high-level direction so the executor can focus on implementation.",
            ],
        }

    def branch_iteration_limit(self, parent: SearchNode, node: SearchNode) -> int:
        if "--quick" in self.bench_args:
            return 2
        return 3

    def handle_probe(
        self,
        request: dict[str, Any],
        *,
        cwd: Path,
        node: SearchNode | None,
        parent: SearchNode | None,
    ) -> dict[str, Any]:
        return run_autokernel_probe(
            request,
            cwd=cwd,
            repo_root=self.repo_root,
            bench_args=self.bench_args,
        )

    def build_python_probe_command(
        self,
        *,
        cwd: Path,
        script_path: Path,
        args: list[str],
    ) -> list[str]:
        if not (self.repo_root / "pyproject.toml").exists():
            return ["python3", str(script_path), *args]
        return [
            "uv",
            "run",
            "--project",
            str(self.repo_root),
            "--directory",
            str(cwd.resolve()),
            "--no-sync",
            "python3",
            str(script_path),
            *args,
        ]

    def probe_environment(self, *, cwd: Path) -> dict[str, str]:
        probe_dir = cwd / ".tree_probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        return {
            **os.environ,
            "UV_CACHE_DIR": str((probe_dir / "uv-cache").resolve()),
        }

    def build_theoretical_limit_inputs(
        self,
        *,
        cwd: Path,
        node: SearchNode | None,
        parent: SearchNode | None,
    ) -> dict[str, Any]:
        current = node if node is not None else parent
        observed = {}
        if current is not None and current.evaluation_result is not None:
            observed = {
                "summary": current.evaluation_result.summary,
                "score": current.evaluation_result.score,
                "metrics": dict(current.evaluation_result.metrics),
            }
        return {
            "hardware": self._hardware_context(),
            "workload": {
                "kernel_type": self.kernel_type,
                "backend": self.profile_prior.get("backend"),
                "model_shapes": self.profile_prior.get("model_shapes"),
                "optimization_scope": self.profile_prior.get("optimization_scope"),
                "pipeline_region_label": self.profile_prior.get("pipeline_region_label"),
                "profile_bottlenecks": self.profile_prior.get("profile_bottlenecks", []),
            },
            "candidate": {
                "cwd": str(cwd),
                "target_path": str(self.kernel_file),
                "candidate_path": current.candidate_path if current is not None else "",
            },
            "observed": observed,
        }

    def _prepare_verify_workspace(self, promoted_path: Path) -> Path:
        assert self.run_workspace is not None
        verify_name = self._derive_verify_filename(promoted_path)
        verify_target = self.run_workspace / verify_name
        shutil.copy2(promoted_path, verify_target)
        return verify_target

    def _derive_verify_filename(self, promoted_path: Path) -> str:
        match = _EXTRACTED_KERNEL_RE.match(self.kernel_file.stem)
        if match:
            return f"{self.kernel_file.stem}_optimized.py"

        kernel_type = extract_kernel_type(promoted_path)
        return f"kernel_{kernel_type}_1_optimized.py"

    def _build_verify_command(self, run_workspace: Path, verify_candidate: Path) -> str:
        assert self.verify_command is not None
        replacements = {
            "workspace": shlex.quote(str(run_workspace)),
            "optimized": shlex.quote(str(verify_candidate)),
            "repo_root": shlex.quote(str(self.repo_root)),
            "kernel_file": shlex.quote(str(self.kernel_file)),
        }

        try:
            command = self.verify_command.format(**replacements)
        except KeyError:
            command = self.verify_command

        if "verify.py" in command and "--workspace" not in command:
            command = f"{command} --workspace {shlex.quote(str(run_workspace))}"
        return command

    def _hardware_context(self) -> dict[str, Any]:
        if self._hardware_context_cache is None:
            self._hardware_context_cache = detect_hardware_context()
        return self._hardware_context_cache


def _match_string(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+)$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _match_float(text: str, key: str) -> float | None:
    raw = _match_string(text, key)
    if raw is None:
        return None
    normalized = raw.strip().rstrip("x%")
    try:
        return float(normalized)
    except ValueError:
        return None


def extract_profile_prior(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return {"kernel_type": "unknown"}

    prior: dict[str, Any] = {
        "kernel_type": extract_kernel_type(path),
        "backend": _match_regex_group(_BACKEND_RE, source) or "triton",
        "source_file": str(path),
    }

    rank_match = _RANK_DOC_RE.search(source)
    if rank_match:
        prior["rank"] = int(rank_match.group("rank"))
        prior["pct_total_gpu_time"] = float(rank_match.group("pct_total"))

    shape_doc_match = _MODEL_SHAPE_DOC_RE.search(source)
    if shape_doc_match:
        prior["model_shape_display"] = shape_doc_match.group("shape").strip()

    scope_doc_match = _PROFILE_SCOPE_DOC_RE.search(source)
    if scope_doc_match:
        prior["optimization_scope"] = _normalize_scope(scope_doc_match.group("scope"))

    region_doc_match = _PIPELINE_REGION_DOC_RE.search(source)
    if region_doc_match:
        prior["pipeline_region_label"] = region_doc_match.group("region").strip()

    upstream_doc_match = _UPSTREAM_REGIONS_DOC_RE.search(source)
    if upstream_doc_match:
        prior["upstream_regions"] = _split_csv_values(upstream_doc_match.group("regions"))

    downstream_doc_match = _DOWNSTREAM_REGIONS_DOC_RE.search(source)
    if downstream_doc_match:
        prior["downstream_regions"] = _split_csv_values(downstream_doc_match.group("regions"))

    bottleneck_doc_match = _PROFILE_BOTTLENECKS_DOC_RE.search(source)
    if bottleneck_doc_match:
        prior["profile_bottlenecks"] = _split_csv_values(bottleneck_doc_match.group("hints"))

    model_shapes = _literal_assignment(source, _MODEL_SHAPES_RE)
    if isinstance(model_shapes, dict):
        prior["model_shapes"] = model_shapes

    return prior

def _build_region_graph(
    *,
    kernel_file: Path,
    kernel_type: str,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    evaluator_prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    explicit_regions = static_prior.get("pipeline_regions") or static_prior.get("region_graph")
    if isinstance(explicit_regions, list):
        regions = _normalize_explicit_regions(explicit_regions)
        if regions:
            return {
                "focus_region_id": str(static_prior.get("focus_region_id", regions[0]["id"])),
                "regions": regions,
            }

    scope = _normalize_scope(static_prior.get("optimization_scope"))
    focus_id = "current-kernel"
    focus_label = str(static_prior.get("pipeline_region_label") or kernel_file.name)
    focus_scope = "kernel" if scope == "kernel" else "region"
    focus_region = {
        "id": focus_id,
        "label": focus_label,
        "scope": focus_scope,
        "summary": _region_summary(
            kernel_type=kernel_type,
            static_prior=static_prior,
            evaluator_prior=evaluator_prior,
            scope=scope,
        ),
        "signals": _region_signals(
            static_prior=static_prior,
            hardware_context=hardware_context,
            evaluator_prior=evaluator_prior,
        ),
        "adjacent": [],
    }
    regions = [focus_region]
    for direction, items in (
        ("upstream", _string_values(static_prior.get("upstream_regions"))),
        ("downstream", _string_values(static_prior.get("downstream_regions"))),
    ):
        for index, label in enumerate(items[:2]):
            region_id = f"{direction}-{index + 1}"
            regions.append(
                {
                    "id": region_id,
                    "label": label,
                    "scope": "region" if scope == "pipeline" else "kernel",
                    "summary": f"{direction.title()} region connected to the current optimization target.",
                    "signals": [],
                    "adjacent": [focus_id],
                }
            )
            focus_region["adjacent"].append(region_id)

    return {
        "focus_region_id": focus_id,
        "regions": regions,
    }


def _region_summary(
    *,
    kernel_type: str,
    static_prior: dict[str, Any],
    evaluator_prior: dict[str, Any] | None,
    scope: str,
) -> str:
    metrics = _dict_metrics((evaluator_prior or {}).get("metrics"))
    bottleneck = str(metrics.get("bottleneck", "unknown")).lower()
    rank = static_prior.get("rank")
    region_label = static_prior.get("pipeline_region_label")
    if scope == "pipeline" and region_label:
        return f"Pipeline focus region {region_label}; current local evidence points to {bottleneck} pressure."
    if scope == "region" and region_label:
        return f"Focused region {region_label}; current local evidence points to {bottleneck} pressure."
    if rank is not None:
        return f"Hot {kernel_type} region from extracted rank {rank}; current local bottleneck looks {bottleneck}."
    return f"Hot {kernel_type} region; current local bottleneck looks {bottleneck}."


def _region_signals(
    *,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    evaluator_prior: dict[str, Any] | None,
) -> list[str]:
    signals: list[str] = []
    signals.append(f"scope={_normalize_scope(static_prior.get('optimization_scope'))}")
    if static_prior.get("pct_total_gpu_time") is not None:
        signals.append(f"pct_total_gpu_time={static_prior['pct_total_gpu_time']}")
    if static_prior.get("model_shape_display"):
        signals.append(f"model_shape={static_prior['model_shape_display']}")
    profile_bottlenecks = _profile_bottleneck_hints(static_prior)
    if profile_bottlenecks:
        signals.append(f"profile_bottlenecks={','.join(profile_bottlenecks[:2])}")
    metrics = _dict_metrics((evaluator_prior or {}).get("metrics"))
    if metrics.get("bottleneck"):
        signals.append(f"local_bottleneck={metrics['bottleneck']}")
    if (evaluator_prior or {}).get("score") is not None:
        signals.append(f"last_score={evaluator_prior['score']}")
    if hardware_context.get("gpu_name"):
        signals.append(f"gpu={hardware_context['gpu_name']}")
    return signals[:4]


def _symptoms_from_context(
    *,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    evaluator_prior: dict[str, Any] | None = None,
    history_context: dict[str, Any],
) -> list[str]:
    symptoms: list[str] = []
    symptoms.append(f"scope={_normalize_scope(static_prior.get('optimization_scope'))}")
    if static_prior.get("pct_total_gpu_time") is not None:
        symptoms.append(f"pct_total_gpu_time={static_prior['pct_total_gpu_time']}")
    if static_prior.get("model_shape_display"):
        symptoms.append(f"model_shape={static_prior['model_shape_display']}")
    if static_prior.get("pipeline_region_label"):
        symptoms.append(f"focus_region={static_prior['pipeline_region_label']}")
    profile_bottlenecks = _profile_bottleneck_hints(static_prior)
    if profile_bottlenecks:
        symptoms.append(f"profile_bottlenecks={','.join(profile_bottlenecks[:3])}")
    metrics = _dict_metrics((evaluator_prior or {}).get("metrics"))
    if metrics.get("bottleneck"):
        symptoms.append(f"local_bottleneck={metrics['bottleneck']}")
    if metrics.get("score_metric") and metrics.get("score_value") is not None:
        symptoms.append(f"{metrics['score_metric']}={metrics['score_value']}")
    if (evaluator_prior or {}).get("accepted") is not None:
        symptoms.append(f"last_accepted={str(bool(evaluator_prior['accepted'])).lower()}")
    if (evaluator_prior or {}).get("score") is not None:
        symptoms.append(f"last_score={evaluator_prior['score']}")
    if hardware_context.get("gpu_name"):
        symptoms.append(f"gpu={hardware_context['gpu_name']}")
    if hardware_context.get("compute_capability"):
        symptoms.append(f"compute_capability={hardware_context['compute_capability']}")
    if history_context.get("negative_insights"):
        symptoms.append(f"recent_negative_insights={len(history_context['negative_insights'])}")
    return symptoms[:8]


def _allowed_branch_families_for_task() -> set[str]:
    return set(_KERNEL_LOCAL_FAMILIES)


def _evaluator_prior(node: SearchNode) -> dict[str, Any]:
    if node.evaluation_result is None:
        return {}
    return {
        "accepted": node.evaluation_result.accepted,
        "score": node.evaluation_result.score,
        "summary": node.evaluation_result.summary,
        "metrics": dict(node.evaluation_result.metrics),
        "raw_log_paths": dict(node.evaluation_result.raw_log_paths),
    }


def _profile_bottleneck_hints(static_prior: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    for key in ("profile_bottlenecks", "bottleneck_hints", "pipeline_bottlenecks"):
        hints.extend(_string_values(static_prior.get(key)))
    if static_prior.get("dominant_bottleneck"):
        hints.append(str(static_prior["dominant_bottleneck"]))
    normalized: list[str] = []
    for hint in hints:
        value = _normalize_hint(hint)
        canonical = _diagnosis_from_hint(value)
        if canonical:
            value = canonical
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _diagnosis_from_hint(hint: str) -> str | None:
    return {
        "memory_bandwidth_pressure": "memory_bandwidth_pressure",
        "memory": "memory_bandwidth_pressure",
        "memory_bandwidth": "memory_bandwidth_pressure",
        "compute_saturation": "compute_saturation",
        "compute": "compute_saturation",
        "synchronization_pressure": "synchronization_pressure",
        "sync": "synchronization_pressure",
        "synchronization": "synchronization_pressure",
        "launch_overhead": "launch_overhead",
        "launch": "launch_overhead",
        "communication_pressure": "communication_pressure",
        "communication": "communication_pressure",
        "kernel_boundary_overhead": "kernel_boundary_overhead",
        "boundary": "kernel_boundary_overhead",
        "fusion": "kernel_boundary_overhead",
        "layout_or_boundary_sensitivity": "layout_or_boundary_sensitivity",
        "layout": "layout_or_boundary_sensitivity",
        "baseline_underperformance": "baseline_underperformance",
        "uncertain_local_bottleneck": "uncertain_local_bottleneck",
        "overlap": "synchronization_pressure",
    }.get(hint)


def _normalize_hint(value: Any) -> str:
    text = str(value).strip().lower()
    if not text:
        return ""
    text = text.replace("-", "_").replace(" ", "_")
    for prefix in ("bottleneck=", "local_bottleneck=", "profile_bottleneck="):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _normalize_explicit_regions(items: list[Any]) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            label = item.strip()
            if not label:
                continue
            regions.append(
                {
                    "id": f"region-{index + 1}",
                    "label": label,
                    "scope": "region",
                    "summary": "",
                    "signals": [],
                    "adjacent": [],
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        region_id = str(item.get("id", f"region-{index + 1}"))
        regions.append(
            {
                "id": region_id,
                "label": str(item.get("label", region_id)),
                "scope": _normalize_scope(item.get("scope")),
                "summary": str(item.get("summary", "")),
                "signals": _string_values(item.get("signals")),
                "adjacent": _string_values(item.get("adjacent")),
            }
        )
    return regions


def _execution_scope_summary(scope: str, focus_region: dict[str, Any]) -> str:
    label = str(focus_region.get("label", "current target"))
    if scope == "pipeline":
        return f"Pipeline-scope branch focused on {label}."
    if scope == "region":
        return f"Region-scope branch focused on {label}."
    return f"Kernel-scope branch focused on {label}."


def _problem_summary(
    *,
    kernel_file: Path,
    kernel_type: str,
    static_prior: dict[str, Any],
) -> str:
    scope = _normalize_scope(static_prior.get("optimization_scope"))
    focus_region = str(static_prior.get("pipeline_region_label") or kernel_file.name)
    rank = static_prior.get("rank")
    shape = static_prior.get("model_shape_display")
    parts = [f"Current target is a hot {kernel_type} kernel"]
    if rank is not None:
        parts.append(f"from extracted rank {rank}")
    if scope != "kernel":
        parts.append(f"inside {scope} scope around {focus_region}")
    if shape:
        parts.append(f"with shape {shape}")
    return " ".join(parts) + "."


def _normalize_scope(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"pipeline", "stage_graph", "graph"}:
        return "pipeline"
    if text in {"region", "subgraph"}:
        return "region"
    return "kernel"


def _split_csv_values(text: str) -> list[str]:
    return [part.strip() for part in str(text).split(",") if part.strip()]


def _string_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return _split_csv_values(value)
    return []


def _literal_assignment(source: str, pattern: re.Pattern[str]) -> Any:
    match = pattern.search(source)
    if not match:
        return None
    try:
        return ast.literal_eval(match.group(1).strip())
    except (SyntaxError, ValueError):
        return None


def _match_regex_group(pattern: re.Pattern[str], source: str) -> str | None:
    match = pattern.search(source)
    if not match:
        return None
    return match.group(1).strip()


def _dict_metrics(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
