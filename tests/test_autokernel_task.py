from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
import json
from pathlib import Path
from unittest import mock

from kernel_tools.task import AutoKernelTask, parse_bench_output, parse_verify_output
from autopipeline.search.types import EvaluationResult, Hypothesis, RootMemory, SearchNode


class AutoKernelTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo_root = Path(self.tempdir.name) / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)
        (self.repo_root / "pyproject.toml").write_text(
            "[project]\nname = 'test-repo'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        for name in ("bench.py", "verify.py", "reference.py"):
            (self.repo_root / name).write_text(f"# {name}\n", encoding="utf-8")

        self.kernel_file = self.repo_root / "kernel_matmul_7.py"
        self.kernel_file.write_text(
            textwrap.dedent(
                '''\
                """
                AutoKernel -- Extracted kernel from model profiling.
                Op type: matmul
                Rank: 7 (18.5% of GPU time)
                Model shape: M=4096, N=4096, K=4096
                """

                KERNEL_TYPE = "matmul"
                BACKEND = "triton"
                MODEL_SHAPES = {"M": 4096, "N": 4096, "K": 4096}

                def kernel():
                    return 1
                '''
            ),
            encoding="utf-8",
        )
        self.task = AutoKernelTask(
            repo_root=self.repo_root,
            kernel_file=self.kernel_file,
            bench_args=["--foo", "bar"],
            verify_command="python verify.py",
        )

    def test_create_sandbox_materializes_local_kernel_copy(self) -> None:
        node = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
        )

        self.task.create_sandbox(node, None)

        self.assertEqual(Path(node.candidate_path).name, "kernel.py")
        self.assertEqual(
            Path(node.candidate_path).read_text(encoding="utf-8"),
            self.kernel_file.read_text(encoding="utf-8"),
        )

    def test_parse_bench_output_and_evaluate_use_speedup_score(self) -> None:
        run_dir = self.repo_root / "run"
        self.task.prepare_run(run_dir)
        node = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(run_dir / "nodes" / "node-0001"),
        )
        self.task.create_sandbox(node, None)
        sample_stdout = textwrap.dedent(
            """
            correctness: PASS
            throughput_tflops: 241.5
            bandwidth_gb_s: 1300.0
            speedup_vs_pytorch: 2.75x
            pct_peak_compute: 61.2%
            pct_peak_bandwidth: 42.4%
            bottleneck: memory
            peak_vram_mb: 512.0
            """
        )

        with mock.patch("kernel_tools.task.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["uv"],
                returncode=0,
                stdout=sample_stdout,
                stderr="",
            )
            result = self.task.evaluate(node)

        self.assertTrue(result.accepted)
        self.assertEqual(result.score, 2.75)
        self.assertEqual(result.metrics["correctness"], "PASS")
        self.assertEqual(result.metrics["bottleneck"], "memory")
        self.assertEqual(result.metrics["score_metric"], "speedup_vs_pytorch")

    def test_promote_writes_optimized_sibling_file(self) -> None:
        node = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
            candidate_path=str(self.kernel_file),
        )

        promoted = self.task.promote(node, RootMemory(objective="test"))

        self.assertEqual(promoted, str((self.repo_root / "kernel_matmul_7_optimized.py").resolve()))
        self.assertTrue(Path(promoted).exists())

    def test_final_verify_uses_run_local_workspace_and_parses_output(self) -> None:
        run_dir = self.repo_root / "run"
        self.task.prepare_run(run_dir)
        promoted_path = self.repo_root / "kernel_matmul_7_optimized.py"
        promoted_path.write_text(self.kernel_file.read_text(encoding="utf-8"), encoding="utf-8")
        self.task._last_promoted_path = promoted_path

        with mock.patch("kernel_tools.task.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["python", "verify.py"],
                returncode=0,
                stdout="correctness: PASS\nend_to_end_speedup: 1.25x\n",
                stderr="",
            )
            result = self.task.final_verify(RootMemory(objective="test"))

        self.assertIsNotNone(result)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["end_to_end_speedup"], 1.25)
        command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
        self.assertIn("--workspace", command)
        self.assertIn(str(run_dir / "workspace"), command)
        copied = run_dir / "workspace" / "kernel_matmul_7_optimized.py"
        self.assertTrue(copied.exists())

    def test_parse_helpers_extract_expected_metrics(self) -> None:
        bench = parse_bench_output(
            "correctness: FAIL\nspeedup_vs_pytorch: 0.50x\nbottleneck: compute\npeak_vram_mb: 1024\n"
        )
        verify = parse_verify_output("correctness: PASS\nend_to_end_speedup: 1.10x\n")

        self.assertEqual(bench["correctness"], "FAIL")
        self.assertEqual(bench["speedup_vs_pytorch"], 0.5)
        self.assertEqual(bench["peak_vram_mb"], 1024.0)
        self.assertEqual(verify["correctness"], "PASS")
        self.assertEqual(verify["end_to_end_speedup"], 1.1)

    def test_planner_context_exposes_facts_rules_and_frozen_decisions(self) -> None:
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.2,
                metrics={"bottleneck": "memory", "score_metric": "speedup_vs_pytorch", "score_value": 1.2},
                summary="accepted local gain",
            ),
        )
        with mock.patch.object(
            self.task,
            "_hardware_context",
            return_value={"gpu_name": "H100", "compute_capability": "9.0"},
        ):
            context = self.task.planner_context(
                parent,
                RootMemory(
                    objective="test",
                    positive_insights=["memory hierarchy branch improved score"],
                    negative_insights=["instruction selection branch regressed"],
                ),
                3,
                upper_bounds={"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
            )

        self.assertEqual(context["static_prior"]["kernel_type"], "matmul")
        self.assertEqual(context["static_prior"]["rank"], 7)
        self.assertEqual(context["hardware_context"]["gpu_name"], "H100")
        self.assertEqual(context["region_graph"]["focus_region_id"], "current-kernel")
        self.assertEqual(context["region_graph"]["regions"][0]["label"], "kernel_matmul_7.py")
        self.assertIn("scope=kernel", context["symptoms"])
        self.assertIn("local_bottleneck=memory", context["symptoms"])
        self.assertIn("speedup_vs_pytorch=1.2", context["symptoms"])
        self.assertIn("gpu=H100", context["symptoms"])
        self.assertTrue(context["evaluator_prior"]["accepted"])
        self.assertEqual(context["profile_hints"], [])
        self.assertEqual(context["planner_rules"]["allowed_scopes"], ["kernel", "region", "pipeline"])
        self.assertIn("memory_hierarchy", context["planner_rules"]["allowed_families"])
        self.assertEqual(context["planner_rules"]["resource_ceiling"]["beam_width"], 3)
        self.assertIn("sandbox surface", " ".join(context["frozen_decisions"]).lower())

    def test_planner_context_expands_region_graph_when_profile_prior_has_scope(self) -> None:
        self.task.profile_prior.update(
            {
                "optimization_scope": "pipeline",
                "pipeline_region_label": "attention block",
                "upstream_regions": ["qkv projection"],
                "downstream_regions": ["attention output"],
                "profile_bottlenecks": ["launch", "boundary"],
            }
        )
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
        )
        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100"}):
            context = self.task.planner_context(parent, RootMemory(objective="test"), 4)

        self.assertEqual(context["region_graph"]["regions"][0]["label"], "attention block")
        self.assertEqual(len(context["region_graph"]["regions"]), 3)
        self.assertIn("upstream-1", context["region_graph"]["regions"][0]["adjacent"])
        self.assertIn("launch_overhead", context["profile_hints"])
        self.assertIn("kernel_boundary_overhead", context["profile_hints"])
        self.assertEqual(context["planner_rules"]["allowed_scopes"], ["kernel", "region", "pipeline"])
        self.assertNotIn("fusion", context["planner_rules"]["allowed_families"])
        self.assertNotIn("launch_consolidation", context["planner_rules"]["allowed_families"])
        self.assertIn("parallelization", context["planner_rules"]["allowed_families"])

    def test_profile_bottleneck_hints_are_canonicalized(self) -> None:
        self.task.profile_prior.update(
            {
                "optimization_scope": "pipeline",
                "pipeline_region_label": "attention block",
                "profile_bottlenecks": ["launch_overhead", "kernel_boundary_overhead"],
            }
        )
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
        )

        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100"}):
            context = self.task.planner_context(parent, RootMemory(objective="test"), 3)

        self.assertIn("launch_overhead", context["profile_hints"])
        self.assertIn("kernel_boundary_overhead", context["profile_hints"])

    def test_planner_context_preserves_resource_ceiling(self) -> None:
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
        )

        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100"}):
            context = self.task.planner_context(
                parent,
                RootMemory(objective="test"),
                1,
                upper_bounds={"beam_width": 3, "children_per_expansion": 1, "max_rounds": 3},
            )

        self.assertEqual(context["planner_rules"]["resource_ceiling"]["beam_width"], 3)
        self.assertEqual(context["planner_rules"]["resource_ceiling"]["children_per_expansion"], 1)
        self.assertEqual(context["planner_rules"]["resource_ceiling"]["max_rounds"], 3)

    def test_prepare_execution_surface_expands_scope_context_for_pipeline_scope(self) -> None:
        node = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="trial", title="Trial", core_hypothesis="trial"),
            node_dir=str(self.repo_root / "node-0001"),
            planner_context={"recommended_scope": "pipeline"},
        )
        self.task.profile_prior.update(
            {
                "optimization_scope": "pipeline",
                "pipeline_region_label": "attention block",
                "upstream_regions": ["qkv projection"],
                "downstream_regions": ["attention output"],
                "profile_bottlenecks": ["launch"],
            }
        )

        self.task.create_sandbox(node, None)
        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100"}):
            self.task.prepare_execution_surface(node, None)

        self.assertEqual(node.planner_context["effective_scope"], "pipeline")
        self.assertEqual(node.planner_context["effective_editable_paths"], ["kernel.py"])
        self.assertEqual(node.planner_context["focus_target"], "attention block")
        self.assertEqual(len(node.planner_context["scope_context_files"]), 2)
        context_path = Path(node.sandbox_path) / "pipeline_context.json"
        self.assertTrue(context_path.exists())
        self.assertIn("attention block", context_path.read_text(encoding="utf-8"))

    def test_handle_probe_compile_smoke_returns_success_for_kernel(self) -> None:
        result = self.task.handle_probe(
            {"kind": "compile_smoke", "path": "kernel_matmul_7.py"},
            cwd=self.repo_root,
            node=None,
            parent=None,
        )

        self.assertEqual(result["status"], "completed")
        self.assertIn("compile_smoke passed", result["summary"])

    def test_handle_probe_hardware_profile_collects_versions_and_inventory(self) -> None:
        def fake_run(*args, **kwargs):
            command = kwargs.get("args") if "args" in kwargs else args[0]
            if command[:2] == ["nvidia-smi", "-L"]:
                return subprocess.CompletedProcess(command, 0, "GPU 0: H100\n", "")
            if command[:3] == ["nvidia-smi", "topo", "-m"]:
                return subprocess.CompletedProcess(command, 0, "GPU0\tCPU Affinity\n", "")
            return subprocess.CompletedProcess(command, 0, '{"torch_version": "2.5.0", "triton_version": "3.3.0"}\n', "")

        with (
            mock.patch("kernel_tools.probes.detect_hardware_context", return_value={"gpu_name": "H100", "gpu_count": 1}),
            mock.patch("kernel_tools.probes.subprocess.run", side_effect=fake_run),
        ):
            result = self.task.handle_probe(
                {"kind": "hardware_profile"},
                cwd=self.repo_root,
                node=None,
                parent=None,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["hardware"]["gpu_name"], "H100")
        self.assertIn("H100", result["data"]["gpu_inventory"])
        self.assertEqual(result["data"]["runtime_versions"]["torch_version"], "2.5.0")

    def test_build_theoretical_limit_inputs_returns_stable_facts(self) -> None:
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
            candidate_path=str(self.kernel_file),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=2.75,
                metrics={
                    "throughput_tflops": 241.5,
                    "bandwidth_gb_s": 1300.0,
                    "pct_peak_compute": 61.2,
                    "pct_peak_bandwidth": 42.4,
                },
                summary="baseline metrics",
            ),
        )

        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100", "gpu_count": 1}):
            result = self.task.build_theoretical_limit_inputs(
                cwd=self.repo_root,
                node=None,
                parent=parent,
            )

        self.assertEqual(result["hardware"]["gpu_name"], "H100")
        self.assertEqual(result["workload"]["kernel_type"], "matmul")
        self.assertEqual(result["workload"]["model_shapes"]["M"], 4096)
        self.assertEqual(result["observed"]["metrics"]["throughput_tflops"], 241.5)

    def test_handle_probe_software_audit_finds_common_training_patterns(self) -> None:
        training_file = self.repo_root / "train.py"
        training_file.write_text(
            textwrap.dedent(
                """\
                import torch
                from torch.utils.data import DataLoader
                model = torch.compile(model)
                loader = DataLoader(ds, num_workers=8, pin_memory=True, persistent_workers=True)
                loss_value = loss.item()
                torch.cuda.synchronize()
                """
            ),
            encoding="utf-8",
        )

        result = self.task.handle_probe(
            {"kind": "software_audit", "paths": ["train.py"]},
            cwd=self.repo_root,
            node=None,
            parent=None,
        )

        self.assertEqual(result["status"], "completed")
        checks = {item["check"] for item in result["data"]["matches"]}
        self.assertIn("torch_compile", checks)
        self.assertIn("dataloader", checks)
        self.assertIn("host_sync", checks)

    def test_handle_probe_python_experiment_runs_diagnostic_script(self) -> None:
        result = self.task.handle_probe(
            {
                "kind": "python_experiment",
                "code": "print('probe-ok')",
            },
            cwd=self.repo_root,
            node=None,
            parent=None,
        )

        self.assertEqual(result["status"], "completed")
        self.assertIn("probe-ok", result["data"]["stdout"])

    def test_handle_probe_python_experiment_prefers_project_environment(self) -> None:
        with mock.patch("kernel_tools.probes.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["uv"],
                returncode=0,
                stdout="probe-ok\n",
                stderr="",
            )
            result = self.task.handle_probe(
                {
                    "kind": "python_experiment",
                    "code": "print('probe-ok')",
                },
                cwd=self.repo_root,
                node=None,
                parent=None,
            )

        command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
        self.assertEqual(command[:3], ["uv", "run", "--project"])
        self.assertEqual(Path(command[3]).resolve(), self.repo_root.resolve())
        self.assertEqual(command[4], "--directory")
        self.assertEqual(Path(command[5]).resolve(), self.repo_root.resolve())
        self.assertIn("--no-sync", command)
        self.assertEqual(result["status"], "completed")

    def test_handle_probe_python_experiment_falls_back_to_python3_when_uv_missing(self) -> None:
        with mock.patch("kernel_tools.probes.subprocess.run") as run_mock:
            run_mock.side_effect = [
                FileNotFoundError("uv not found"),
                subprocess.CompletedProcess(
                    args=["python3"],
                    returncode=0,
                    stdout="probe-ok\n",
                    stderr="",
                ),
            ]
            result = self.task.handle_probe(
                {
                    "kind": "python_experiment",
                    "code": "print('probe-ok')",
                },
                cwd=self.repo_root,
                node=None,
                parent=None,
            )

        fallback_call = run_mock.call_args_list[1]
        fallback_command = fallback_call.kwargs.get("args", fallback_call.args[0])
        self.assertEqual(fallback_command[0], "python3")
        self.assertTrue(str(fallback_command[1]).endswith("probe_experiment.py"))
        self.assertEqual(result["status"], "completed")

    def test_handle_probe_compile_report_parses_compiler_facts(self) -> None:
        stderr = (
            "ptxas info    : Used 48 registers, 4096 bytes smem, 128 bytes cmem[0]\n"
            "ptxas info    : 0 bytes stack frame, 16 bytes spill stores, 8 bytes spill loads\n"
        )
        with mock.patch("kernel_tools.probes.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["python3"],
                returncode=0,
                stdout="",
                stderr=stderr,
            )
            result = self.task.handle_probe(
                {"kind": "compile_report", "path": "kernel_matmul_7.py"},
                cwd=self.repo_root,
                node=None,
                parent=None,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["parsed"]["registers"], 48)
        self.assertEqual(result["data"]["parsed"]["shared_mem_bytes"], 4096)
        self.assertEqual(result["data"]["parsed"]["spill_store_bytes"], 16)

    def test_handle_probe_ncu_profile_uses_default_bench_command(self) -> None:
        with mock.patch("kernel_tools.probes.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["ncu"],
                returncode=0,
                stdout="==PROF== done\n",
                stderr="",
            )
            result = self.task.handle_probe(
                {"kind": "ncu_profile", "metrics": ["sm__throughput.avg.pct_of_peak_sustained_elapsed"]},
                cwd=self.repo_root,
                node=None,
                parent=None,
            )

        command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["ncu", "--csv", "--page", "raw"])
        bench_index = command.index("--no-sync") + 1
        self.assertEqual(Path(command[bench_index]).resolve(), (self.repo_root / "bench.py").resolve())
        self.assertEqual(result["status"], "completed")

    def test_handle_probe_nsys_profile_uses_default_bench_command(self) -> None:
        with mock.patch("kernel_tools.probes.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["nsys"],
                returncode=0,
                stdout="Generated:\n",
                stderr="",
            )
            result = self.task.handle_probe(
                {"kind": "nsys_profile"},
                cwd=self.repo_root,
                node=None,
                parent=None,
            )

        command = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
        self.assertEqual(command[:3], ["nsys", "profile", "--force-overwrite=true"])
        bench_index = command.index("--no-sync") + 1
        self.assertEqual(Path(command[bench_index]).resolve(), (self.repo_root / "bench.py").resolve())
        self.assertEqual(result["status"], "completed")

    def test_handle_probe_stage_summary_reads_trace_artifact(self) -> None:
        trace_dir = self.repo_root / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / "kernel_trace.json"
        trace_path.write_text(
            json.dumps(
                {
                    "traceEvents": [
                        {"name": "cudaLaunchKernel", "ph": "X", "dur": 15},
                        {"name": "memcpyHtoD", "ph": "X", "dur": 30},
                        {"name": "cudaStreamSynchronize", "ph": "X", "dur": 5},
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = self.task.handle_probe(
            {"kind": "stage_summary", "trace_path": str(trace_path)},
            cwd=self.repo_root,
            node=None,
            parent=None,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["complete_event_count"], 3)
        self.assertEqual(result["data"]["memcpy_count"], 1)
        self.assertEqual(result["data"]["sync_or_wait_count"], 1)

    def test_handle_probe_runtime_breakdown_returns_category_totals(self) -> None:
        trace_dir = self.repo_root / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / "kernel_trace.json"
        trace_path.write_text(
            json.dumps(
                {
                    "traceEvents": [
                        {"name": "dataloader_wait", "ph": "X", "dur": 10},
                        {"name": "cudaLaunchKernel", "ph": "X", "dur": 40},
                        {"name": "ncclAllReduce", "ph": "X", "dur": 25},
                        {"name": "cudaStreamSynchronize", "ph": "X", "dur": 5},
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = self.task.handle_probe(
            {"kind": "runtime_breakdown", "trace_path": str(trace_path)},
            cwd=self.repo_root,
            node=None,
            parent=None,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["total_timed_us"], 80.0)
        self.assertEqual(result["data"]["top_events"][0]["name"], "cudaLaunchKernel")


if __name__ == "__main__":
    unittest.main()
