from __future__ import annotations

import ast
import re
import shlex
import shutil
import os
import subprocess
from pathlib import Path
from typing import Any

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
            editable_paths=["kernel.py"],
            constraints=[
                "Do not modify bench.py, verify.py, orchestrate.py, reference.py, or other repo control files.",
                "Only change the sandbox-local kernel.py candidate.",
                "Treat benchmark and verifier output as read-only evidence.",
            ],
            reference_paths=[
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
                constraints_summary="Keep interfaces, numerics, and output shape unchanged.",
                editable_path_hints=["kernel.py"],
            ),
            Hypothesis(
                id="memory-access",
                title="Improve memory access",
                core_hypothesis=(
                    "Change staging, vectorization, or data movement to improve memory access "
                    "efficiency for this candidate."
                ),
                constraints_summary="Do not change the reference interface or benchmark logic.",
                editable_path_hints=["kernel.py"],
            ),
            Hypothesis(
                id="reduce-overhead",
                title="Reduce local overhead",
                core_hypothesis=(
                    "Reduce synchronization, temporary state, or register pressure in the "
                    "current implementation while preserving behavior."
                ),
                constraints_summary="Keep the algorithm local to the current candidate.",
                editable_path_hints=["kernel.py"],
            ),
            Hypothesis(
                id="algorithm-local",
                title="Try one local algorithmic variant",
                core_hypothesis=(
                    f"Try one local {kernel_type}-specific algorithmic variant aligned to the "
                    "current bottleneck signal."
                ),
                constraints_summary="Only one focused algorithmic change for this branch.",
                editable_path_hints=["kernel.py"],
            ),
        ]
        return hypotheses[:limit]

    def watchman_context(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
    ) -> dict[str, Any]:
        benchmark_context = _benchmark_context(parent)
        hardware_context = self._hardware_context()
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
            benchmark_context=benchmark_context,
        )
        bottleneck_card = _build_bottleneck_card(
            kernel_file=self.kernel_file,
            kernel_type=self.kernel_type,
            static_prior=self.profile_prior,
            hardware_context=hardware_context,
            benchmark_context=benchmark_context,
            history_context=history_context,
            region_graph=region_graph,
        )
        avoid_families = _string_values(bottleneck_card.get("avoid_families"))
        branch_families = _recommend_branch_families(
            kernel_type=self.kernel_type,
            scope=str(bottleneck_card.get("scope", "kernel")),
            diagnoses=_string_values(bottleneck_card.get("diagnoses")),
            allowed_families=_allowed_branch_families_for_task(),
            avoid_families=avoid_families,
            static_prior=self.profile_prior,
            hardware_context=hardware_context,
            benchmark_context=benchmark_context,
            history_context=history_context,
            limit=limit,
        )
        bottleneck_card["response_families"] = [
            str(entry.get("family", "")) for entry in branch_families if entry.get("family")
        ]
        search_policy = _search_policy_hint(
            bottleneck_card=bottleneck_card,
            benchmark_context=benchmark_context,
            history_context=history_context,
            limit=limit,
        )
        return {
            "static_prior": self.profile_prior,
            "hardware_context": hardware_context,
            "benchmark_context": benchmark_context,
            "history_context": history_context,
            "region_graph": region_graph,
            "bottleneck_card": bottleneck_card,
            "search_policy": search_policy,
            "branch_families": branch_families,
            "frozen_decisions": [
                "Keep branch identities aligned to distinct mechanism families.",
                "Use profile facts, hardware context, and recent insights as priors before proposing variants.",
                "Treat the region graph as the location prior and the bottleneck card as the diagnosis prior.",
                "Current AutoKernelTask still edits only sandbox-local kernel.py, so response families stay kernel-local even if profile hints come from a wider pipeline context.",
                "Treat CLI beam and round settings as upper bounds; let watchman tighten them when the diagnosis is clear.",
                "Executor should stay inside the chosen family instead of reopening high-level direction search.",
            ],
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


def detect_hardware_context() -> dict[str, Any]:
    context: dict[str, Any] = {
        "gpu_name": "unknown",
        "gpu_count": 0,
        "driver_version": "unknown",
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "all"),
        "torch_cuda_available": False,
    }

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines:
                first = [part.strip() for part in lines[0].split(",")]
                context["gpu_count"] = len(lines)
                context["gpu_name"] = first[0]
                if len(first) > 1:
                    context["memory_total_mb"] = float(first[1])
                if len(first) > 2:
                    context["driver_version"] = first[2]
    except OSError:
        pass

    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            device_index = 0
            props = torch.cuda.get_device_properties(device_index)
            context["torch_cuda_available"] = True
            context["gpu_count"] = max(context.get("gpu_count", 0), torch.cuda.device_count())
            context["gpu_name"] = props.name
            context["compute_capability"] = f"{props.major}.{props.minor}"
            context["memory_total_mb"] = round(props.total_memory / (1024 * 1024), 2)
    except Exception:
        pass

    return context


