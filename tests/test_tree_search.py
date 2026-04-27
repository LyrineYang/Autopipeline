from __future__ import annotations

import json
import shutil
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


class PolicyAwareFakeTask(FakeTask):
    def __init__(
        self,
        *,
        seed_hypotheses: list[Hypothesis],
        score_by_hypothesis: dict[str, float],
        accepted_hypotheses: set[str],
        search_policy: dict[str, object],
    ) -> None:
        super().__init__(
            seed_hypotheses=seed_hypotheses,
            score_by_hypothesis=score_by_hypothesis,
            accepted_hypotheses=accepted_hypotheses,
        )
        self.search_policy = search_policy

    def planner_context(self, parent, root, limit, *, upper_bounds=None):
        return {
            "branch_cards": [
                {
                    "id": item.id,
                    "family": item.family,
                    "title": item.title,
                    "core_hypothesis": item.core_hypothesis,
                }
                for item in self._seed_hypotheses[:limit]
            ],
            "search_policy": self.search_policy,
        }


class RejectingFinalVerifyTask(FakeTask):
    def final_verify(self, root: RootMemory) -> dict | None:
        return {
            "accepted": False,
            "summary": "final verification failed",
            "correctness": "FAIL",
        }


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
        planner_packet=None,
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


class LoopAwareFakeTask(FakeTask):
    def branch_iteration_limit(self, parent, node) -> int:
        return 3

    def evaluate(self, node: SearchNode) -> EvaluationResult:
        self.evaluated_node_ids.append(node.id)
        key = Path(node.candidate_path).read_text(encoding="utf-8").strip()
        score = self.score_by_hypothesis[key]
        accepted = key in self.accepted_hypotheses
        return EvaluationResult(
            accepted=accepted,
            score=score,
            metrics={"score": score, "returncode": 0},
            summary=f"{key}:{score}",
            raw_log_paths={},
        )


class PipelineLoopTask(LoopAwareFakeTask):
    def create_sandbox(self, node: SearchNode, parent: SearchNode | None) -> None:
        super().create_sandbox(node, parent)
        pipeline_path = Path(node.sandbox_path) / "pipeline.json"
        if parent is not None and parent.sandbox_path:
            source = Path(parent.sandbox_path) / "pipeline.json"
            if source.exists():
                pipeline_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                pipeline_path.write_text(json.dumps({"variant": "baseline"}), encoding="utf-8")
        else:
            pipeline_path.write_text(json.dumps({"variant": "baseline"}), encoding="utf-8")

    def checkpoint_candidate(self, node: SearchNode, checkpoint_path: Path) -> None:
        checkpoint_dir = checkpoint_path.parent / checkpoint_path.stem
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(node.candidate_path), checkpoint_dir / "candidate.txt")
        shutil.copy2(Path(node.sandbox_path) / "pipeline.json", checkpoint_dir / "pipeline.json")

    def restore_candidate(self, node: SearchNode, checkpoint_path: Path) -> None:
        checkpoint_dir = checkpoint_path.parent / checkpoint_path.stem
        shutil.copy2(checkpoint_dir / "candidate.txt", Path(node.candidate_path))
        shutil.copy2(checkpoint_dir / "pipeline.json", Path(node.sandbox_path) / "pipeline.json")


class SurfaceAwarePipelineTask(PipelineLoopTask):
    def prepare_execution_surface(self, node: SearchNode, parent: SearchNode | None) -> None:
        candidate_text = Path(node.candidate_path).read_text(encoding="utf-8").strip()
        (Path(node.sandbox_path) / "surface.txt").write_text(candidate_text, encoding="utf-8")
        node.planner_context["effective_editable_paths"] = ["candidate.txt", "pipeline.json"]


