from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tree.autokernel import AutoKernelTask, _family_score, parse_bench_output, parse_verify_output
from tree.types import Hypothesis, RootMemory, SearchNode


class AutoKernelTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo_root = Path(self.tempdir.name) / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)
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

        with mock.patch("tree.autokernel.subprocess.run") as run_mock:
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

        with mock.patch("tree.autokernel.subprocess.run") as run_mock:
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

    def test_watchman_context_exposes_profile_hardware_and_branch_families(self) -> None:
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
        )
        parent.evaluation_result = type("Eval", (), {
            "accepted": True,
            "score": 1.4,
            "summary": "baseline",
            "metrics": {
                "bottleneck": "memory",
                "speedup_vs_pytorch": 1.4,
                "score_metric": "speedup_vs_pytorch",
                "score_value": 1.4,
            },
        })()

        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100", "compute_capability": "9.0"}):
            context = self.task.watchman_context(
                parent,
                RootMemory(
                    objective="test",
                    positive_insights=["memory hierarchy branch improved score"],
                    negative_insights=["instruction selection branch regressed"],
                ),
                3,
            )

        self.assertEqual(context["static_prior"]["kernel_type"], "matmul")
        self.assertEqual(context["static_prior"]["rank"], 7)
        self.assertEqual(context["hardware_context"]["gpu_name"], "H100")
        self.assertEqual(context["benchmark_context"]["metrics"]["bottleneck"], "memory")
        self.assertEqual(context["region_graph"]["focus_region_id"], "current-kernel")
        self.assertEqual(context["region_graph"]["regions"][0]["label"], "kernel_matmul_7.py")
        self.assertIn("scope=kernel", context["region_graph"]["regions"][0]["signals"])
        self.assertIn("local_bottleneck=memory", context["region_graph"]["regions"][0]["signals"])
        self.assertEqual(context["bottleneck_card"]["scope"], "kernel")
        self.assertEqual(context["bottleneck_card"]["target"], "kernel_matmul_7.py")
        self.assertIn("memory_bandwidth_pressure", context["bottleneck_card"]["diagnoses"])
        self.assertIn("memory_hierarchy", context["bottleneck_card"]["response_families"])
        self.assertEqual(len(context["branch_families"]), 3)
        self.assertEqual(len({entry["family"] for entry in context["branch_families"]}), 3)

    def test_watchman_context_expands_to_pipeline_regions_when_profile_prior_has_scope(self) -> None:
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
        parent.evaluation_result = type("Eval", (), {
            "accepted": True,
            "score": 1.0,
            "summary": "baseline",
            "metrics": {
                "bottleneck": "memory",
                "speedup_vs_pytorch": 1.0,
                "score_metric": "speedup_vs_pytorch",
                "score_value": 1.0,
            },
        })()

        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100"}):
            context = self.task.watchman_context(parent, RootMemory(objective="test"), 4)

        self.assertEqual(context["region_graph"]["regions"][0]["label"], "attention block")
        self.assertEqual(len(context["region_graph"]["regions"]), 3)
        self.assertIn("upstream-1", context["region_graph"]["regions"][0]["adjacent"])
        self.assertEqual(context["bottleneck_card"]["scope"], "pipeline")
        self.assertEqual(context["bottleneck_card"]["target"], "attention block")
        self.assertIn("launch_overhead", context["bottleneck_card"]["diagnoses"])
        self.assertIn("kernel_boundary_overhead", context["bottleneck_card"]["diagnoses"])
        families = [entry["family"] for entry in context["branch_families"]]
        self.assertNotIn("fusion", families)
        self.assertNotIn("launch_consolidation", families)
        self.assertNotIn("instruction_selection", families)
        self.assertIn("occupancy_launch", families)
        self.assertIn("parallelization", families)

    def test_profile_bottleneck_accepts_canonical_diagnosis_names(self) -> None:
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
        parent.evaluation_result = type("Eval", (), {
            "accepted": True,
            "score": 1.0,
            "summary": "baseline",
            "metrics": {
                "bottleneck": "unknown",
                "speedup_vs_pytorch": 1.0,
                "score_metric": "speedup_vs_pytorch",
                "score_value": 1.0,
            },
        })()

        with mock.patch.object(self.task, "_hardware_context", return_value={"gpu_name": "H100"}):
            context = self.task.watchman_context(parent, RootMemory(objective="test"), 3)

        self.assertIn("launch_overhead", context["bottleneck_card"]["diagnoses"])
        self.assertIn("kernel_boundary_overhead", context["bottleneck_card"]["diagnoses"])

    def test_family_score_penalizes_matching_negative_insights(self) -> None:
        benchmark_context = {"metrics": {"bottleneck": "compute"}}
        baseline = _family_score(
            "instruction_selection",
            kernel_type="matmul",
            benchmark_context=benchmark_context,
            history_context={},
        )
        improved = _family_score(
            "instruction_selection",
            kernel_type="matmul",
            benchmark_context=benchmark_context,
            history_context={"positive_insights": ["instruction selection branch improved"]},
        )
        regressed = _family_score(
            "instruction_selection",
            kernel_type="matmul",
            benchmark_context=benchmark_context,
            history_context={"negative_insights": ["instruction selection branch regressed"]},
        )

        self.assertGreater(improved, baseline)
        self.assertLess(regressed, baseline)


if __name__ == "__main__":
    unittest.main()