def _benchmark_context(parent: SearchNode) -> dict[str, Any]:
    metrics = dict(parent.evaluation_result.metrics) if parent.evaluation_result is not None else {}
    return {
        "score": parent.evaluation_result.score if parent.evaluation_result is not None else None,
        "accepted": parent.evaluation_result.accepted if parent.evaluation_result is not None else None,
        "summary": parent.evaluation_result.summary if parent.evaluation_result is not None else "",
        "metrics": metrics,
    }


def _build_bottleneck_card(
    *,
    kernel_file: Path,
    kernel_type: str,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    benchmark_context: dict[str, Any],
    history_context: dict[str, Any],
    region_graph: dict[str, Any],
) -> dict[str, Any]:
    scope = _resolve_scope(static_prior, region_graph)
    target = str(static_prior.get("pipeline_region_label") or kernel_file.name)
    diagnoses = _diagnoses_from_context(
        kernel_type=kernel_type,
        static_prior=static_prior,
        benchmark_context=benchmark_context,
        scope=scope,
    )
    return {
        "scope": scope,
        "target": target,
        "symptoms": _symptoms_from_context(
            static_prior=static_prior,
            hardware_context=hardware_context,
            benchmark_context=benchmark_context,
            history_context=history_context,
        ),
        "diagnoses": diagnoses,
        "response_families": [],
        "avoid_families": _avoid_families_for_diagnoses(diagnoses),
        "rationale": _bottleneck_rationale(diagnoses, scope=scope),
    }


def _build_region_graph(
    *,
    kernel_file: Path,
    kernel_type: str,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    benchmark_context: dict[str, Any],
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
            benchmark_context=benchmark_context,
            scope=scope,
        ),
        "signals": _region_signals(
            static_prior=static_prior,
            hardware_context=hardware_context,
            benchmark_context=benchmark_context,
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
    benchmark_context: dict[str, Any],
    scope: str,
) -> str:
    metrics = _dict_metrics(benchmark_context.get("metrics"))
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
    benchmark_context: dict[str, Any],
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
    metrics = _dict_metrics(benchmark_context.get("metrics"))
    if metrics.get("bottleneck"):
        signals.append(f"local_bottleneck={metrics['bottleneck']}")
    if hardware_context.get("gpu_name"):
        signals.append(f"gpu={hardware_context['gpu_name']}")
    return signals[:4]


