from __future__ import annotations

import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tree.agent import AgentStepError, CommandAgentAdapter
from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot
from tree.watchman import WatchmanPacket


class AdapterTask:
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


class CommandAgentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo_root = Path(self.tempdir.name)
        self.task = AdapterTask()
        self.snapshot = TaskSnapshot(
            task_type="fake",
            objective="test adapter",
            target_path=str(self.repo_root / "kernel.py"),
            editable_paths=["kernel.py"],
            constraints=["do not edit control files"],
            reference_paths=[],
        )
        self.root = RootMemory(objective="test adapter")
        self.parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(self.repo_root / "node-0001"),
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
        self.node = SearchNode(
            id="node-0002",
            parent_id="node-0001",
            depth=1,
            hypothesis=Hypothesis(id="child", title="Child", core_hypothesis="child", family="memory_hierarchy"),
            node_dir=str(self.repo_root / "node-0002"),
            sandbox_path=str(self.repo_root / "sandbox"),
            candidate_path=str(self.repo_root / "sandbox" / "kernel.py"),
        )
        Path(self.parent.node_dir).mkdir(parents=True, exist_ok=True)
        Path(self.node.node_dir).mkdir(parents=True, exist_ok=True)
        Path(self.node.sandbox_path).mkdir(parents=True, exist_ok=True)
        Path(self.node.candidate_path).write_text("kernel", encoding="utf-8")

    def test_propose_uses_watchman_branch_cards(self) -> None:
        adapter = CommandAgentAdapter(self.repo_root)
        packet = WatchmanPacket(
            branch_cards=[
                Hypothesis(
                    id="planner-1",
                    title="Planner branch",
                    core_hypothesis="planner hypothesis",
                    family="memory_hierarchy",
                )
            ]
        )

        hypotheses = adapter.propose(
            self.task,
            self.snapshot,
            self.root,
            self.parent,
            1,
            watchman_packet=packet,
        )

        self.assertEqual(len(hypotheses), 1)
        self.assertEqual(hypotheses[0].core_hypothesis, "planner hypothesis")

    def test_missing_commands_fall_back_to_completed_execution(self) -> None:
        adapter = CommandAgentAdapter(self.repo_root)
        execution = adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(execution.status, "completed")

    def test_execute_reads_json_output_and_receives_env_paths(self) -> None:
        script = self._write_script(
            "executor.py",
            """
            import json
            import os
            from pathlib import Path

            prompt = Path(os.environ["TREE_PROMPT_PATH"]).read_text(encoding="utf-8")
            payload = json.loads(Path(os.environ["TREE_PAYLOAD_PATH"]).read_text(encoding="utf-8"))
            telemetry = json.loads(Path(os.environ["TREE_TELEMETRY_PATH"]).read_text(encoding="utf-8"))
            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "summary": prompt.splitlines()[0],
                        "changed": True,
                        "candidate_path": payload["candidate_path"],
                        "metadata": {"action": telemetry["action"]},
                    }
                ),
                encoding="utf-8",
            )
            """,
        )
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        result = adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metadata["action"], "execute")
        logs_dir = Path(self.node.node_dir) / "agent" / "execute"
        self.assertTrue((logs_dir / "prompt.txt").exists())
        self.assertTrue((logs_dir / "payload.json").exists())
        self.assertTrue((logs_dir / "telemetry.json").exists())
        self.assertTrue((logs_dir / "output.json").exists())

    def test_reflect_reads_json_output_and_receives_env_paths(self) -> None:
        script = self._write_script(
            "reflector.py",
            """
            import json
            import os
            from pathlib import Path

            telemetry = json.loads(Path(os.environ["TREE_TELEMETRY_PATH"]).read_text(encoding="utf-8"))
            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                json.dumps(
                    {
                        "polarity": "positive",
                        "summary": telemetry["action"],
                        "evidence": ["e1", "e2"],
                    }
                ),
                encoding="utf-8",
            )
            """,
        )
        adapter = CommandAgentAdapter(
            self.repo_root,
            reflector_command=self._python_command(script),
        )
        self.node.evaluation_result = EvaluationResult(
            accepted=True,
            score=2.0,
            metrics={"returncode": 0},
            summary="candidate",
        )

        insight = adapter.reflect(self.task, self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(insight.polarity, "positive")
        self.assertEqual(insight.summary, "reflect")
        self.assertEqual(insight.evidence, ["e1", "e2"])

    def test_invalid_json_raises_agent_step_error(self) -> None:
        script = self._write_script(
            "invalid_json.py",
            """
            import os
            from pathlib import Path

            Path(os.environ["TREE_OUTPUT_PATH"]).write_text("{not-json", encoding="utf-8")
            """,
        )
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        with self.assertRaises(AgentStepError):
            adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

    def test_missing_output_raises_agent_step_error(self) -> None:
        script = self._write_script("missing_output.py", "pass\n")
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        with self.assertRaises(AgentStepError):
            adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

    def test_nonzero_returncode_raises_agent_step_error(self) -> None:
        script = self._write_script(
            "fail.py",
            """
            import sys
            sys.exit(3)
            """,
        )
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        with self.assertRaises(AgentStepError):
            adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

    def _write_script(self, name: str, source: str) -> Path:
        path = self.repo_root / name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def _python_command(self, script: Path) -> str:
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


if __name__ == "__main__":
    unittest.main()
