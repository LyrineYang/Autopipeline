from __future__ import annotations

import json
import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tree.agent import CommandAgentAdapter
from tree.planner import Planner
from tree.search import TreeSearchEngine
from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchConfig, SearchNode, TaskSnapshot


class PlannerContextTask:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def seed_hypotheses(self, parent, root, limit):
        return [
            Hypothesis(
                id="seed-1",
                title="Seed 1",
                core_hypothesis="seed hypothesis",
                editable_path_hints=["kernel.py"],
                family="memory_hierarchy",
            )
        ][:limit]

    def planner_context(self, parent, root, limit, *, upper_bounds=None):
        return {
            "problem_summary": "Current target is a hot kernel; stay inside kernel.py.",
            "planner_rules": {
                "allowed_scopes": ["kernel"],
                "allowed_families": ["memory_hierarchy", "parallelization"],
                "editable_paths": ["kernel.py"],
            },
            "frozen_decisions": ["Keep the branch local to kernel.py."],
        }


class StableSearchTask:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.run_dir: Path | None = None
        self.promoted_path: Path | None = None

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            task_type="fake",
            objective="improve deterministic score",
            target_path=str(self.repo_root / "kernel.py"),
            editable_paths=["kernel.py"],
            constraints=["only edit the sandbox candidate"],
            reference_paths=[],
        )

    def prepare_run(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def create_sandbox(self, node: SearchNode, parent: SearchNode | None) -> None:
        sandbox_dir = Path(node.node_dir) / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = sandbox_dir / "kernel.py"
        if parent is not None and parent.candidate_path:
            candidate_path.write_text(Path(parent.candidate_path).read_text(encoding="utf-8"), encoding="utf-8")
        else:
            candidate_path.write_text("baseline\n", encoding="utf-8")
        node.sandbox_path = str(sandbox_dir)
        node.candidate_path = str(candidate_path)

    def evaluate(self, node: SearchNode) -> EvaluationResult:
        content = Path(node.candidate_path).read_text(encoding="utf-8").strip()
        score = {
            "baseline": 1.0,
            "fast": 3.0,
            "safe": 2.0,
        }[content]
        return EvaluationResult(
            accepted=True,
            score=score,
            metrics={"score": score, "returncode": 0},
            summary=f"{content}:{score}",
        )

    def promote(self, node: SearchNode, root: RootMemory) -> str | None:
        assert self.run_dir is not None
        promoted_path = self.run_dir / "promoted.txt"
        promoted_path.write_text(Path(node.candidate_path).read_text(encoding="utf-8"), encoding="utf-8")
        self.promoted_path = promoted_path
        return str(promoted_path)

    def final_verify(self, root: RootMemory) -> dict | None:
        return None

    def seed_hypotheses(self, parent, root, limit):
        return [
            Hypothesis(
                id="fast",
                title="Fast",
                core_hypothesis="fast-branch",
                editable_path_hints=["kernel.py"],
                family="memory_hierarchy",
            ),
            Hypothesis(
                id="safe",
                title="Safe",
                core_hypothesis="safe-branch",
                editable_path_hints=["kernel.py"],
                family="parallelization",
            ),
        ][:limit]

    def planner_context(self, parent, root, limit, *, upper_bounds=None):
        return {
            "branch_cards": [
                {
                    "id": "fast",
                    "title": "Fast",
                    "core_hypothesis": "fast-branch",
                    "editable_path_hints": ["kernel.py"],
                    "family": "memory_hierarchy",
                },
                {
                    "id": "safe",
                    "title": "Safe",
                    "core_hypothesis": "safe-branch",
                    "editable_path_hints": ["kernel.py"],
                    "family": "parallelization",
                },
            ][:limit],
            "planner_rules": {
                "allowed_scopes": ["kernel"],
                "allowed_families": ["memory_hierarchy", "parallelization"],
                "editable_paths": ["kernel.py"],
            },
            "frozen_decisions": ["Keep the search local to kernel.py."],
        }


class AgentFrameworkStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo_root = Path(self.tempdir.name)
        (self.repo_root / "kernel.py").write_text("baseline\n", encoding="utf-8")

    def test_planner_probe_roundtrip_is_stable_across_repeated_runs(self) -> None:
        sandbox_dir = self.repo_root / "planner-sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        (sandbox_dir / "kernel.py").write_text("baseline\n", encoding="utf-8")
        parent_dir = self.repo_root / "planner-node"
        parent_dir.mkdir(parents=True, exist_ok=True)
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(parent_dir),
            sandbox_path=str(sandbox_dir),
            candidate_path=str(sandbox_dir / "kernel.py"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.0,
                metrics={"returncode": 0},
                summary="baseline",
            ),
        )
        task = PlannerContextTask(self.repo_root)
        snapshot = TaskSnapshot(
            task_type="fake",
            objective="planner stability",
            target_path=str(self.repo_root / "kernel.py"),
            editable_paths=["kernel.py"],
            constraints=[],
            reference_paths=[],
        )
        root = RootMemory(objective="planner stability")
        script = self._write_script(
            "planner_stability.py",
            """
            import json
            import os
            import time
            from pathlib import Path

            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps([{"kind": "read_text", "path": "kernel.py", "lines": 1}]),
                encoding="utf-8",
            )
            results_path = Path(os.environ["TREE_PROBE_RESULTS_PATH"])
            for _ in range(40):
                try:
                    results = json.loads(results_path.read_text(encoding="utf-8"))
                except Exception:
                    results = []
                if results:
                    Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                        json.dumps(
                            {
                                "branch_cards": [
                                    {
                                        "id": "variant-a",
                                        "title": "Variant A",
                                        "core_hypothesis": "exploit variant a",
                                        "family": "memory_hierarchy",
                                        "editable_path_hints": ["kernel.py"],
                                    },
                                    {
                                        "id": "variant-b",
                                        "title": "Variant B",
                                        "core_hypothesis": "exploit variant b",
                                        "family": "memory_hierarchy",
                                        "editable_path_hints": ["kernel.py"],
                                    },
                                ],
                                "frozen_decisions": ["planner probe handshake is stable"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    break
                time.sleep(0.05)
            else:
                raise SystemExit(9)
            """,
        )
        planner = Planner(planner_command=self._python_command(script))

        for iteration in range(6):
            with self.subTest(iteration=iteration):
                packet = planner.build_packet(
                    task,
                    snapshot,
                    root,
                    parent,
                    2,
                    upper_bounds={"beam_width": 2, "children_per_expansion": 2, "max_rounds": 2},
                )
                self.assertEqual(
                    [card.core_hypothesis for card in packet.branch_cards],
                    ["exploit variant a", "exploit variant b"],
                )
                self.assertEqual(
                    [card.family for card in packet.branch_cards],
                    ["memory_hierarchy", "memory_hierarchy"],
                )
                self.assertEqual(packet.frozen_decisions, ["planner probe handshake is stable"])

    def test_executor_probe_roundtrip_is_stable_across_repeated_runs(self) -> None:
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(
                self._write_script(
                    "executor_stability.py",
                    """
                    import json
                    import os
                    import time
                    from pathlib import Path

                    Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                        json.dumps([{"kind": "read_text", "path": "kernel.py", "lines": 1}]),
                        encoding="utf-8",
                    )
                    results_path = Path(os.environ["TREE_PROBE_RESULTS_PATH"])
                    for _ in range(40):
                        try:
                            results = json.loads(results_path.read_text(encoding="utf-8"))
                        except Exception:
                            results = []
                        if results:
                            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                                json.dumps(
                                    {
                                        "status": "completed",
                                        "summary": results[0]["summary"],
                                        "changed": False,
                                        "metadata": {
                                            "probe_status": results[0]["status"],
                                            "line_count": len(results[0]["data"]["lines"]),
                                        },
                                    }
                                ),
                                encoding="utf-8",
                            )
                            break
                        time.sleep(0.05)
                    else:
                        raise SystemExit(9)
                    """,
                )
            ),
        )
        task = PlannerContextTask(self.repo_root)
        snapshot = TaskSnapshot(
            task_type="fake",
            objective="executor stability",
            target_path=str(self.repo_root / "kernel.py"),
            editable_paths=["kernel.py"],
            constraints=[],
            reference_paths=[],
        )
        root = RootMemory(objective="executor stability")
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "parent-node"),
            status="completed",
            sandbox_path=str(self.repo_root),
            candidate_path=str(self.repo_root / "kernel.py"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.0,
                metrics={"returncode": 0},
                summary="baseline",
            ),
        )
        Path(parent.node_dir).mkdir(parents=True, exist_ok=True)

        for iteration in range(6):
            with self.subTest(iteration=iteration):
                node_dir = self.repo_root / f"execute-node-{iteration:02d}"
                node = SearchNode(
                    id=f"node-{iteration + 2:04d}",
                    parent_id=parent.id,
                    depth=1,
                    hypothesis=Hypothesis(
                        id="child",
                        title="Child",
                        core_hypothesis="child",
                        family="memory_hierarchy",
                    ),
                    node_dir=str(node_dir),
                    sandbox_path=str(self.repo_root),
                    candidate_path=str(self.repo_root / "kernel.py"),
                )
                node_dir.mkdir(parents=True, exist_ok=True)

                result = adapter.execute(task, snapshot, root, parent, node)

                self.assertEqual(result.status, "completed")
                self.assertEqual(result.metadata["probe_status"], "completed")
                self.assertEqual(result.metadata["line_count"], 1)
                self.assertIn("Read", result.summary)

    def test_tree_search_result_is_stable_across_repeated_runs(self) -> None:
        executor_script = self._write_script(
            "search_executor.py",
            """
            import json
            import os
            from pathlib import Path

            payload = json.loads(Path(os.environ["TREE_PAYLOAD_PATH"]).read_text(encoding="utf-8"))
            hypothesis = payload["node"]["hypothesis"]["core_hypothesis"]
            candidate_path = Path(payload["candidate_path"])
            if hypothesis == "fast-branch":
                candidate_path.write_text("fast\\n", encoding="utf-8")
            elif hypothesis == "safe-branch":
                candidate_path.write_text("safe\\n", encoding="utf-8")
            else:
                raise SystemExit(8)
            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "summary": hypothesis,
                        "changed": True,
                        "candidate_path": str(candidate_path),
                    }
                ),
                encoding="utf-8",
            )
            """,
        )

        for iteration in range(5):
            with self.subTest(iteration=iteration):
                run_dir = self.repo_root / f"tree-run-{iteration:02d}"
                task = StableSearchTask(self.repo_root)
                agent = CommandAgentAdapter(
                    self.repo_root,
                    executor_command=self._python_command(executor_script),
                )
                engine = TreeSearchEngine(
                    task=task,
                    agent=agent,
                    config=SearchConfig(
                        beam_width=1,
                        max_rounds=1,
                        children_per_expansion=2,
                    ),
                    run_dir=run_dir,
                )

                result = engine.run()

                self.assertEqual(result.best_node_id, "node-0002")
                self.assertEqual(engine.root_memory.best_score, 3.0)
                self.assertEqual(Path(task.promoted_path).read_text(encoding="utf-8").strip(), "fast")
                self.assertEqual(engine.frontier_node_ids, ["node-0002"])

    def _write_script(self, name: str, body: str) -> Path:
        script = self.repo_root / name
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        return script

    def _python_command(self, script: Path) -> str:
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


if __name__ == "__main__":
    unittest.main()