def _diagnoses_from_context(
    *,
    kernel_type: str,
    static_prior: dict[str, Any],
    benchmark_context: dict[str, Any],
    scope: str,
) -> list[str]:
    metrics = _dict_metrics(benchmark_context.get("metrics"))
    diagnoses: list[str] = []
    for hint in _profile_bottleneck_hints(static_prior):
        diagnosis = _diagnosis_from_hint(hint)
        if diagnosis and diagnosis not in diagnoses:
            diagnoses.append(diagnosis)

    if not diagnoses:
        bottleneck = str(metrics.get("bottleneck", "unknown")).lower()
        if bottleneck == "memory":
            diagnoses.append("memory_bandwidth_pressure")
        elif bottleneck == "compute":
            diagnoses.append("compute_saturation")
        elif bottleneck == "launch":
            diagnoses.append("launch_overhead")
        else:
            diagnoses.append("uncertain_local_bottleneck")

    if kernel_type in {"softmax", "layernorm", "rmsnorm", "reduce", "cross_entropy"}:
        if "layout_or_boundary_sensitivity" not in diagnoses:
            diagnoses.append("layout_or_boundary_sensitivity")

    if scope == "pipeline" and "kernel_boundary_overhead" not in diagnoses:
        if _string_values(static_prior.get("upstream_regions")) or _string_values(static_prior.get("downstream_regions")):
            diagnoses.append("kernel_boundary_overhead")

    if metrics.get("score_value") is not None and float(metrics.get("score_value") or 0.0) < 1.0:
        if "baseline_underperformance" not in diagnoses:
            diagnoses.append("baseline_underperformance")

    return diagnoses[:4]


