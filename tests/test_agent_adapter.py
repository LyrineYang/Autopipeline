from __future__ import annotations

import json
import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tree.agent import AgentStepError, CommandAgentAdapter
from tree.prompts import build_execution_payload, build_execution_prompt
from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot
from tree.planner import PlannerPacket


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


class ManagedRuntimeTask(AdapterTask):
    def build_python_probe_command(self, *, cwd, script_path, args):
        return [sys.executable, str(script_path), *args]

    def probe_environment(self, *, cwd):
        return {"PROBE_RUNTIME": "managed"}

    def build_theoretical_limit_inputs(self, *, cwd, node, parent):
        return {
            "hardware": {"peak_compute_tflops": 1000.0, "peak_bandwidth_gbps": 3200.0},
            "workload": {"shape": {"M": 128, "N": 128, "K": 128}},
        }


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

    def test_propose_uses_planner_branch_cards(self) -> None:
        adapter = CommandAgentAdapter(self.repo_root)
        packet = PlannerPacket(
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
            planner_packet=packet,
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
            sensing = json.loads(Path(os.environ["TREE_SENSING_PATH"]).read_text(encoding="utf-8"))
            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps(
                    [
                        {
                            "kind": "read_text",
                            "path": "kernel.py",
                            "mode": "tail",
                            "lines": 5,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "summary": prompt.splitlines()[0],
                        "changed": True,
                        "candidate_path": payload["candidate_path"],
                        "metadata": {"action": telemetry["action"], "sensing": sensing["action"]},
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
        self.assertEqual(result.metadata["sensing"], "execute")
        logs_dir = Path(self.node.node_dir) / "agent" / "execute"
        self.assertTrue((logs_dir / "prompt.txt").exists())
        self.assertTrue((logs_dir / "payload.json").exists())
        self.assertTrue((logs_dir / "telemetry.json").exists())
        self.assertTrue((logs_dir / "output.json").exists())
        self.assertTrue((logs_dir / "command_status.json").exists())
        self.assertTrue((logs_dir / "sensing.json").exists())
        sensing_manifest = json.loads((logs_dir / "sensing.json").read_text(encoding="utf-8"))
        self.assertIn("verifier", sensing_manifest)
        self.assertIn("recent_probe_results", sensing_manifest)
        self.assertEqual(sensing_manifest["verifier"]["status"], "healthy")
        self.assertEqual(sensing_manifest["verifier"]["verdict"], "need_more_evidence")
        self.assertIn("evidence", sensing_manifest["verifier"])
        self.assertIn("regressions", sensing_manifest["verifier"])
        self.assertIn("uncertainty", sensing_manifest["verifier"])
        probe_results = json.loads((logs_dir / "probe_results.json").read_text(encoding="utf-8"))
        self.assertEqual(probe_results[0]["kind"], "read_text")
        self.assertEqual(probe_results[0]["status"], "completed")
        self.assertIn("kernel", "\n".join(probe_results[0]["data"]["lines"]))

    def test_execute_can_consume_probe_results_in_same_step(self) -> None:
        script = self._write_script(
            "executor_same_step_probe.py",
            """
            import json
            import os
            import time
            from pathlib import Path

            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps(
                    [
                        {
                            "kind": "read_text",
                            "path": "kernel.py",
                            "mode": "tail",
                            "lines": 5,
                        }
                    ]
                ),
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
                                "metadata": {"probe_status": results[0]["status"]},
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
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        result = adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metadata["probe_status"], "completed")
        self.assertIn("Read", result.summary)

    def test_execute_can_consume_generic_python_probe_in_same_step(self) -> None:
        script = self._write_script(
            "executor_same_step_python_probe.py",
            """
            import json
            import os
            import time
            from pathlib import Path

            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps(
                    [
                        {
                            "kind": "python",
                            "code": "print(6 * 7)",
                        }
                    ]
                ),
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
                                "summary": results[0]["data"]["stdout"].strip(),
                                "changed": False,
                                "metadata": {"probe_kind": results[0]["kind"]},
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
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        result = adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "42")
        self.assertEqual(result.metadata["probe_kind"], "python")

    def test_execute_generic_python_probe_uses_task_runtime_hook(self) -> None:
        script = self._write_script(
            "executor_python_probe_runtime.py",
            """
            import json
            import os
            import time
            from pathlib import Path

            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps(
                    [
                        {
                            "kind": "python",
                            "code": "import os; print(os.environ.get('PROBE_RUNTIME', 'missing'))",
                        }
                    ]
                ),
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
                                "summary": results[0]["data"]["stdout"].strip(),
                                "changed": False,
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
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        result = adapter.execute(ManagedRuntimeTask(), self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "managed")

    def test_execute_can_request_theoretical_limit_inputs(self) -> None:
        script = self._write_script(
            "executor_theoretical_limit_inputs.py",
            """
            import json
            import os
            import time
            from pathlib import Path

            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps([{"kind": "theoretical_limit_inputs"}]),
                encoding="utf-8",
            )
            results_path = Path(os.environ["TREE_PROBE_RESULTS_PATH"])
            for _ in range(40):
                try:
                    results = json.loads(results_path.read_text(encoding="utf-8"))
                except Exception:
                    results = []
                if results:
                    payload = results[0]["data"]["task_inputs"]
                    Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": results[0]["summary"],
                                "changed": False,
                                "metadata": {
                                    "peak_compute_tflops": payload["hardware"]["peak_compute_tflops"],
                                    "shape_m": payload["workload"]["shape"]["M"],
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
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
        )

        result = adapter.execute(ManagedRuntimeTask(), self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metadata["peak_compute_tflops"], 1000.0)
        self.assertEqual(result.metadata["shape_m"], 128)

    def test_execute_clears_stale_probe_requests_between_runs(self) -> None:
        first_script = self._write_script(
            "executor_probe_once.py",
            """
            import json
            import os
            from pathlib import Path

            Path(os.environ["TREE_PROBE_REQUESTS_PATH"]).write_text(
                json.dumps([{"kind": "read_text", "path": "kernel.py", "lines": 1}]),
                encoding="utf-8",
            )
            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                json.dumps({"status": "completed", "summary": "first", "changed": False}),
                encoding="utf-8",
            )
            """,
        )
        second_script = self._write_script(
            "executor_no_probe.py",
            """
            import json
            import os
            from pathlib import Path

            Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                json.dumps({"status": "completed", "summary": "second", "changed": False}),
                encoding="utf-8",
            )
            """,
        )
        first_adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(first_script),
        )
        second_adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(second_script),
        )

        first_adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)
        second_adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        logs_dir = Path(self.node.node_dir) / "agent" / "execute"
        probe_results = json.loads((logs_dir / "probe_results.json").read_text(encoding="utf-8"))
        self.assertEqual(probe_results, [])

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

    def test_execution_prompt_and_payload_include_planner_freeze(self) -> None:
        self.node.planner_context = {
            "recommended_scope": "kernel",
            "effective_scope": "pipeline",
            "effective_editable_paths": ["kernel.py"],
            "focus_target": "attention block",
            "scope_summary": "Pipeline-scope branch focused on attention block.",
            "scope_context_files": ["pipeline_context.json", "pipeline_context.md"],
            "stop_conditions": ["stop after one accepted improvement"],
            "frozen_decisions": ["keep the branch inside memory hierarchy"],
            "local_iteration": 2,
            "local_iteration_limit": 3,
            "current_branch_best_score": 1.5,
            "local_history": [{"iteration": 1, "score": 1.5}],
        }

        payload = build_execution_payload(self.snapshot, self.root, self.parent, self.node)
        prompt = build_execution_prompt(self.snapshot, self.root, self.parent, self.node)

        self.assertEqual(payload["planner_context"]["recommended_scope"], "kernel")
        self.assertEqual(payload["effective_scope"], "pipeline")
        self.assertEqual(payload["task"]["editable_paths"], ["kernel.py"])
        self.assertEqual(payload["scope_context_files"], ["pipeline_context.json", "pipeline_context.md"])
        self.assertEqual(payload["local_iteration"], 2)
        self.assertEqual(payload["current_branch_best_score"], 1.5)
        self.assertIn("Effective scope: pipeline", prompt)
        self.assertIn("Focus target: attention block", prompt)
        self.assertIn("pipeline_context.json", prompt)
        self.assertIn("stop after one accepted improvement", prompt)
        self.assertIn("keep the branch inside memory hierarchy", prompt)
        self.assertIn("Local iteration: 2 / 3", prompt)

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

    def test_command_timeout_stops_executor_and_records_status(self) -> None:
        script = self._write_script(
            "slow_executor.py",
            """
            import time
            time.sleep(5)
            """,
        )
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
            command_timeout_seconds=0.2,
        )

        with self.assertRaisesRegex(AgentStepError, "timed out"):
            adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        status = json.loads(
            ((Path(self.node.node_dir) / "agent" / "execute" / "command_status.json").read_text(encoding="utf-8"))
        )
        self.assertTrue(status["timed_out"])
        self.assertEqual(status["status"], "timed_out")

    def test_command_cancel_file_stops_executor_and_records_status(self) -> None:
        script = self._write_script(
            "cancel_executor.py",
            """
            import os
            import time
            from pathlib import Path

            Path(os.environ["TREE_CANCEL_PATH"]).write_text("stop", encoding="utf-8")
            time.sleep(5)
            """,
        )
        adapter = CommandAgentAdapter(
            self.repo_root,
            executor_command=self._python_command(script),
            command_timeout_seconds=5,
        )

        with self.assertRaisesRegex(AgentStepError, "cancelled"):
            adapter.execute(self.task, self.snapshot, self.root, self.parent, self.node)

        status = json.loads(
            ((Path(self.node.node_dir) / "agent" / "execute" / "command_status.json").read_text(encoding="utf-8"))
        )
        self.assertTrue(status["cancelled"])
        self.assertEqual(status["status"], "cancelled")

    def _write_script(self, name: str, source: str) -> Path:
        path = self.repo_root / name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def _python_command(self, script: Path) -> str:
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


if __name__ == "__main__":
    unittest.main()
