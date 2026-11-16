from __future__ import annotations

import unittest

from autopipeline.search.sensing import build_sensing_summary
from autopipeline.search.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot
from autopipeline.search.feedback import build_agent_feedback


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

        sensing_summary = build_sensing_summary(
            task_snapshot=task_snapshot,
            recent_probe_results=[],
            node=node,
        )
        summary = build_agent_feedback(
            task_snapshot=task_snapshot,
            root=root,
            recent_probe_results=[],
            node=node,
            sensing_summary=sensing_summary,
        )

        self.assertEqual(summary["verdict"], "keep")
        self.assertIn("end_to_end_ms=81.5", summary["evidence"])
        self.assertIn("stage_summary.overlap_ratio=0.72", summary["evidence"])
        self.assertEqual(summary["objective_evaluation"]["verdict"], "keep")
        self.assertNotIn(
            "Missing objective signals in evaluation metrics: end_to_end_ms, overlap_ratio",
            summary["regressions"],
        )

    def test_pipeline_objective_compares_parent_and_child_metrics(self) -> None:
        task_snapshot = TaskSnapshot(
            task_type="pipeline",
            objective="lower end-to-end latency and improve overlap",
            target_path="/tmp/pipeline.yaml",
            editable_paths=["pipeline.yaml"],
            constraints=[],
            reference_paths=[],
            target_kind="pipeline",
            surface_type="workspace",
            objective_signals=["end_to_end_ms", "overlap_ratio", "communication_ms"],
        )
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="parent", title="Parent", core_hypothesis="baseline"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.0,
                metrics={
                    "end_to_end_ms": 100.0,
                    "stage_summary": {
                        "overlap_ratio": 0.55,
                        "communication_ms": 24.0,
                    },
                },
                summary="baseline pipeline",
            ),
        )
        node = SearchNode(
            id="node-0002",
            parent_id="node-0001",
            depth=1,
            hypothesis=Hypothesis(id="branch", title="Branch", core_hypothesis="improve overlap"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.2,
                metrics={
                    "end_to_end_ms": 86.0,
                    "stage_summary": {
                        "overlap_ratio": 0.70,
                        "communication_ms": 18.0,
                    },
                },
                summary="candidate pipeline",
            ),
        )

        sensing_summary = build_sensing_summary(
            task_snapshot=task_snapshot,
            recent_probe_results=[],
            parent=parent,
            node=node,
        )
        summary = build_agent_feedback(
            task_snapshot=task_snapshot,
            root=RootMemory(objective=task_snapshot.objective, best_score=1.0),
            recent_probe_results=[],
            parent=parent,
            node=node,
            sensing_summary=sensing_summary,
        )

        self.assertEqual(summary["verdict"], "keep")
        self.assertEqual(summary["objective_evaluation"]["verdict"], "keep")
        self.assertTrue(
            any("end-to-end latency improved" in item for item in summary["objective_evaluation"]["evidence"])
        )
        self.assertTrue(
            any("overlap ratio improved" in item for item in summary["objective_evaluation"]["evidence"])
        )

    def test_pipeline_objective_rejects_primary_regression(self) -> None:
        task_snapshot = TaskSnapshot(
            task_type="pipeline",
            objective="lower end-to-end latency",
            target_path="/tmp/pipeline.yaml",
            editable_paths=["pipeline.yaml"],
            constraints=[],
            reference_paths=[],
            target_kind="pipeline",
            objective_signals=["end_to_end_ms"],
        )
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="parent", title="Parent", core_hypothesis="baseline"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.0,
                metrics={"end_to_end_ms": 100.0},
                summary="baseline pipeline",
            ),
        )
        node = SearchNode(
            id="node-0002",
            parent_id="node-0001",
            depth=1,
            hypothesis=Hypothesis(id="branch", title="Branch", core_hypothesis="regress latency"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.1,
                metrics={"end_to_end_ms": 112.0},
                summary="correct but slower pipeline",
            ),
        )

        sensing_summary = build_sensing_summary(
            task_snapshot=task_snapshot,
            recent_probe_results=[],
            parent=parent,
            node=node,
        )
        summary = build_agent_feedback(
            task_snapshot=task_snapshot,
            root=RootMemory(objective=task_snapshot.objective),
            recent_probe_results=[],
            parent=parent,
            node=node,
            sensing_summary=sensing_summary,
        )

        self.assertEqual(summary["verdict"], "reject")
        self.assertEqual(summary["objective_evaluation"]["verdict"], "reject")
        self.assertTrue(any("end-to-end latency regressed" in item for item in summary["regressions"]))

    def test_verifier_uses_backend_reject_over_fast_pipeline_metrics(self) -> None:
        task_snapshot = TaskSnapshot(
            task_type="pipeline",
            objective="lower end-to-end latency",
            target_path="/tmp/pipeline.yaml",
            editable_paths=["pipeline.yaml"],
            constraints=[],
            reference_paths=[],
            target_kind="pipeline",
            objective_signals=["end_to_end_ms"],
        )
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="parent", title="Parent", core_hypothesis="baseline"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.0,
                metrics={"end_to_end_ms": 100.0},
                summary="baseline pipeline",
            ),
        )
        node = SearchNode(
            id="node-0002",
            parent_id="node-0001",
            depth=1,
            hypothesis=Hypothesis(id="branch", title="Branch", core_hypothesis="fast wrong candidate"),
            evaluation_result=EvaluationResult(
                accepted=False,
                score=2.0,
                metrics={
                    "end_to_end_ms": 50.0,
                    "verification": {
                        "backend": "executable",
                        "verdict": "reject",
                        "failure_reason": "numerical_error_exceeded",
                        "report_path": "/tmp/report.json",
                        "numerical_error": {
                            "max_abs_error": 1.0,
                        },
                        "performance": {
                            "speedup": 2.0,
                        },
                    },
                },
                summary="fast but wrong",
            ),
        )

        sensing_summary = build_sensing_summary(
            task_snapshot=task_snapshot,
            recent_probe_results=[],
            parent=parent,
            node=node,
        )
        summary = build_agent_feedback(
            task_snapshot=task_snapshot,
            root=RootMemory(objective=task_snapshot.objective),
            recent_probe_results=[],
            parent=parent,
            node=node,
            sensing_summary=sensing_summary,
        )

        self.assertEqual(summary["verdict"], "reject")
        self.assertEqual(summary["objective_evaluation"]["verdict"], "reject")
        self.assertEqual(summary["verification_backend"]["failure_reason"], "numerical_error_exceeded")
        self.assertTrue(any("verification_failure_reason" in item for item in summary["regressions"]))

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

        summary = build_agent_feedback(
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
