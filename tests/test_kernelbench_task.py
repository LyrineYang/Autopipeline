from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from kernelbench.bridge import KernelBenchProblem
from kernelbench.task import KernelBenchTask, parse_kernelbench_output
from autopipeline.search.agent import CommandAgentAdapter
from autopipeline.search.engine import TreeSearchEngine
from autopipeline.search.types import Hypothesis, RootMemory, SearchConfig, SearchNode


class KernelBenchTaskTests(unittest.TestCase):
    def test_parse_kernelbench_summary_output(self) -> None:
        metrics = parse_kernelbench_output(
            """
            correctness: PASS
            speedup: 1.250x
            kernel_time_ms: 0.4000
            reference_time_ms: 0.5000
            fast_1.0: PASS
            fast_1.5: FAIL
            """
        )

        self.assertEqual(metrics["correctness"], "PASS")
        self.assertAlmostEqual(metrics["speedup"], 1.25)
        self.assertTrue(metrics["fast_1.0"])
        self.assertFalse(metrics["fast_1.5"])

    def test_kernelbench_task_evaluates_fixture_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path.cwd()
            task = KernelBenchTask.from_problem(
                repo_root=repo_root,
                problem=_fixture_problem(tmp),
                bench_args=_quick_correctness_args(),
                timeout_seconds=120,
                bench_script_path=_fake_bench_script(Path(tmp)),
            )
            parent = SearchNode(
                id="node-0000",
                parent_id=None,
                depth=0,
                hypothesis=Hypothesis(id="root", title="Root", core_hypothesis="root"),
            )
            root = RootMemory(objective="kernelbench")
            hypotheses = task.seed_hypotheses(parent, root, 3)
            context = task.planner_context(parent, root, 3)

            self.assertEqual(hypotheses[0].family, "memory_access")
            self.assertEqual(hypotheses[0].idea_id, "memory-access")
            self.assertIn("memory_access", context["planner_rules"]["allowed_families"])
            self.assertIn("optimization_prior", context)
            run_dir = Path(tmp) / "run"
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

            self.assertTrue(result.accepted, result.summary)
            self.assertEqual(result.metrics["correctness"], "PASS")
            self.assertTrue((Path(node.sandbox_path) / "kernel.py").exists())
            self.assertTrue((Path(node.sandbox_path) / "workspace" / "kb_active" / "reference.py").exists())

    def test_kernelbench_tree_runs_with_codex_wrapper_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path.cwd()
            task = KernelBenchTask.from_problem(
                repo_root=repo_root,
                problem=_fixture_problem(tmp),
                bench_args=_quick_correctness_args(),
                timeout_seconds=120,
                bench_script_path=_fake_bench_script(Path(tmp)),
            )
            run_dir = Path(tmp) / "tree"
            wrapper = repo_root / "scripts" / "codex_tree_agent.py"
            executor_command = (
                "CODEX_TREE_DRY_RUN=1 "
                f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))} execute"
            )
            engine = TreeSearchEngine(
                task=task,
                agent=CommandAgentAdapter(
                    repo_root,
                    executor_command=executor_command,
                    command_timeout_seconds=120,
                ),
                config=SearchConfig(
                    beam_width=1,
                    max_rounds=1,
                    children_per_expansion=1,
                    executor_command=executor_command,
                    command_timeout_seconds=120,
                ),
                run_dir=run_dir,
            )

            result = engine.run()
            state = json.loads((Path(result.run_dir) / "search_state.json").read_text(encoding="utf-8"))

            self.assertEqual(result.total_nodes, 2)
            self.assertIsNotNone(result.promoted_path)
            self.assertTrue(Path(result.promoted_path).exists())
            self.assertEqual(state["nodes"]["node-0001"]["evaluation_result"]["metrics"]["correctness"], "PASS")
            self.assertEqual(state["nodes"]["node-0002"]["execution_result"]["metadata"]["codex_wrapper"], "dry_run")


def _fixture_problem(tmp: str) -> KernelBenchProblem:
    return KernelBenchProblem(
        level=1,
        problem_id=999,
        name="fixture_identity_add",
        source_code="""
import torch
import torch.nn as nn


class Model(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x + 1


def get_inputs():
    return [torch.ones(4)]


def get_init_inputs():
    return []
""",
    )


def _quick_correctness_args() -> list[str]:
    return [
        "--correctness-only",
        "--n-trials",
        "1",
        "--skip-stability",
        "--skip-determinism",
    ]


def _fake_bench_script(root: Path) -> Path:
    path = root / "fake_bench_kb.py"
    path.write_text(
        """
from __future__ import annotations

import json
from pathlib import Path

result = {
    "correctness": "PASS",
    "speedup": 1.0,
    "kernel_time_ms": 1.0,
    "reference_time_ms": 1.0,
}
active = Path("workspace/kb_active")
active.mkdir(parents=True, exist_ok=True)
(active / "results.json").write_text(json.dumps([result], indent=2), encoding="utf-8")
print("SUMMARY")
print("correctness: PASS")
print("speedup: 1.000x")
print("kernel_time_ms: 1.0000")
print("reference_time_ms: 1.0000")
print("fast_1.0: PASS")
print("fast_1.5: FAIL")
""",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
