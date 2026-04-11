from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tree.agent import AgentAdapter, NoOpAgentAdapter
from tree.search import TreeSearchEngine
from tree.types import EvaluationResult, ExecutionResult, Hypothesis, Insight, RootMemory, SearchConfig, SearchNode, TaskSnapshot


class FakeTask:
    def __init__(
        self,
        *,
        seed_hypotheses: list[Hypothesis],
        score_by_hypothesis: dict[str, float],
        accepted_hypotheses: set[str],
    ) -> None:
        self._seed_hypotheses = seed_hypotheses
        self.score_by_hypothesis = score_by_hypothesis
        self.accepted_hypotheses = accepted_hypotheses
        self.run_dir: Path | None = None
        self.promoted_path: Path | None = None
        self.evaluated_node_ids: list[str] = []

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            task_type="fake",
            objective="improve fake score",
            target_path="/tmp/fake.py",
            editable_paths=["candidate.txt"],
            constraints=["only edit the sandbox candidate"],
            reference_paths=[],
        )

    def prepare_run(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def create_sandbox(self, node: SearchNode, parent: SearchNode | None) -> None:
        sandbox_dir = Path(node.node_dir) / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = sandbox_dir / "candidate.txt"
        if parent is not None and parent.candidate_path:
            candidate_path.write_text(Path(parent.candidate_path).read_text(encoding="utf-8"), encoding="utf-8")
        else:
            candidate_path.write_text("baseline\n", encoding="utf-8")
        node.sandbox_path = str(sandbox_dir)
        node.candidate_path = str(candidate_path)

    def evaluate(self, node: SearchNode) -> EvaluationResult:
        self.evaluated_node_ids.append(node.id)
        key = node.hypothesis.core_hypothesis
        score = self.score_by_hypothesis[key]
        accepted = key in self.accepted_hypotheses
        return EvaluationResult(
            accepted=accepted,
            score=score,
            metrics={"score": score, "returncode": 0},
            summary=f"{key}:{score}",
        )

    def promote(self, node: SearchNode, root: RootMemory) -> str | None:
        assert self.run_dir is not None
        promoted_path = self.run_dir / "promoted.txt"
        promoted_path.write_text(Path(node.candidate_path).read_text(encoding="utf-8"), encoding="utf-8")
        self.promoted_path = promoted_path
        return str(promoted_path)

    def final_verify(self, root: RootMemory) -> dict | None:
        return None

    def seed_hypotheses(
        self,
        parent: SearchNode,
        root: RootMemory,
        limit: int,
    ) -> list[Hypothesis]:
        return self._seed_hypotheses[:limit]


class FakeAgent(AgentAdapter):
    def __init__(self, *, proposals_by_parent: dict[str, list[Hypothesis]]) -> None:
        self.proposals_by_parent = proposals_by_parent

    def propose(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode,
        limit: int,
    ) -> list[Hypothesis]:
        return self.proposals_by_parent.get(parent.id, [])[:limit]

    def execute(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        Path(node.candidate_path).write_text(node.hypothesis.core_hypothesis, encoding="utf-8")
        return ExecutionResult(
            status="completed",
            summary=f"updated {node.id}",
            changed=True,
            candidate_path=node.candidate_path,
        )

    def reflect(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode | None,
        node: SearchNode,
    ) -> Insight:
        assert node.evaluation_result is not None
        polarity = "positive" if node.evaluation_result.accepted else "negative"
        return Insight(
            polarity=polarity,
            summary=f"{node.hypothesis.title}:{node.evaluation_result.score}",
            evidence=[node.hypothesis.core_hypothesis],
            source_node_id=node.id,
        )


class FailingExecuteAgent(FakeAgent):
    def execute(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        return ExecutionResult(
            status="failed",
            summary="executor reported a branch failure",
            changed=False,
            candidate_path=node.candidate_path,
        )


class TreeSearchEngineTests(unittest.TestCase):
    def test_engine_updates_best_node_and_prunes_extra_accepted_nodes(self) -> None:
        fast = Hypothesis(id="fast", title="Fast", core_hypothesis="fast-path")
        slow = Hypothesis(id="slow", title="Slow", core_hypothesis="slow-path")
        task = FakeTask(
            seed_hypotheses=[fast, slow],
            score_by_hypothesis={
                "Record the unmodified baseline candidate.": 1.0,
                "fast-path": 3.0,
                "slow-path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline candidate.",
                "fast-path",
                "slow-path",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [fast, slow]}),
                config=SearchConfig(beam_width=1, max_rounds=1, children_per_expansion=2),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(engine.frontier_node_ids, ["node-0002"])
            self.assertEqual(engine.nodes["node-0003"].status, "pruned")
            self.assertEqual(engine.root_memory.best_score, 3.0)
            self.assertIn("Fast:3.0", engine.root_memory.positive_insights)

            state = json.loads((run_dir / "search_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["final_result"]["best_node_id"], "node-0002")
            self.assertTrue((run_dir / "tree_events.jsonl").exists())
            self.assertIsNotNone(task.promoted_path)

    def test_engine_retains_previous_frontier_when_no_child_is_accepted(self) -> None:
        reject = Hypothesis(id="reject", title="Reject", core_hypothesis="reject-path")
        task = FakeTask(
            seed_hypotheses=[reject],
            score_by_hypothesis={
                "Record the unmodified baseline candidate.": 1.0,
                "reject-path": 0.5,
            },
            accepted_hypotheses={"Record the unmodified baseline candidate."},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [reject]}),
                config=SearchConfig(beam_width=2, max_rounds=1, children_per_expansion=1),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0001")
            self.assertEqual(engine.frontier_node_ids, ["node-0001"])
            self.assertEqual(engine.nodes["node-0002"].status, "completed")
            self.assertIn("Reject:0.5", engine.root_memory.negative_insights)

            events = (run_dir / "tree_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("frontier_updated", events)
            self.assertIn('"retained_previous_frontier": true', events)

    def test_rejected_high_score_child_does_not_replace_best_node(self) -> None:
        reject = Hypothesis(id="reject", title="Reject", core_hypothesis="reject-high-score")
        task = FakeTask(
            seed_hypotheses=[reject],
            score_by_hypothesis={
                "Record the unmodified baseline candidate.": 1.0,
                "reject-high-score": 9.0,
            },
            accepted_hypotheses={"Record the unmodified baseline candidate."},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [reject]}),
                config=SearchConfig(beam_width=1, max_rounds=1, children_per_expansion=1),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0001")
            self.assertEqual(engine.root_memory.best_score, 1.0)
            self.assertEqual(engine.nodes["node-0002"].evaluation_result.score, 9.0)
            self.assertFalse(engine.nodes["node-0002"].evaluation_result.accepted)

    def test_executor_failed_status_short_circuits_evaluate_and_reflect(self) -> None:
        child = Hypothesis(id="child", title="Child", core_hypothesis="child-path")
        task = FakeTask(
            seed_hypotheses=[child],
            score_by_hypothesis={
                "Record the unmodified baseline candidate.": 1.0,
                "child-path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline candidate.",
                "child-path",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FailingExecuteAgent(proposals_by_parent={"node-0001": [child]}),
                config=SearchConfig(beam_width=1, max_rounds=1, children_per_expansion=1),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0001")
            self.assertEqual(task.evaluated_node_ids, ["node-0001"])
            self.assertEqual(engine.nodes["node-0002"].status, "failed")
            self.assertIsNone(engine.nodes["node-0002"].evaluation_result)
            self.assertIsNone(engine.nodes["node-0002"].insight)
            self.assertEqual(engine.nodes["node-0002"].error, "executor reported a branch failure")

    def test_noop_agent_can_drive_a_minimal_search_run(self) -> None:
        task = FakeTask(
            seed_hypotheses=[
                Hypothesis(id="seed-1", title="Seed 1", core_hypothesis="seed-improvement"),
            ],
            score_by_hypothesis={
                "Record the unmodified baseline candidate.": 1.0,
                "seed-improvement": 1.5,
            },
            accepted_hypotheses={
                "Record the unmodified baseline candidate.",
                "seed-improvement",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=NoOpAgentAdapter(),
                config=SearchConfig(beam_width=1, max_rounds=1, children_per_expansion=1),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.total_nodes, 2)
            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(task.evaluated_node_ids, ["node-0001", "node-0002"])
            self.assertTrue((run_dir / "search_state.json").exists())
            self.assertTrue((run_dir / "tree_events.jsonl").exists())
            self.assertIsNotNone(result.promoted_path)


if __name__ == "__main__":
    unittest.main()