class LoopAgent(FakeAgent):
    def __init__(self, *, proposals_by_parent: dict[str, list[Hypothesis]], edits_by_iteration: dict[int, str]) -> None:
        super().__init__(proposals_by_parent=proposals_by_parent)
        self.edits_by_iteration = edits_by_iteration

    def execute(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        iteration = int(node.planner_context.get("local_iteration", 1))
        content = self.edits_by_iteration[iteration]
        Path(node.candidate_path).write_text(content, encoding="utf-8")
        return ExecutionResult(
            status="completed",
            summary=f"iteration {iteration}",
            changed=True,
            candidate_path=node.candidate_path,
            raw_log_paths={"stdout": str(Path(node.node_dir) / f"execute-{iteration}.log")},
        )


class PipelineLoopAgent(LoopAgent):
    def execute(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        result = super().execute(task, task_snapshot, root, parent, node)
        iteration = int(node.planner_context.get("local_iteration", 1))
        content = self.edits_by_iteration[iteration]
        (Path(node.sandbox_path) / "pipeline.json").write_text(
            json.dumps({"variant": content}),
            encoding="utf-8",
        )
        return result


class ExhaustingLoopAgent(LoopAgent):
    def execute(
        self,
        task,
        task_snapshot,
        root,
        parent: SearchNode,
        node: SearchNode,
    ) -> ExecutionResult:
        iteration = int(node.planner_context.get("local_iteration", 1))
        content = self.edits_by_iteration[iteration]
        Path(node.candidate_path).write_text(content, encoding="utf-8")
        return ExecutionResult(
            status="completed",
            summary=f"iteration {iteration} exhausted",
            changed=False,
            candidate_path=node.candidate_path,
            raw_log_paths={"stdout": str(Path(node.node_dir) / f"execute-{iteration}.log")},
        )


class TreeSearchEngineTests(unittest.TestCase):
    def test_same_core_hypothesis_can_expand_different_pipeline_targets(self) -> None:
        first = Hypothesis(
            id="stage-a",
            title="Stage A",
            core_hypothesis="raise overlap in the hot path",
            scope="pipeline",
            target="encoder.block.0",
            family="runtime_policy",
        )
        second = Hypothesis(
            id="stage-b",
            title="Stage B",
            core_hypothesis="raise overlap in the hot path",
            scope="pipeline",
            target="encoder.block.1",
            family="runtime_policy",
        )
        task = FakeTask(
            seed_hypotheses=[first, second],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "raise overlap in the hot path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
                "raise overlap in the hot path",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [first, second]}),
                config=SearchConfig(beam_width=2, max_rounds=1, children_per_expansion=2),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.total_nodes, 3)
            self.assertIn("node-0002", engine.nodes)
            self.assertIn("node-0003", engine.nodes)
            self.assertEqual(engine.nodes["node-0002"].hypothesis.target, "encoder.block.0")
            self.assertEqual(engine.nodes["node-0003"].hypothesis.target, "encoder.block.1")

    def test_same_target_same_core_can_expand_when_branch_ids_differ(self) -> None:
        first = Hypothesis(
            id="branch-a",
            title="Variant A",
            core_hypothesis="raise overlap in the hot path",
            scope="pipeline",
            target="encoder.block.0",
            family="runtime_policy",
        )
        second = Hypothesis(
            id="branch-b",
            title="Variant B",
            core_hypothesis="raise overlap in the hot path",
            scope="pipeline",
            target="encoder.block.0",
            family="runtime_policy",
        )
        task = FakeTask(
            seed_hypotheses=[first, second],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "raise overlap in the hot path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
                "raise overlap in the hot path",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [first, second]}),
                config=SearchConfig(beam_width=2, max_rounds=1, children_per_expansion=2),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.total_nodes, 3)
            self.assertEqual(engine.nodes["node-0002"].hypothesis.id, "branch-a")
            self.assertEqual(engine.nodes["node-0003"].hypothesis.id, "branch-b")

    def test_engine_updates_best_node_and_prunes_extra_accepted_nodes(self) -> None:
        fast = Hypothesis(id="fast", title="Fast", core_hypothesis="fast-path")
        slow = Hypothesis(id="slow", title="Slow", core_hypothesis="slow-path")
        task = FakeTask(
            seed_hypotheses=[fast, slow],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "fast-path": 3.0,
                "slow-path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
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

    def test_agent_budget_can_stop_expansion_before_executor_call(self) -> None:
        branch = Hypothesis(id="branch", title="Branch", core_hypothesis="branch-path")
        task = FakeTask(
            seed_hypotheses=[branch],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "branch-path": 2.0,
            },
            accepted_hypotheses={"Record the unmodified baseline workspace.", "branch-path"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [branch]}),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command="agent-backed-executor",
                    max_agent_calls=0,
                ),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.total_nodes, 1)
            self.assertEqual(result.best_node_id, "node-0001")
            state = json.loads((run_dir / "search_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["budget"]["agent_usage"]["remaining_agent_calls"], 0)

    def test_engine_retains_previous_frontier_when_no_child_is_accepted(self) -> None:
        reject = Hypothesis(id="reject", title="Reject", core_hypothesis="reject-path")
        task = FakeTask(
            seed_hypotheses=[reject],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "reject-path": 0.5,
            },
            accepted_hypotheses={"Record the unmodified baseline workspace."},
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
                "Record the unmodified baseline workspace.": 1.0,
                "reject-high-score": 9.0,
            },
            accepted_hypotheses={"Record the unmodified baseline workspace."},
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

    def test_rejected_baseline_does_not_block_accepted_child(self) -> None:
        child = Hypothesis(id="child", title="Child", core_hypothesis="child-path")
        task = FakeTask(
            seed_hypotheses=[child],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 9.0,
                "child-path": 1.5,
            },
            accepted_hypotheses={"child-path"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [child]}),
                config=SearchConfig(beam_width=1, max_rounds=1, children_per_expansion=1),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(engine.root_memory.baseline_score, 9.0)
            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(engine.root_memory.best_score, 1.5)
            self.assertEqual(Path(result.promoted_path).read_text(encoding="utf-8"), "child-path")

    def test_failed_final_verify_discards_promoted_artifact(self) -> None:
        child = Hypothesis(id="child", title="Child", core_hypothesis="child-path")
        task = RejectingFinalVerifyTask(
            seed_hypotheses=[child],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "child-path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
                "child-path",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [child]}),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    enable_final_verify=True,
                ),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertIsNone(result.promoted_path)
            self.assertFalse(result.final_verify["accepted"])
            self.assertIsNotNone(task.promoted_path)
            self.assertFalse(task.promoted_path.exists())
            self.assertEqual(engine.nodes["node-0002"].status, "completed")

    def test_executor_failed_status_short_circuits_evaluate_and_reflect(self) -> None:
        child = Hypothesis(id="child", title="Child", core_hypothesis="child-path")
        task = FakeTask(
            seed_hypotheses=[child],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "child-path": 2.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
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
                "Record the unmodified baseline workspace.": 1.0,
                "seed-improvement": 1.5,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
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

    def test_planner_policy_can_tighten_children_beam_and_rounds(self) -> None:
        first = Hypothesis(id="first", title="First", core_hypothesis="first-path", family="memory_hierarchy")
        second = Hypothesis(id="second", title="Second", core_hypothesis="second-path", family="parallelization")
        task = PolicyAwareFakeTask(
            seed_hypotheses=[first, second],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "first-path": 2.0,
                "second-path": 3.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
                "first-path",
                "second-path",
            },
            search_policy={
                "recommended_scope": "kernel",
                "scope_options": ["kernel"],
                "exploration_bias": "exploit",
                "branch_budget": {
                    "children": 1,
                    "beam_width": 1,
                    "rounds": 1,
                },
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [first, second]}),
                config=SearchConfig(beam_width=3, max_rounds=3, children_per_expansion=2),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.total_nodes, 2)
            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(engine.root_memory.rounds_completed, 1)
            self.assertNotIn("node-0003", engine.nodes)

    def test_planner_can_keep_beam_wider_than_children_cap(self) -> None:
        first = Hypothesis(id="first", title="First", core_hypothesis="first-path", family="memory_hierarchy")
        second = Hypothesis(id="second", title="Second", core_hypothesis="second-path", family="parallelization")
        task = PolicyAwareFakeTask(
            seed_hypotheses=[first, second],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "first-path": 2.0,
                "second-path": 3.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
                "first-path",
                "second-path",
            },
            search_policy={
                "recommended_scope": "kernel",
                "scope_options": ["kernel"],
                "exploration_bias": "balanced",
                "branch_budget": {
                    "children": 1,
                    "beam_width": 2,
                    "rounds": 2,
                },
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(proposals_by_parent={"node-0001": [first, second]}),
                config=SearchConfig(beam_width=3, max_rounds=2, children_per_expansion=1),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.total_nodes, 2)
            self.assertEqual(engine.frontier_node_ids, ["node-0002"])
            events = (run_dir / "tree_events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"effective_beam_width": 2', events)

    def test_round_budget_is_honored_per_parent(self) -> None:
        first = Hypothesis(id="first", title="First", core_hypothesis="first-path", family="memory_hierarchy")
        second = Hypothesis(id="second", title="Second", core_hypothesis="second-path", family="parallelization")
        followup = Hypothesis(id="follow", title="Follow", core_hypothesis="follow-path", family="memory_hierarchy")

        class MixedPolicyTask(FakeTask):
            def planner_context(self, parent, root, limit, *, upper_bounds=None):
                if parent.hypothesis.core_hypothesis == "Record the unmodified baseline workspace.":
                    families = [first, second]
                    budget = 3
                    children = 2
                else:
                    families = [followup]
                    budget = 1 if parent.hypothesis.core_hypothesis == "first-path" else 3
                    children = 1
                return {
                    "branch_cards": [
                        {
                            "id": item.id,
                            "family": item.family,
                            "title": item.title,
                            "core_hypothesis": item.core_hypothesis,
                        }
                        for item in families[:limit]
                    ],
                    "search_policy": {
                        "recommended_scope": "kernel",
                        "scope_options": ["kernel"],
                        "exploration_bias": "balanced",
                        "branch_budget": {
                            "children": children,
                            "beam_width": 2,
                            "rounds": budget,
                        },
                    },
                }

        task = MixedPolicyTask(
            seed_hypotheses=[first, second],
            score_by_hypothesis={
                "Record the unmodified baseline workspace.": 1.0,
                "first-path": 2.0,
                "second-path": 3.0,
                "follow-path": 4.0,
            },
            accepted_hypotheses={
                "Record the unmodified baseline workspace.",
                "first-path",
                "second-path",
                "follow-path",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=FakeAgent(
                    proposals_by_parent={
                        "node-0001": [first, second],
                        "node-0002": [followup],
                        "node-0003": [followup],
                    }
                ),
                config=SearchConfig(beam_width=2, max_rounds=3, children_per_expansion=2),
                run_dir=run_dir,
            )

            engine.run()

            self.assertIn("node-0004", engine.nodes)
            self.assertNotIn("node-0005", engine.nodes)

    def test_branch_local_loop_keeps_best_iteration_candidate(self) -> None:
        branch = Hypothesis(id="branch", title="Branch", core_hypothesis="branch-path")
        task = LoopAwareFakeTask(
            seed_hypotheses=[branch],
            score_by_hypothesis={
                "baseline": 1.0,
                "trial-1": 2.0,
                "trial-2": 3.5,
                "trial-3": 1.5,
                "Record the unmodified baseline workspace.": 1.0,
            },
            accepted_hypotheses={"baseline", "trial-1", "trial-2", "trial-3", "Record the unmodified baseline workspace."},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=LoopAgent(
                    proposals_by_parent={"node-0001": [branch]},
                    edits_by_iteration={1: "trial-1", 2: "trial-2", 3: "trial-3"},
                ),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command="executor",
                ),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(engine.nodes["node-0002"].evaluation_result.score, 3.5)
            self.assertEqual(Path(engine.nodes["node-0002"].candidate_path).read_text(encoding="utf-8"), "trial-2")
            self.assertEqual(engine.nodes["node-0002"].execution_result.metadata["branch_iterations"], 3)
            self.assertEqual(
                [entry["iteration"] for entry in engine.nodes["node-0002"].execution_result.metadata["local_history"]],
                [1, 2, 3],
            )

    def test_branch_local_loop_restores_pipeline_surface_checkpoint(self) -> None:
        branch = Hypothesis(
            id="branch",
            title="Branch",
            core_hypothesis="pipeline-branch",
            scope="pipeline",
            target="attention-stack",
        )
        task = PipelineLoopTask(
            seed_hypotheses=[branch],
            score_by_hypothesis={
                "baseline": 1.0,
                "trial-1": 2.0,
                "trial-2": 3.5,
                "trial-3": 1.5,
                "Record the unmodified baseline workspace.": 1.0,
            },
            accepted_hypotheses={
                "baseline",
                "trial-1",
                "trial-2",
                "trial-3",
                "Record the unmodified baseline workspace.",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=PipelineLoopAgent(
                    proposals_by_parent={"node-0001": [branch]},
                    edits_by_iteration={1: "trial-1", 2: "trial-2", 3: "trial-3"},
                ),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command="executor",
                ),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(Path(engine.nodes["node-0002"].candidate_path).read_text(encoding="utf-8"), "trial-2")
            pipeline_state = json.loads(
                (Path(engine.nodes["node-0002"].sandbox_path) / "pipeline.json").read_text(encoding="utf-8")
            )
            self.assertEqual(pipeline_state["variant"], "trial-2")

    def test_branch_local_loop_refreshes_derived_surface_after_restore(self) -> None:
        branch = Hypothesis(
            id="branch",
            title="Branch",
            core_hypothesis="pipeline-branch",
            scope="pipeline",
            target="attention-stack",
        )
        task = SurfaceAwarePipelineTask(
            seed_hypotheses=[branch],
            score_by_hypothesis={
                "baseline": 1.0,
                "trial-1": 2.0,
                "trial-2": 3.5,
                "trial-3": 1.5,
                "Record the unmodified baseline workspace.": 1.0,
            },
            accepted_hypotheses={
                "baseline",
                "trial-1",
                "trial-2",
                "trial-3",
                "Record the unmodified baseline workspace.",
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=PipelineLoopAgent(
                    proposals_by_parent={"node-0001": [branch]},
                    edits_by_iteration={1: "trial-1", 2: "trial-2", 3: "trial-3"},
                ),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command="executor",
                ),
                run_dir=run_dir,
            )

            engine.run()

            surface_text = (Path(engine.nodes["node-0002"].sandbox_path) / "surface.txt").read_text(encoding="utf-8")
            self.assertEqual(surface_text, "trial-2")

    def test_branch_local_loop_honors_planner_round_limit(self) -> None:
        branch = Hypothesis(id="branch", title="Branch", core_hypothesis="branch-path", family="memory_hierarchy")

        class RoundLimitedTask(LoopAwareFakeTask):
            def planner_context(self, parent, root, limit, *, upper_bounds=None):
                return {
                    "branch_cards": [
                        {
                            "id": branch.id,
                            "family": branch.family,
                            "title": branch.title,
                            "core_hypothesis": branch.core_hypothesis,
                        }
                    ],
                    "search_policy": {
                        "recommended_scope": "kernel",
                        "scope_options": ["kernel"],
                        "exploration_bias": "exploit",
                        "branch_budget": {
                            "children": 1,
                            "beam_width": 1,
                            "rounds": 1,
                        },
                    },
                }

        task = RoundLimitedTask(
            seed_hypotheses=[branch],
            score_by_hypothesis={
                "baseline": 1.0,
                "trial-1": 2.0,
                "trial-2": 3.5,
                "Record the unmodified baseline workspace.": 1.0,
            },
            accepted_hypotheses={"baseline", "trial-1", "trial-2", "Record the unmodified baseline workspace."},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=LoopAgent(
                    proposals_by_parent={"node-0001": [branch]},
                    edits_by_iteration={1: "trial-1", 2: "trial-2"},
                ),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command="executor",
                ),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0002")
            self.assertEqual(engine.nodes["node-0002"].evaluation_result.score, 2.0)
            self.assertNotIn("branch_iterations", engine.nodes["node-0002"].execution_result.metadata)

    def test_branch_local_loop_records_exhausted_branch_without_failure(self) -> None:
        branch = Hypothesis(id="branch", title="Branch", core_hypothesis="branch-path")
        task = LoopAwareFakeTask(
            seed_hypotheses=[branch],
            score_by_hypothesis={
                "baseline": 1.0,
                "trial-1": 0.8,
                "Record the unmodified baseline workspace.": 1.0,
            },
            accepted_hypotheses={"baseline", "Record the unmodified baseline workspace."},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            engine = TreeSearchEngine(
                task=task,
                agent=ExhaustingLoopAgent(
                    proposals_by_parent={"node-0001": [branch]},
                    edits_by_iteration={1: "trial-1"},
                ),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command="executor",
                ),
                run_dir=run_dir,
            )

            result = engine.run()

            self.assertEqual(result.best_node_id, "node-0001")
            self.assertEqual(engine.nodes["node-0002"].status, "pruned")
            self.assertTrue(engine.nodes["node-0002"].evaluation_result.accepted)
            self.assertEqual(engine.nodes["node-0002"].evaluation_result.score, 1.0)
            self.assertEqual(
                Path(engine.nodes["node-0002"].candidate_path).read_text(encoding="utf-8").strip(),
                "baseline",
            )
            self.assertEqual(
                engine.nodes["node-0002"].execution_result.metadata["branch_outcome"],
                "exhausted",
            )


if __name__ == "__main__":
    unittest.main()
