from __future__ import annotations

import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tree.prompts import build_watchman_prompt
from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot
from tree.watchman import Watchman


class SeedOnlyTask:
    def seed_hypotheses(self, parent, root, limit):
        return [
            Hypothesis(
                id="seed-1",
                title="Seed 1",
                core_hypothesis="seed hypothesis",
                family="seed_family",
            )
        ][:limit]


class ContextTask(SeedOnlyTask):
    def watchman_context(self, parent, root, limit, *, upper_bounds=None):
        return {
            "static_prior": {"kernel_type": "matmul", "optimization_scope": "kernel"},
            "hardware_context": {"gpu_name": "H100"},
            "benchmark_context": {"metrics": {"bottleneck": "memory"}},
            "history_context": {"positive_insights": ["memory branch improved"]},
            "region_graph": {
                "focus_region_id": "current-kernel",
                "regions": [
                    {
                        "id": "current-kernel",
                        "label": "kernel.py",
                        "scope": "kernel",
                        "summary": "Hot matmul region.",
                        "signals": ["local_bottleneck=memory"],
                        "adjacent": ["upstream-1", "downstream-1"],
                    },
                    {
                        "id": "upstream-1",
                        "label": "qkv projection",
                        "scope": "region",
                        "summary": "Feeds the current hot region.",
                        "signals": ["layout_churn"],
                        "adjacent": ["current-kernel"],
                    },
                    {
                        "id": "downstream-1",
                        "label": "attention output",
                        "scope": "region",
                        "summary": "Consumes the hot region output.",
                        "signals": ["launch_overhead"],
                        "adjacent": ["current-kernel"],
                    },
                ],
            },
            "symptoms": ["local_bottleneck=memory", "gpu=H100"],
            "profile_hints": ["memory_bandwidth_pressure"],
            "problem_summary": "Current target is memory-bound and stays inside one kernel.",
            "planner_rules": {
                "allowed_scopes": ["kernel"],
                "allowed_families": ["memory_hierarchy", "parallelization"],
                "editable_paths": ["kernel.py"],
                "avoid_families": ["instruction_selection"],
            },
            "frozen_decisions": ["Stay inside kernel.py and freeze the high-level branch identity."],
        }


class WatchmanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(Path(self.tempdir.name) / "node-0001"),
            evaluation_result=EvaluationResult(
                accepted=True,
                score=1.0,
                metrics={"returncode": 0},
                summary="baseline",
            ),
        )
        Path(self.parent.node_dir).mkdir(parents=True, exist_ok=True)
        self.snapshot = TaskSnapshot(
            task_type="fake",
            objective="test watchman",
            target_path="/tmp/kernel.py",
            editable_paths=["kernel.py"],
            constraints=[],
            reference_paths=[],
        )
        self.root = RootMemory(objective="test watchman")

    def test_falls_back_to_seed_hypotheses_without_task_context(self) -> None:
        packet = Watchman().build_packet(SeedOnlyTask(), self.snapshot, self.root, self.parent, 1)

        self.assertEqual(len(packet.branch_cards), 1)
        self.assertEqual(packet.branch_cards[0].core_hypothesis, "seed hypothesis")
        self.assertEqual(packet.branch_cards[0].family, "seed_family")
        self.assertEqual(packet.static_prior["source"], "seed_hypotheses")
        self.assertIsNotNone(packet.search_policy)

    def test_builds_packet_from_task_facts(self) -> None:
        packet = Watchman().build_packet(
            ContextTask(),
            self.snapshot,
            self.root,
            self.parent,
            2,
            upper_bounds={"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertEqual(len(packet.branch_cards), 1)
        self.assertEqual(packet.branch_cards[0].family, "seed_family")
        self.assertIsNotNone(packet.region_graph)
        self.assertEqual(packet.region_graph.focus_region_id, "current-kernel")
        self.assertEqual(len(packet.region_graph.regions), 3)
        self.assertIsNotNone(packet.bottleneck_card)
        self.assertEqual(packet.bottleneck_card.diagnoses, ["memory_bandwidth_pressure"])
        self.assertEqual(packet.bottleneck_card.response_families, ["seed_family"])
        self.assertIsNotNone(packet.search_policy)
        self.assertEqual(packet.search_policy.recommended_scope, "kernel")
        self.assertEqual(packet.search_policy.scope_options, ["kernel"])
        self.assertEqual(packet.search_policy.branch_budget.children, 1)
        self.assertEqual(packet.search_policy.branch_budget.beam_width, 3)
        self.assertIn("high-level branch identity", " ".join(packet.frozen_decisions).lower())

    def test_planner_command_can_override_watchman_packet(self) -> None:
        script = Path(self.tempdir.name) / "planner.py"
        script.write_text(
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                payload = json.loads(Path(os.environ["TREE_PAYLOAD_PATH"]).read_text(encoding="utf-8"))
                telemetry = json.loads(Path(os.environ["TREE_TELEMETRY_PATH"]).read_text(encoding="utf-8"))
                prompt = Path(os.environ["TREE_PROMPT_PATH"]).read_text(encoding="utf-8")
                Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                    json.dumps(
                        {
                            "bottleneck_card": {
                                "scope": "kernel",
                                "target": payload["task"]["target_path"],
                                "symptoms": ["local_bottleneck=memory"],
                                "diagnoses": ["memory_bandwidth_pressure"],
                                "response_families": ["memory_hierarchy"],
                                "avoid_families": [],
                                "rationale": telemetry["facts"]["problem_summary"],
                            },
                            "search_policy": {
                                "recommended_scope": "kernel",
                                "scope_options": ["kernel"],
                                "exploration_bias": "exploit",
                                "branch_budget": {"children": 1, "beam_width": 2, "rounds": 1},
                                "resource_hint": {"tier": "lean", "notes": [prompt.splitlines()[0]]},
                                "stop_conditions": ["stop after one accepted improvement"],
                            },
                            "branch_cards": [
                                {
                                    "id": "planner-1",
                                    "title": "Planner branch",
                                    "core_hypothesis": "planner hypothesis",
                                    "constraints_summary": "keep it local",
                                    "editable_path_hints": ["kernel.py"],
                                    "family": "memory_hierarchy",
                                    "rationale": "planner chose memory hierarchy",
                                    "target_signals": ["bandwidth_gb_s"],
                                    "supporting_evidence": ["bottleneck=memory"],
                                    "disallowed_directions": ["do not pivot to tensorization"],
                                }
                            ],
                            "frozen_decisions": ["freeze to one memory branch"],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ),
            encoding="utf-8",
        )

        packet = Watchman(
            planner_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        ).build_packet(
            ContextTask(),
            self.snapshot,
            self.root,
            self.parent,
            2,
            upper_bounds={"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertEqual(packet.branch_cards[0].core_hypothesis, "planner hypothesis")
        self.assertEqual(packet.search_policy.branch_budget.beam_width, 2)
        self.assertEqual(packet.frozen_decisions, ["freeze to one memory branch"])
        logs_dir = Path(self.parent.node_dir) / "agent" / "watchman"
        self.assertTrue((logs_dir / "prompt.txt").exists())
        self.assertTrue((logs_dir / "payload.json").exists())
        self.assertTrue((logs_dir / "telemetry.json").exists())
        self.assertTrue((logs_dir / "output.json").exists())

    def test_watchman_prompt_summarizes_problem_and_rules(self) -> None:
        raw_context = ContextTask().watchman_context(self.parent, self.root, 2)
        prompt = build_watchman_prompt(
            self.snapshot,
            self.root,
            self.parent,
            2,
            raw_context,
            {"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertIn("Allowed scopes: kernel", prompt)
        self.assertIn("Problem summary: Current target is memory-bound", prompt)


if __name__ == "__main__":
    unittest.main()
