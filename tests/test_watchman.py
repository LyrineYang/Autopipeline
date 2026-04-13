from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tree.prompts import build_proposal_prompt
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
    def watchman_context(self, parent, root, limit):
        return {
            "static_prior": {"kernel_type": "matmul"},
            "hardware_context": {"gpu_name": "H100"},
            "benchmark_context": {"metrics": {"bottleneck": "memory"}},
            "history_context": {"positive_insights": ["memory branch improved"]},
            "region_graph": {
                "focus_region_id": "current-kernel",
                "regions": [
                    {
                        "id": "current-kernel",
                        "label": "kernel.py",
                        "scope": "region",
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
                    }
                ],
            },
            "bottleneck_card": {
                "scope": "pipeline",
                "target": "kernel.py",
                "symptoms": ["local_bottleneck=memory"],
                "diagnoses": ["memory_bandwidth_pressure", "kernel_boundary_overhead"],
                "response_families": ["memory_hierarchy", "fusion"],
                "avoid_families": ["instruction_selection"],
                "rationale": "Local evidence points to memory pressure first.",
            },
            "search_policy": {
                "recommended_scope": "pipeline",
                "scope_options": ["kernel", "region", "pipeline"],
                "exploration_bias": "explore",
                "branch_budget": {
                    "children": 2,
                    "rounds": 3,
                    "beam_width": 2,
                },
                "resource_hint": {
                    "tier": "generous",
                    "notes": ["Preserve headroom while diagnosis is uncertain."],
                },
                "stop_conditions": ["stop when one family clearly wins"],
            },
            "branch_families": [
                {
                    "id": "memory",
                    "family": "memory_hierarchy",
                    "title": "Memory branch",
                    "core_hypothesis": "Improve memory traffic",
                    "constraints_summary": "Memory changes only",
                    "rationale": "Parent is memory-bound",
                    "target_signals": ["bandwidth_gb_s"],
                    "supporting_evidence": ["bottleneck=memory"],
                    "disallowed_directions": ["Do not switch to tensorization."],
                },
                {
                    "id": "parallel",
                    "family": "parallelization",
                    "title": "Parallel branch",
                    "core_hypothesis": "Retune tiles",
                    "constraints_summary": "Parallelization only",
                },
            ],
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
        self.assertEqual(packet.static_prior["source"], "seed_hypotheses")

    def test_builds_branch_cards_from_watchman_context(self) -> None:
        packet = Watchman().build_packet(ContextTask(), self.snapshot, self.root, self.parent, 2)

        self.assertEqual(len(packet.branch_cards), 2)
        self.assertEqual(packet.branch_cards[0].family, "memory_hierarchy")
        self.assertEqual(packet.branch_cards[0].target_signals, ["bandwidth_gb_s"])
        self.assertIsNotNone(packet.region_graph)
        self.assertEqual(packet.region_graph.focus_region_id, "current-kernel")
        self.assertEqual(len(packet.region_graph.regions), 3)
        self.assertEqual(packet.region_graph.regions[0].label, "kernel.py")
        self.assertIsNotNone(packet.bottleneck_card)
        self.assertEqual(packet.bottleneck_card.scope, "pipeline")
        self.assertEqual(packet.bottleneck_card.diagnoses, ["memory_bandwidth_pressure", "kernel_boundary_overhead"])
        self.assertEqual(packet.bottleneck_card.response_families[0], "memory_hierarchy")
        self.assertIsNotNone(packet.search_policy)
        self.assertEqual(packet.search_policy.recommended_scope, "pipeline")
        self.assertEqual(packet.search_policy.scope_options, ["kernel", "region", "pipeline"])
        self.assertEqual(packet.search_policy.exploration_bias, "explore")
        self.assertEqual(packet.search_policy.branch_budget.children, 2)
        self.assertEqual(packet.search_policy.branch_budget.beam_width, 2)
        self.assertEqual(packet.search_policy.branch_budget.rounds, 3)
        self.assertEqual(packet.search_policy.resource_hint.tier, "generous")
        self.assertIn("high-level decision", " ".join(packet.frozen_decisions).lower())

    def test_proposal_prompt_only_shows_focus_region_and_nearby_regions(self) -> None:
        packet = Watchman().build_packet(ContextTask(), self.snapshot, self.root, self.parent, 2)

        prompt = build_proposal_prompt(self.snapshot, self.root, self.parent, 2, packet)

        self.assertIn("Focus region: kernel.py (region)", prompt)
        self.assertIn("Nearby regions: qkv projection (region), attention output (region)", prompt)


if __name__ == "__main__":
    unittest.main()