def _symptoms_from_context(
    *,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    benchmark_context: dict[str, Any],
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
    metrics = _dict_metrics(benchmark_context.get("metrics"))
    if metrics.get("bottleneck"):
        symptoms.append(f"local_bottleneck={metrics['bottleneck']}")
    if metrics.get("score_metric") and metrics.get("score_value") is not None:
        symptoms.append(f"{metrics['score_metric']}={metrics['score_value']}")
    if hardware_context.get("gpu_name"):
        symptoms.append(f"gpu={hardware_context['gpu_name']}")
    if hardware_context.get("compute_capability"):
        symptoms.append(f"compute_capability={hardware_context['compute_capability']}")
    if history_context.get("negative_insights"):
        symptoms.append(f"recent_negative_insights={len(history_context['negative_insights'])}")
    return symptoms[:6]


def _avoid_families_for_diagnoses(diagnoses: list[str]) -> list[str]:
    avoid: list[str] = []
    if "memory_bandwidth_pressure" in diagnoses:
        avoid.append("instruction_selection")
    if "compute_saturation" in diagnoses:
        avoid.append("memory_hierarchy")
    if "layout_or_boundary_sensitivity" in diagnoses:
        avoid.append("instruction_selection")
    if "launch_overhead" in diagnoses:
        avoid.append("instruction_selection")
    if "kernel_boundary_overhead" in diagnoses:
        avoid.append("instruction_selection")
    if "communication_pressure" in diagnoses:
        avoid.append("control_flow_specialization")
    return list(dict.fromkeys(avoid))


def _bottleneck_rationale(diagnoses: list[str], *, scope: str) -> str:
    if not diagnoses:
        return "No stable local diagnosis is available yet."
    prefix = {
        "pipeline": "Current pipeline diagnosis",
        "region": "Current region diagnosis",
    }.get(scope, "Current local diagnosis")
    if len(diagnoses) == 1:
        return f"{prefix} points to {diagnoses[0]}."
    return f"{prefix} points to {diagnoses[0]} with secondary pressure from {diagnoses[1]}."


def _search_policy_hint(
    *,
    bottleneck_card: dict[str, Any],
    benchmark_context: dict[str, Any],
    history_context: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    diagnoses = _string_values(bottleneck_card.get("diagnoses"))
    scope = str(bottleneck_card.get("scope", "kernel"))
    accepted = bool(benchmark_context.get("accepted"))
    score = benchmark_context.get("score")
    positive_count = len(_string_values(history_context.get("positive_insights")))
    negative_count = len(_string_values(history_context.get("negative_insights")))

    if "uncertain_local_bottleneck" in diagnoses or negative_count >= positive_count + 2:
        exploration_bias = "explore"
    elif accepted and score is not None and float(score) > 1.0 and positive_count >= negative_count:
        exploration_bias = "exploit"
    else:
        exploration_bias = "balanced"

    suggested_children = {
        "explore": min(limit, 3),
        "balanced": min(limit, 2),
        "exploit": 1,
    }[exploration_bias]
    suggested_beam_width = {
        "explore": min(limit, 2),
        "balanced": 2 if limit > 1 else 1,
        "exploit": 1,
    }[exploration_bias]
    suggested_rounds = {
        "explore": 3,
        "balanced": 2,
        "exploit": 1,
    }[exploration_bias]

    stop_conditions: list[str] = []
    if accepted and score is not None and float(score) > 1.5:
        stop_conditions.append("stop when a child preserves correctness and clearly beats the current baseline")
    if "uncertain_local_bottleneck" in diagnoses:
        stop_conditions.append("stop broad branching once one family produces a consistent accepted improvement")
    if scope == "pipeline":
        stop_conditions.append("do not escalate beyond kernel-local execution until a pipeline task exists")

    return {
        "recommended_scope": scope,
        "exploration_bias": exploration_bias,
        "suggested_children": suggested_children,
        "suggested_rounds": suggested_rounds,
        "suggested_beam_width": suggested_beam_width,
        "stop_conditions": stop_conditions,
    }


def _recommend_branch_families(
    *,
    kernel_type: str,
    scope: str,
    diagnoses: list[str],
    allowed_families: set[str] | None,
    avoid_families: list[str],
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    benchmark_context: dict[str, Any],
    history_context: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    templates = _family_templates(kernel_type)
    scored = []
    for template in templates:
        family = str(template["family"])
        if allowed_families is not None and family not in allowed_families:
            continue
        if family in avoid_families:
            continue
        score = _family_score(
            family,
            kernel_type=kernel_type,
            scope=scope,
            diagnoses=diagnoses,
            benchmark_context=benchmark_context,
            history_context=history_context,
        )
        scored.append((score, template))
    scored.sort(key=lambda item: item[0], reverse=True)

    branch_families: list[dict[str, Any]] = []
    for _, template in scored[:limit]:
        branch_families.append(
            {
                **template,
                "target_signals": template["target_signals"],
                "supporting_evidence": _supporting_evidence(
                    template["family"],
                    static_prior=static_prior,
                    hardware_context=hardware_context,
                    benchmark_context=benchmark_context,
                    history_context=history_context,
                ),
            }
        )
    return branch_families


def _family_templates(kernel_type: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "memory-hierarchy",
            "family": "memory_hierarchy",
            "title": "Memory hierarchy branch",
            "core_hypothesis": (
                "Reshape data movement, staging, vectorization, or locality to better match the hot shapes "
                "without changing external semantics."
            ),
            "constraints_summary": "Stay within memory-hierarchy changes; do not switch to a broad algorithm rewrite.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when bandwidth, locality, or memory traffic looks like the dominant limiter.",
            "target_signals": ["bandwidth_gb_s", "pct_peak_bandwidth", "peak_vram_mb", "bottleneck"],
            "disallowed_directions": ["Do not pivot into tensorization or broad algorithm changes."],
        },
        {
            "id": "parallelization",
            "family": "parallelization",
            "title": "Parallelization branch",
            "core_hypothesis": (
                "Retune tile geometry, work partitioning, or CTA/warp decomposition to better balance the current workload."
            ),
            "constraints_summary": "Keep the same overall algorithm family; change how work is partitioned.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when current launch decomposition leaves parallelism or occupancy on the table.",
            "target_signals": ["throughput_tflops", "speedup_vs_pytorch", "bottleneck"],
            "disallowed_directions": ["Do not combine this branch with a broad memory-layout rewrite."],
        },
        {
            "id": "occupancy-launch",
            "family": "occupancy_launch",
            "title": "Occupancy and launch branch",
            "core_hypothesis": (
                "Rebalance launch geometry, register pressure, and shared-memory usage to increase useful active work."
            ),
            "constraints_summary": "Focus on launch and resource balance instead of changing core math.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when the kernel appears resource-limited or launch-limited.",
            "target_signals": ["speedup_vs_pytorch", "pct_peak_compute", "pct_peak_bandwidth"],
            "disallowed_directions": ["Do not turn this branch into a new algorithm family."],
        },
        {
            "id": "latency-hiding",
            "family": "latency_hiding",
            "title": "Latency-hiding branch",
            "core_hypothesis": (
                "Reduce exposed stalls by overlapping work, trimming synchronization, or improving pipelining."
            ),
            "constraints_summary": "Preserve the same algorithm and numerical behavior while reducing exposed latency.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when memory or synchronization stalls appear to dominate the hot path.",
            "target_signals": ["throughput_tflops", "bandwidth_gb_s", "bottleneck"],
            "disallowed_directions": ["Do not mix this branch with broad control-flow specialization."],
        },
        {
            "id": "instruction-selection",
            "family": "instruction_selection",
            "title": "Instruction-selection branch",
            "core_hypothesis": (
                f"Introduce a more compute-dense {kernel_type}-appropriate instruction path or tensorization strategy "
                "without changing the external interface."
            ),
            "constraints_summary": "Focus on compute path selection rather than memory hierarchy changes.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when the kernel is compute-facing and has room for denser math instructions.",
            "target_signals": ["throughput_tflops", "pct_peak_compute", "bottleneck"],
            "disallowed_directions": ["Do not turn this branch into a broad memory-traffic rewrite."],
        },
        {
            "id": "control-flow-specialization",
            "family": "control_flow_specialization",
            "title": "Control-flow specialization branch",
            "core_hypothesis": (
                "Specialize indexing or hot-shape control flow to remove unnecessary branching and wasted work."
            ),
            "constraints_summary": "Keep the hot-path specialization local; avoid changing benchmark or verifier assumptions.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when the extracted kernel already targets a narrow shape family and can benefit from specialization.",
            "target_signals": ["speedup_vs_pytorch", "correctness", "bottleneck"],
            "disallowed_directions": ["Do not combine with a separate tensorization rewrite in the same branch."],
        },
        {
            "id": "fusion",
            "family": "fusion",
            "title": "Fusion branch",
            "core_hypothesis": (
                "Reduce cross-kernel boundary cost by fusing adjacent work or collapsing redundant stage transitions."
            ),
            "constraints_summary": "Only pursue local fusion or boundary removal; do not rewrite the whole pipeline.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when pipeline boundaries, repeated launches, or redundant materialization dominate.",
            "target_signals": ["launch_overhead", "boundary_cost", "materialization_count"],
            "disallowed_directions": ["Do not turn this branch into a pure single-kernel tiling exercise."],
        },
        {
            "id": "layout-propagation",
            "family": "layout_propagation",
            "title": "Layout propagation branch",
            "core_hypothesis": (
                "Keep a better intermediate layout across adjacent work so later stages avoid transpose or reshaping overhead."
            ),
            "constraints_summary": "Focus on region or boundary layout choices instead of isolated instruction tweaks.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when region boundaries or layout churn make local kernels look slower than they should.",
            "target_signals": ["layout_churn", "boundary_cost", "memory_traffic"],
            "disallowed_directions": ["Do not drift into a broad launch retuning branch."],
        },
        {
            "id": "launch-consolidation",
            "family": "launch_consolidation",
            "title": "Launch consolidation branch",
            "core_hypothesis": (
                "Reduce launch overhead by merging tiny stages, simplifying boundaries, or batching control flow."
            ),
            "constraints_summary": "Focus on launch count and stage granularity rather than local math changes.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when many small kernels or stage transitions dominate runtime.",
            "target_signals": ["launch_overhead", "kernel_count", "stage_granularity"],
            "disallowed_directions": ["Do not spend this branch on isolated instruction selection."],
        },
        {
            "id": "overlap-pipelining",
            "family": "overlap_pipelining",
            "title": "Overlap and pipelining branch",
            "core_hypothesis": (
                "Restructure the hot region to overlap communication, memory movement, or dependent stages more effectively."
            ),
            "constraints_summary": "Preserve semantics while reducing exposed waiting across the region.",
            "editable_path_hints": ["kernel.py"],
            "rationale": "Use this branch when synchronization, communication, or stage waiting dominates the bottleneck.",
            "target_signals": ["communication_wait", "sync_wait", "stall_ratio"],
            "disallowed_directions": ["Do not collapse into a pure memory-layout rewrite unless the branch card says so."],
        },
    ]


def _family_score(
    family: str,
    *,
    kernel_type: str,
    scope: str = "kernel",
    diagnoses: list[str] | None = None,
    benchmark_context: dict[str, Any],
    history_context: dict[str, Any],
) -> float:
    score = 0.0
    diagnoses = diagnoses or []
    metrics = _dict_metrics(benchmark_context.get("metrics"))
    bottleneck = str(metrics.get("bottleneck", "unknown")).lower()

    if bottleneck == "memory":
        score += {
            "memory_hierarchy": 4.0,
            "latency_hiding": 3.0,
            "parallelization": 2.0,
            "control_flow_specialization": 1.5,
            "occupancy_launch": 1.0,
        }.get(family, 0.0)
    elif bottleneck == "compute":
        score += {
            "instruction_selection": 4.0,
            "parallelization": 3.0,
            "occupancy_launch": 2.5,
            "latency_hiding": 1.5,
            "memory_hierarchy": 1.0,
        }.get(family, 0.0)
    else:
        score += {
            "parallelization": 2.5,
            "memory_hierarchy": 2.5,
            "occupancy_launch": 2.0,
            "control_flow_specialization": 1.5,
            "latency_hiding": 1.5,
            "instruction_selection": 1.5,
        }.get(family, 0.0)

    for diagnosis in diagnoses:
        score += {
            "memory_bandwidth_pressure": {
                "memory_hierarchy": 4.0,
                "latency_hiding": 2.5,
                "layout_propagation": 2.0,
            },
            "compute_saturation": {
                "instruction_selection": 4.0,
                "parallelization": 3.0,
                "occupancy_launch": 2.0,
            },
            "launch_overhead": {
                "launch_consolidation": 4.0,
                "fusion": 3.0,
                "occupancy_launch": 1.5,
            },
            "synchronization_pressure": {
                "overlap_pipelining": 4.0,
                "latency_hiding": 3.0,
            },
            "communication_pressure": {
                "overlap_pipelining": 4.0,
                "fusion": 1.5,
            },
            "kernel_boundary_overhead": {
                "fusion": 4.0,
                "layout_propagation": 3.0,
                "launch_consolidation": 2.0,
            },
            "layout_or_boundary_sensitivity": {
                "layout_propagation": 4.0,
                "memory_hierarchy": 2.0,
                "control_flow_specialization": 1.5,
            },
            "baseline_underperformance": {
                "parallelization": 1.0,
                "memory_hierarchy": 1.0,
                "fusion": 1.0,
            },
            "uncertain_local_bottleneck": {
                "parallelization": 1.0,
                "memory_hierarchy": 1.0,
                "occupancy_launch": 1.0,
            },
        }.get(diagnosis, {}).get(family, 0.0)

    if scope == "kernel" and family in {"fusion", "layout_propagation", "launch_consolidation", "overlap_pipelining"}:
        score -= 1.5
    if scope == "region" and family in {"layout_propagation", "launch_consolidation", "overlap_pipelining"}:
        score += 1.0
    if scope == "pipeline" and family in {"fusion", "layout_propagation", "launch_consolidation", "overlap_pipelining"}:
        score += 1.5

    if kernel_type in {"matmul", "flash_attention", "fused_mlp"} and family in {"instruction_selection", "parallelization"}:
        score += 1.0
    if kernel_type in {"softmax", "layernorm", "rmsnorm", "reduce", "cross_entropy", "rotary_embedding"} and family in {"memory_hierarchy", "control_flow_specialization"}:
        score += 1.0

    family_phrase = family.replace("_", " ")
    positive_blob = " ".join(
        str(entry).lower()
        for entry in history_context.get("positive_insights", [])
    )
    negative_blob = " ".join(
        str(entry).lower()
        for entry in history_context.get("negative_insights", [])
    )
    if family_phrase in positive_blob:
        score += 0.5
    if family_phrase in negative_blob:
        score -= 0.5

    return score


def _supporting_evidence(
    family: str,
    *,
    static_prior: dict[str, Any],
    hardware_context: dict[str, Any],
    benchmark_context: dict[str, Any],
    history_context: dict[str, Any],
) -> list[str]:
    evidence: list[str] = []
    if static_prior.get("optimization_scope"):
        evidence.append(f"scope={_normalize_scope(static_prior['optimization_scope'])}")
    if static_prior.get("kernel_type"):
        evidence.append(f"kernel_type={static_prior['kernel_type']}")
    if static_prior.get("pct_total_gpu_time") is not None:
        evidence.append(f"pct_total_gpu_time={static_prior['pct_total_gpu_time']}")
    if static_prior.get("model_shape_display"):
        evidence.append(f"model_shape={static_prior['model_shape_display']}")
    if static_prior.get("pipeline_region_label"):
        evidence.append(f"focus_region={static_prior['pipeline_region_label']}")
    profile_bottlenecks = _profile_bottleneck_hints(static_prior)
    if profile_bottlenecks:
        evidence.append(f"profile_bottlenecks={','.join(profile_bottlenecks[:3])}")
    metrics = _dict_metrics(benchmark_context.get("metrics"))
    if metrics.get("bottleneck"):
        evidence.append(f"bottleneck={metrics['bottleneck']}")
    if metrics.get("score_metric") and metrics.get("score_value") is not None:
        evidence.append(f"{metrics['score_metric']}={metrics['score_value']}")
    if hardware_context.get("gpu_name"):
        evidence.append(f"gpu={hardware_context['gpu_name']}")
    if hardware_context.get("compute_capability"):
        evidence.append(f"compute_capability={hardware_context['compute_capability']}")
    negatives = history_context.get("negative_insights", [])
    if negatives:
        evidence.append(f"recent_negative_insights={len(negatives)}")
    positives = history_context.get("positive_insights", [])
    if positives:
        evidence.append(f"recent_positive_insights={len(positives)}")
    evidence.append(f"family={family}")
    return evidence


def _allowed_branch_families_for_task() -> set[str]:
    return set(_KERNEL_LOCAL_FAMILIES)


def _resolve_scope(static_prior: dict[str, Any], region_graph: dict[str, Any]) -> str:
    explicit_scope = _normalize_scope(static_prior.get("optimization_scope"))
    if explicit_scope != "kernel":
        return explicit_scope
    regions = region_graph.get("regions", [])
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, dict):
                continue
            region_scope = _normalize_scope(region.get("scope"))
            if region_scope == "pipeline":
                return "pipeline"
            if region_scope == "region":
                return "region"
    return "kernel"


def _profile_bottleneck_hints(static_prior: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    for key in ("profile_bottlenecks", "bottleneck_hints", "pipeline_bottlenecks"):
        hints.extend(_string_values(static_prior.get(key)))
    if static_prior.get("dominant_bottleneck"):
        hints.append(str(static_prior["dominant_bottleneck"]))
    normalized: list[str] = []
    for hint in hints:
        value = _normalize_hint(hint)
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
