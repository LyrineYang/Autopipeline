from __future__ import annotations

import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tree.planner import Planner
from tree.prior import kernel_optimization_prior
from tree.prompts import build_planner_prompt
from tree.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot


class SeedOnlyTask:
    def seed_hypotheses(self, parent, root, limit):
        return [
            Hypothesis(
                id="seed-1",
                title="Seed 1",
                core_hypothesis="seed hypothesis",
            )
        ][:limit]


class ContextTask(SeedOnlyTask):
    def planner_context(self, parent, root, limit, *, upper_bounds=None):
        return {
            "static_prior": {"kernel_type": "matmul", "optimization_scope": "kernel"},
            "optimization_prior": kernel_optimization_prior(),
            "hardware_context": {"gpu_name": "H100"},
            "history_context": {"positive_insights": ["memory branch improved"]},
            "region_graph": {
                "focus_region_id": "current-kernel",
                "regions": [
                    {
                        "id": "current-kernel",
                        "label": "kernel.py",
                        "scope": "kernel",
                        "summary": "Hot matmul region.",
                        "signals": ["profile_bottlenecks=memory_bandwidth_pressure"],
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
            "symptoms": ["profile_bottlenecks=memory_bandwidth_pressure", "gpu=H100"],
            "profile_hints": ["memory_bandwidth_pressure"],
            "problem_summary": "Current target is a hot matmul kernel; planner should infer direction from static facts and hardware context.",
            "branch_cards": [
                {
                    "id": "ctx-1",
                    "title": "Context branch",
                    "core_hypothesis": "context-provided hypothesis",
                    "idea_id": "memory-access",
                    "implementation_variant": "shared-memory-tiling",
                    "family": "memory_hierarchy",
                    "editable_path_hints": ["kernel.py"],
                }
            ],
            "planner_rules": {
                "allowed_scopes": ["kernel"],
                "allowed_families": ["memory_hierarchy", "parallelization"],
                "editable_paths": ["kernel.py"],
                "avoid_families": ["instruction_selection"],
            },
            "frozen_decisions": ["Stay inside kernel.py and freeze the high-level branch identity."],
        }


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.sandbox_dir = Path(self.tempdir.name) / "sandbox"
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        (self.sandbox_dir / "kernel.py").write_text("# sandbox candidate\n", encoding="utf-8")
        self.parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="baseline", title="Baseline", core_hypothesis="baseline"),
            node_dir=str(Path(self.tempdir.name) / "node-0001"),
            sandbox_path=str(self.sandbox_dir),
            candidate_path=str(self.sandbox_dir / "kernel.py"),
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
            objective="test planner",
            target_path="/tmp/kernel.py",
            editable_paths=["kernel.py"],
            constraints=[],
            reference_paths=[],
        )
        self.root = RootMemory(objective="test planner")

    def test_falls_back_to_seed_hypotheses_without_task_context(self) -> None:
        packet = Planner().build_packet(SeedOnlyTask(), self.snapshot, self.root, self.parent, 1)

        self.assertEqual(len(packet.branch_cards), 1)
        self.assertEqual(packet.branch_cards[0].core_hypothesis, "seed hypothesis")
        self.assertEqual(packet.branch_cards[0].family, "unspecified")
        self.assertEqual(packet.static_prior["source"], "seed_hypotheses")
        self.assertIsNotNone(packet.search_policy)

    def test_builds_packet_from_task_facts(self) -> None:
        packet = Planner().build_packet(
            ContextTask(),
            self.snapshot,
            self.root,
            self.parent,
            2,
            upper_bounds={"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertEqual(len(packet.branch_cards), 1)
        self.assertEqual(packet.branch_cards[0].core_hypothesis, "context-provided hypothesis")
        self.assertEqual(packet.branch_cards[0].idea_id, "memory-access")
        self.assertEqual(packet.branch_cards[0].implementation_variant, "shared-memory-tiling")
        self.assertEqual(packet.branch_cards[0].family, "memory_hierarchy")
        self.assertIsNotNone(packet.region_graph)
        self.assertEqual(packet.region_graph.focus_region_id, "current-kernel")
        self.assertEqual(len(packet.region_graph.regions), 3)
        self.assertIsNotNone(packet.bottleneck_card)
        self.assertEqual(packet.bottleneck_card.diagnoses, [])
        self.assertEqual(packet.bottleneck_card.response_families, ["memory_hierarchy"])
        self.assertIsNotNone(packet.search_policy)
        self.assertEqual(packet.search_policy.recommended_scope, "kernel")
        self.assertEqual(packet.search_policy.scope_options, ["kernel"])
        self.assertIsNone(packet.search_policy.branch_budget.children)
        self.assertIsNone(packet.search_policy.branch_budget.beam_width)
        self.assertIn("high-level branch identity", " ".join(packet.frozen_decisions).lower())
        self.assertEqual(packet.branch_cards[0].core_hypothesis, "context-provided hypothesis")

    def test_same_family_variants_are_preserved(self) -> None:
        class SameFamilyTask(ContextTask):
            def planner_context(self, parent, root, limit, *, upper_bounds=None):
                context = super().planner_context(parent, root, limit, upper_bounds=upper_bounds)
                context["branch_cards"] = [
                    {
                        "id": "ctx-a",
                        "title": "Variant A",
                        "core_hypothesis": "exploit variant a",
                        "family": "memory_hierarchy",
                        "editable_path_hints": ["kernel.py"],
                    },
                    {
                        "id": "ctx-b",
                        "title": "Variant B",
                        "core_hypothesis": "exploit variant b",
                        "family": "memory_hierarchy",
                        "editable_path_hints": ["kernel.py"],
                    },
                ]
                return context

        packet = Planner().build_packet(SameFamilyTask(), self.snapshot, self.root, self.parent, 2)

        self.assertEqual(len(packet.branch_cards), 2)
        self.assertEqual([card.family for card in packet.branch_cards], ["memory_hierarchy", "memory_hierarchy"])

    def test_same_idea_can_emit_multiple_implementation_variants(self) -> None:
        class SameIdeaTask(ContextTask):
            def planner_context(self, parent, root, limit, *, upper_bounds=None):
                context = super().planner_context(parent, root, limit, upper_bounds=upper_bounds)
                context["planner_rules"]["allowed_families"] = ["memory_access"]
                context["branch_cards"] = [
                    {
                        "id": "tile-a",
                        "title": "Tile A",
                        "core_hypothesis": "Improve memory locality for the hot path.",
                        "idea_id": "memory-access",
                        "implementation_variant": "shared-memory-tiling",
                        "family": "memory_access",
                        "editable_path_hints": ["kernel.py"],
                    },
                    {
                        "id": "tile-b",
                        "title": "Tile B",
                        "core_hypothesis": "Improve memory locality for the hot path.",
                        "idea_id": "memory-access",
                        "implementation_variant": "vectorized-loads",
                        "family": "memory_access",
                        "editable_path_hints": ["kernel.py"],
                    },
                ]
                return context

        packet = Planner().build_packet(SameIdeaTask(), self.snapshot, self.root, self.parent, 2)

        self.assertEqual([card.idea_id for card in packet.branch_cards], ["memory-access", "memory-access"])
        self.assertEqual(
            [card.implementation_variant for card in packet.branch_cards],
            ["shared-memory-tiling", "vectorized-loads"],
        )

    def test_planner_handoff_modes_control_executor_detail(self) -> None:
        class HandoffTask(ContextTask):
            def planner_context(self, parent, root, limit, *, upper_bounds=None):
                context = super().planner_context(parent, root, limit, upper_bounds=upper_bounds)
                context["planner_rules"]["allowed_families"] = ["memory_access"]
                context["branch_cards"] = [
                    {
                        "id": "memory-a",
                        "title": "Memory idea",
                        "core_hypothesis": "Improve memory locality for the hot path.",
                        "idea_id": "memory-access",
                        "implementation_variant": "shared-memory-tiling",
                        "implementation_steps": ["inspect stride pattern", "stage reused values"],
                        "family": "memory_access",
                        "editable_path_hints": ["kernel.py"],
                    }
                ]
                return context

        expected = {
            "idea": ("", []),
            "variant": ("shared-memory-tiling", []),
            "detailed": ("shared-memory-tiling", ["inspect stride pattern", "stage reused values"]),
        }
        for mode, (variant, steps) in expected.items():
            with self.subTest(mode=mode):
                packet = Planner(planner_handoff_mode=mode).build_packet(
                    HandoffTask(),
                    self.snapshot,
                    self.root,
                    self.parent,
                    1,
                )

                self.assertEqual(packet.branch_cards[0].implementation_variant, variant)
                self.assertEqual(packet.branch_cards[0].implementation_steps, steps)

    def test_planner_command_can_override_planner_packet(self) -> None:
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
                                "symptoms": ["profile_bottlenecks=memory_bandwidth_pressure"],
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

        packet = Planner(
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
        logs_dir = Path(self.parent.node_dir) / "agent" / "planner"
        self.assertTrue((logs_dir / "prompt.txt").exists())
        self.assertTrue((logs_dir / "payload.json").exists())
        self.assertTrue((logs_dir / "telemetry.json").exists())
        self.assertTrue((logs_dir / "output.json").exists())

    def test_planner_sensing_uses_parent_sandbox_workspace(self) -> None:
        script = Path(self.tempdir.name) / "planner_sensing_cwd.py"
        script.write_text(
            textwrap.dedent(
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
                        sensing = json.loads(Path(os.environ["TREE_SENSING_PATH"]).read_text(encoding="utf-8"))
                        passive = json.loads(Path(sensing["passive_snapshot_path"]).read_text(encoding="utf-8"))
                        Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                            json.dumps({"branch_cards": [], "frozen_decisions": [passive["cwd"]]}),
                            encoding="utf-8",
                        )
                        break
                    time.sleep(0.05)
                else:
                    raise SystemExit(9)
                """
            ),
            encoding="utf-8",
        )

        packet = Planner(
            planner_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        ).build_packet(
            ContextTask(),
            self.snapshot,
            self.root,
            self.parent,
            1,
            upper_bounds={"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertEqual(packet.frozen_decisions, [str(self.sandbox_dir)])

    def test_planner_output_is_clamped_to_task_families_and_scopes(self) -> None:
        script = Path(self.tempdir.name) / "planner_invalid.py"
        script.write_text(
            textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["TREE_OUTPUT_PATH"]).write_text(
                    json.dumps(
                        {
                            "search_policy": {
                                "recommended_scope": "pipeline",
                                "scope_options": ["kernel", "pipeline"],
                                "branch_budget": {"children": 2, "beam_width": 2, "rounds": 2},
                            },
                            "branch_cards": [
                                {
                                    "id": "bad-1",
                                    "title": "Bad branch",
                                    "core_hypothesis": "illegal fusion branch",
                                    "family": "fusion",
                                    "editable_path_hints": ["pipeline.py"],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ),
            encoding="utf-8",
        )

        packet = Planner(
            planner_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        ).build_packet(
            ContextTask(),
            self.snapshot,
            self.root,
            self.parent,
            2,
            upper_bounds={"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertEqual(packet.branch_cards[0].family, "unspecified")
        self.assertEqual(packet.search_policy.recommended_scope, "kernel")
        self.assertEqual(packet.search_policy.scope_options, ["kernel"])

    def test_planner_prompt_summarizes_problem_and_rules(self) -> None:
        raw_context = ContextTask().planner_context(self.parent, self.root, 2)
        prompt = build_planner_prompt(
            self.snapshot,
            self.root,
            self.parent,
            2,
            raw_context,
            {"beam_width": 3, "children_per_expansion": 2, "max_rounds": 4},
        )

        self.assertIn("Allowed scopes: kernel", prompt)
        self.assertIn("Optimization prior families: memory_access", prompt)
        self.assertIn("Problem summary: Current target is a hot matmul kernel", prompt)


if __name__ == "__main__":
    unittest.main()
