from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tree.pipeline import (
    PipelineTask,
    create_pipeline_smoke_fixture,
    run_pipeline_smoke,
    setup_pipeline_workspace,
)
from tree.sensing import build_sensing_summary
from tree.types import Hypothesis, RootMemory, SearchNode


class PipelineBridgeTests(unittest.TestCase):
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

    def test_pipeline_smoke_runs_agent_backed_tree_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_pipeline_smoke(repo_root=root, run_dir=root / "smoke")
            state = json.loads(
                (Path(result.run_dir) / "search_state.json").read_text(encoding="utf-8")
            )
            baseline = state["nodes"]["node-0001"]["evaluation_result"]
            candidate = state["nodes"]["node-0002"]["evaluation_result"]

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertTrue(Path(result.promoted_path or "").exists())
            self.assertGreater(candidate["score"], baseline["score"])
            self.assertLess(
                candidate["metrics"]["end_to_end_ms"],
                baseline["metrics"]["end_to_end_ms"],
            )

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
