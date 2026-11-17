from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import autopipeline

from autopipeline.pipeline.task import (
    PipelineTask,
    setup_pipeline_workspace,
)
from autopipeline.pipeline.smoke import create_pipeline_smoke_fixture, run_pipeline_smoke
from autopipeline.search.planner import Planner
from autopipeline.project import initialize
from autopipeline.search.sensing import SensingRuntime, build_sensing_summary
from autopipeline.search.types import Hypothesis, RootMemory, SearchNode


class PipelineBridgeTests(unittest.TestCase):
    def test_evaluator_subprocess_can_import_the_source_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = initialize(root / "workload", source=None, device="cpu", dtype="float32")
            task = PipelineTask(repo_root=root, spec_path=spec)
            env = task._command_env(sandbox=task.spec.workspace_source, node_dir=root)
            self.assertEqual(
                Path(env["PYTHONPATH"].split(os.pathsep)[0]),
                Path(autopipeline.__file__).resolve().parent.parent,
            )

    def test_native_task_uses_default_probe_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = initialize(root / "workload", source=None, device="cpu", dtype="float32")
            task = PipelineTask(repo_root=root, spec_path=spec)
            snapshot = task.snapshot()
            runtime = SensingRuntime(
                base_dir=root / "run", cwd=task.spec.workspace_source, task_snapshot=snapshot,
                root=RootMemory(objective=snapshot.objective), task=task,
            )
            logs = root / "logs"
            runtime.prepare("execute", logs)
            result = runtime.run_probe_request("execute", logs, {"kind": "python", "code": "print('probe works')"})
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["data"]["stdout"].strip(), "probe works")
            facts = runtime.run_probe_request("execute", logs, {"kind": "theoretical_limit_inputs"})
            self.assertEqual(facts["status"], "completed")

    def test_final_verification_requires_fresh_valid_evidence(self):
        cases = [
            (["{python}", "-c", "pass"], False, "correctness_evidence_missing"),
            (["{python}", "-c", "from pathlib import Path; Path('metrics.json').write_text('[]')"], False, "correctness_evidence_missing"),
            (["{python}", "-c", "from pathlib import Path; Path('metrics.json').write_text('[')"], False, "correctness_evidence_missing"),
            (["{python}", "-c", "import json; from pathlib import Path; Path('metrics.json').write_text(json.dumps(dict(correctness='PASS', output_value=2, throughput=100, max_abs_error=0)))"], False, "numerical_error_exceeded"),
            (["{python}", "-c", "import sys; sys.exit(1)"], False, "candidate_failed"),
            (["{python}", "evaluate_pipeline.py"], True, None),
        ]
        for command, accepted, reason in cases:
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    spec_path = create_pipeline_smoke_fixture(root / "fixture")
                    task = PipelineTask(repo_root=root, spec_path=spec_path)
                    task.spec.final_verify_command = command
                    task.prepare_run(root / "run")
                    node = SearchNode(
                        id="node-0001", parent_id=None, depth=0,
                        hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
                        node_dir=str(root / "run" / "nodes" / "node-0001"),
                    )
                    task.create_sandbox(node, None)
                    node.evaluation_result = task.evaluate(node)
                    self.assertTrue(node.evaluation_result.accepted)
                    memory = RootMemory(objective=task.snapshot().objective)
                    promoted = task.promote(node, memory)
                    self.assertTrue((Path(promoted) / "metrics.json").exists())
                    result = task.final_verify(memory)
                    self.assertEqual(result["accepted"], accepted)
                    self.assertEqual(result["failure_reason"], reason)
                    self.assertTrue(Path(result["verification"]["report_path"]).exists())

    def test_pipeline_task_evaluates_smoke_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = create_pipeline_smoke_fixture(root / "fixture")
            task = PipelineTask(repo_root=root, spec_path=spec_path)
            run_dir = root / "run"
            task.prepare_run(run_dir)
            node = SearchNode(
                id="node-0001",
                parent_id=None,
                depth=0,
                hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
                node_dir=str(run_dir / "nodes" / "node-0001"),
            )
            Path(node.node_dir).mkdir(parents=True)
            task.create_sandbox(node, None)

            result = task.evaluate(node)

            self.assertTrue(result.accepted)
            self.assertIn("end_to_end_ms", result.metrics)
            self.assertIn("stage_summary", result.metrics)
            self.assertGreater(result.score, 0)
            self.assertEqual(result.metrics["verification_verdict"], "keep")
            self.assertTrue(Path(result.metrics["verification_report_path"]).exists())
            report = json.loads(Path(result.metrics["verification_report_path"]).read_text(encoding="utf-8"))
            self.assertIn("reference", report)
            self.assertIn("candidate", report)
            self.assertIn("stdout", report["candidate"])
            self.assertIn("stderr", report["candidate"])

    def test_pipeline_backend_rejects_wrong_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = create_pipeline_smoke_fixture(root / "fixture")
            task = PipelineTask(repo_root=root, spec_path=spec_path)
            run_dir = root / "run"
            task.prepare_run(run_dir)
            node = SearchNode(
                id="node-0002",
                parent_id="node-0001",
                depth=1,
                hypothesis=Hypothesis(
                    id="wrong",
                    title="Wrong",
                    core_hypothesis="make candidate faster but numerically wrong",
                    family="pipeline_latency",
                ),
                node_dir=str(run_dir / "nodes" / "node-0002"),
            )
            Path(node.node_dir).mkdir(parents=True)
            task.create_sandbox(node, None)
            config_path = Path(node.candidate_path)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            for stage in config["stages"]:
                stage["latency_ms"] = round(float(stage["latency_ms"]) * 0.5, 3)
            config["output_value"] = 2.0
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

            result = task.evaluate(node)

            self.assertFalse(result.accepted)
            self.assertEqual(result.metrics["verification_verdict"], "reject")
            self.assertEqual(result.metrics["verification_failure_reason"], "numerical_error_exceeded")
            self.assertEqual(result.metrics["verification"]["numerical_error"]["max_abs_error"], 1.0)
            report_path = run_dir / "nodes" / node.id / "verification" / "verification_report.json"
            self.assertTrue(report_path.is_file())
            trial = json.loads((run_dir / "trials" / node.id / "trial.json").read_text())
            self.assertEqual(Path(trial["verification_report"]), report_path)
            self.assertFalse((run_dir / "trials" / node.id / "verification_report.json").exists())

    def test_pipeline_smoke_runs_agent_backed_tree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_pipeline_smoke(repo_root=root, run_dir=root / "smoke")
            state = json.loads(
                (Path(result.run_dir) / "search_state.json").read_text(encoding="utf-8")
            )
            baseline = state["nodes"]["node-0001"]["evaluation_result"]
            candidate = state["nodes"]["node-0002"]["evaluation_result"]
            child_families = [
                node["hypothesis"]["family"]
                for node_id, node in sorted(state["nodes"].items())
                if node_id != "node-0001"
            ]
            prior = json.loads(
                (Path(result.run_dir) / "archive" / "planner_prior.json").read_text(encoding="utf-8")
            )
            node2_report = json.loads(
                (Path(result.run_dir) / "nodes" / "node-0002" / "verification" / "verification_report.json").read_text(
                    encoding="utf-8"
                )
            )
            node2_trial = json.loads(
                (Path(result.run_dir) / "trials" / "node-0002" / "trial.json").read_text(encoding="utf-8")
            )
            node2_region_context = json.loads(
                (Path(result.run_dir) / "nodes" / "node-0002" / "sandbox" / "bottleneck_region.json").read_text(
                    encoding="utf-8"
                )
            )
            archive_records = [
                json.loads(line)
                for line in (Path(result.run_dir) / "archive" / "motifs.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            baseline_config = json.loads(
                (Path(result.run_dir) / "nodes" / "node-0001" / "sandbox" / "pipeline_config.json").read_text(
                    encoding="utf-8"
                )
            )
            first_trial_config = json.loads(
                (Path(result.run_dir) / "nodes" / "node-0002" / "sandbox" / "pipeline_config.json").read_text(
                    encoding="utf-8"
                )
            )
            second_trial_config = json.loads(
                (Path(result.run_dir) / "nodes" / "node-0003" / "sandbox" / "pipeline_config.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(state["final_result"]["total_nodes"], 4)
            self.assertEqual(child_families, ["pipeline_latency", "stage_balance", "overlap"])
            self.assertTrue(Path(result.promoted_path or "").exists())
            self.assertGreater(candidate["score"], baseline["score"])
            self.assertLess(
                candidate["metrics"]["end_to_end_ms"],
                baseline["metrics"]["end_to_end_ms"],
            )
            self.assertTrue(prior["top_positive_motifs"])
            self.assertTrue(prior["top_positive_motifs"][0]["condition"])
            self.assertTrue(prior["backend_verdicts"])
            self.assertEqual(node2_report["verdict"], "keep")
            self.assertGreater(node2_report["performance"]["speedup"], 1.0)
            self.assertNotEqual(first_trial_config, baseline_config)
            self.assertNotEqual(second_trial_config, baseline_config)
            self.assertNotEqual(first_trial_config, second_trial_config)
            baseline_trial = json.loads((Path(result.run_dir) / "baseline" / "trial.json").read_text())
            baseline_report = Path(result.run_dir) / "nodes/node-0001/verification/verification_report.json"
            self.assertEqual(Path(baseline_trial["verification_report"]), baseline_report)
            self.assertTrue(baseline_report.is_file())
            self.assertTrue((Path(result.run_dir) / "regions" / "node-0001.region.json").exists())
            self.assertEqual(
                Path(node2_trial["verification_report"]),
                Path(result.run_dir) / "nodes/node-0002/verification/verification_report.json",
            )
            self.assertTrue((Path(result.run_dir) / "profile" / "node-0002.profile_probe.json").exists())
            self.assertEqual(archive_records[0]["verification"]["verdict"], "keep")
            self.assertTrue(archive_records[0]["verification"]["report_path"])
            self.assertEqual(
                state["nodes"]["node-0002"]["planner_context"]["work_unit"]["type"],
                "bottleneck_region",
            )
            self.assertEqual(node2_report["bottleneck_region"]["scope"], "pipeline")
            self.assertTrue(node2_report["bottleneck_region"]["inputs"])
            self.assertTrue(node2_report["bottleneck_region"]["outputs"])
            self.assertEqual(node2_report["bottleneck_region"]["evaluation"]["metrics_path"], "metrics.json")
            self.assertEqual(node2_report["candidate_modifications"][0]["path"], "pipeline_config.json")
            self.assertEqual(node2_trial["candidate_modifications"][0]["status"], "modified")
            self.assertEqual(node2_region_context["id"], "pipeline-critical-path")

    def test_pipeline_planner_context_builds_diagnosis_backed_branch_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = create_pipeline_smoke_fixture(root / "fixture")
            task = PipelineTask(repo_root=root, spec_path=spec_path)
            run_dir = root / "run"
            task.prepare_run(run_dir)
            parent = SearchNode(
                id="node-0001",
                parent_id=None,
                depth=0,
                hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
                node_dir=str(run_dir / "nodes" / "node-0001"),
            )
            Path(parent.node_dir).mkdir(parents=True)
            task.create_sandbox(parent, None)
            parent.evaluation_result = task.evaluate(parent)
            root_memory = RootMemory(
                objective=task.snapshot().objective,
                baseline_score=parent.evaluation_result.score,
                best_score=parent.evaluation_result.score,
                best_node_id=parent.id,
            )

            packet = Planner().build_packet(
                task,
                task.snapshot(),
                root_memory,
                parent,
                3,
                upper_bounds={"children_per_expansion": 3, "beam_width": 2, "max_rounds": 1},
            )

            self.assertIsNotNone(packet.region_graph)
            self.assertEqual(packet.region_graph.focus_region_id, "pipeline-critical-path")
            self.assertIsNotNone(packet.bottleneck_card)
            self.assertIn("stage_imbalance", packet.bottleneck_card.diagnoses)
            self.assertIn("pipeline_overlap_failure", packet.bottleneck_card.diagnoses)
            self.assertIn("stage_balance", packet.bottleneck_card.response_families)
            self.assertIn("overlap", packet.bottleneck_card.response_families)
            self.assertEqual(packet.hardware_context["device"], "local-fixture")
            self.assertEqual(packet.hardware_context["scope"], "fixed")
            self.assertEqual(
                [card.family for card in packet.branch_cards],
                ["pipeline_latency", "stage_balance", "overlap"],
            )
            for card in packet.branch_cards:
                self.assertTrue(card.target_signals)
                self.assertTrue(card.supporting_evidence)
                self.assertEqual(card.scope, "pipeline")
                self.assertEqual(card.work_unit["type"], "bottleneck_region")
                self.assertEqual(card.work_unit["region"]["scope"], "pipeline")
                self.assertTrue(card.work_unit["region"]["inputs"])
                self.assertTrue(card.work_unit["region"]["outputs"])
                self.assertEqual(card.work_unit["region"]["candidate_surface"]["target_path"], "pipeline_config.json")
            self.assertEqual(packet.search_policy.branch_budget.children, 3)
            self.assertEqual(packet.search_policy.branch_budget.beam_width, 2)
            region_artifact = json.loads((run_dir / "regions" / "node-0001.region.json").read_text(encoding="utf-8"))
            self.assertEqual(region_artifact["bottleneck_region"]["id"], "pipeline-critical-path")
            self.assertEqual(region_artifact["bottleneck_region"]["evaluation"]["entrypoint"], ["{python}", "evaluate_pipeline.py"])
            self.assertNotEqual(packet.branch_cards[0].work_unit["region_id"], "pipeline_config.json")

            task.spec.metadata["runtime"] = "pytorch"
            parent.evaluation_result.metrics = {
                "end_to_end_ms": 1.0,
                "profiler": {"status": "completed"},
            }
            planner = Planner()
            bounds = {"children_per_expansion": 2, "beam_width": 2, "max_rounds": 4}
            first = planner.build_packet(task, task.snapshot(), root_memory, parent, 2, upper_bounds=bounds)
            self.assertEqual(first.search_policy.branch_budget.children, 2)
            self.assertEqual(first.search_policy.branch_budget.beam_width, 2)
            self.assertIsNone(first.search_policy.branch_budget.max_tree_depth)
            self.assertIsNone(first.search_policy.branch_budget.max_local_iterations)
            self.assertEqual([card.idea_id for card in first.branch_cards], ["forward-overhead", "operator-cost"])
            self.assertIsNone(first.region_graph)
            self.assertTrue(all(not card.work_unit for card in first.branch_cards))
            root_memory.rounds_completed = 1
            next_round = planner.build_packet(task, task.snapshot(), root_memory, parent, 2, upper_bounds=bounds)
            self.assertNotEqual(first.branch_cards[0].id, next_round.branch_cards[0].id)

    def test_setup_pipeline_workspace_copies_source_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_path = create_pipeline_smoke_fixture(root / "fixture")
            output = setup_pipeline_workspace(spec_path, root / "active")
            metadata = json.loads(Path(output["metadata"]).read_text(encoding="utf-8"))

            self.assertTrue(Path(output["target"]).exists())
            self.assertEqual(metadata["target_path"], "pipeline_config.json")
            self.assertIn("end_to_end_ms", metadata["objective_signals"])

    def test_sensing_compresses_pipeline_probe_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_snapshot = PipelineTask(
                repo_root=Path.cwd(),
                spec_path=create_pipeline_smoke_fixture(Path(tmp) / "fixture"),
            ).snapshot()
            recent_probe_results = [
                {
                    "status": "completed",
                    "kind": "stage_summary",
                    "summary": "stage summary",
                    "data": {
                        "metrics": {
                            "end_to_end_ms": 90.0,
                            "throughput": 11.1,
                        },
                        "stage_summary": {
                            "slowest_stage_ms": 42.0,
                            "overlap_ratio": 0.64,
                        },
                    },
                }
            ]

            summary = build_sensing_summary(
                task_snapshot=task_snapshot,
                recent_probe_results=recent_probe_results,
                parent=None,
                node=None,
            )

            self.assertEqual(summary["objective_evaluation"]["verdict"], "need_more_evidence")
            self.assertEqual(summary["objective_evaluation"]["signals"]["end_to_end_ms"], 90.0)
            self.assertEqual(summary["objective_evaluation"]["signals"]["overlap_ratio"], 0.64)


if __name__ == "__main__":
    unittest.main()
