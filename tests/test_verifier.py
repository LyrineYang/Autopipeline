from __future__ import annotations

import unittest

from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot
from tree.verifier import build_verifier_summary


class CustomVerifierTask:
    def augment_verifier_summary(
        self,
        *,
        task_snapshot,
        root,
        recent_probe_results,
        parent,
        node,
    ):
        return {
            "status": "warning",
            "evidence": ["custom evidence"],
            "uncertainty": ["custom uncertainty"],
        }


class VerifierTests(unittest.TestCase):
    def test_verifier_surfaces_pipeline_metrics_and_stage_summary(self) -> None:
        task_snapshot = TaskSnapshot(
            task_type="pipeline",
            objective="lower end-to-end cost",
            target_path="/tmp/pipeline.yaml",
            editable_paths=["pipeline.yaml"],
            constraints=[],
            reference_paths=[],
            target_kind="pipeline",
            surface_type="workspace",
            objective_signals=["end_to_end_ms", "overlap_ratio"],
        )
        root = RootMemory(objective=task_snapshot.objective, best_score=1.0)
        node = SearchNode(
            id="node-0002",
            parent_id="node-0001",
            depth=1,
            hypothesis=Hypothesis(id="branch", title="Branch", core_hypothesis="improve overlap"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.2,
                metrics={
                    "end_to_end_ms": 81.5,
                    "tokens_per_second": 2200,
                    "stage_summary": {
                        "slowest_stage": "attention",
                        "slowest_stage_ms": 33.0,
                        "overlap_ratio": 0.72,
                    },
                },
                summary="accepted pipeline improvement",
            ),
        )

        summary = build_verifier_summary(
            task_snapshot=task_snapshot,
            root=root,
            recent_probe_results=[],
            node=node,
        )

        self.assertEqual(summary["verdict"], "keep")
        self.assertIn("end_to_end_ms=81.5", summary["evidence"])
        self.assertIn("stage_summary.overlap_ratio=0.72", summary["evidence"])
        self.assertNotIn(
            "Missing objective signals in evaluation metrics: end_to_end_ms, overlap_ratio",
            summary["regressions"],
        )

    def test_verifier_accepts_task_specific_summary_augmentation(self) -> None:
        task_snapshot = TaskSnapshot(
            task_type="pipeline",
            objective="lower end-to-end cost",
            target_path="/tmp/pipeline.yaml",
            editable_paths=["pipeline.yaml"],
            constraints=[],
            reference_paths=[],
        )
        node = SearchNode(
            id="node-0002",
            parent_id="node-0001",
            depth=1,
            hypothesis=Hypothesis(id="branch", title="Branch", core_hypothesis="improve overlap"),
            evaluation_result=EvaluationResult(
                accepted=False,
                score=0.9,
                metrics={},
                summary="regressed",
            ),
        )

        summary = build_verifier_summary(
            task_snapshot=task_snapshot,
            root=RootMemory(objective=task_snapshot.objective),
            recent_probe_results=[],
            node=node,
            task=CustomVerifierTask(),
        )

        self.assertEqual(summary["status"], "warning")
        self.assertIn("custom evidence", summary["evidence"])
        self.assertIn("custom uncertainty", summary["uncertainty"])


if __name__ == "__main__":
    unittest.main()
