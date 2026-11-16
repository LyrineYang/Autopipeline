from __future__ import annotations

import unittest

from autopipeline.search.budget import BudgetLedger
from autopipeline.search.planner import Planner
from autopipeline.search.types import EvaluationResult, Hypothesis, RootMemory, SearchNode, TaskSnapshot


class BudgetTask:
    def seed_hypotheses(self, parent, root, limit):
        return [
            Hypothesis(
                id="budget-branch",
                title="Budget Branch",
                core_hypothesis="try a cheap branch",
                family="cheap_family",
            )
        ][:limit]

    def planner_context(self, parent, root, limit, *, upper_bounds=None):
        return {
            "branch_cards": [
                {
                    "id": "budget-branch",
                    "title": "Budget Branch",
                    "core_hypothesis": "try a cheap branch",
                    "family": "cheap_family",
                }
            ],
            "planner_rules": {"allowed_families": ["cheap_family"]},
        }


class BudgetLedgerTests(unittest.TestCase):
    def test_records_agent_usage_from_command_output(self) -> None:
        ledger = BudgetLedger(max_agent_calls=3)

        ledger.record_command(
            action="execute",
            status="completed",
            duration_seconds=1.25,
            output={
                "metadata": {
                    "llm_usage": {
                        "api_calls": 2,
                        "prompt_tokens": 100,
                        "completion_tokens": 40,
                        "cost_usd": 0.012,
                    }
                }
            },
        )

        summary = ledger.summary()
        self.assertEqual(summary["agent_usage"]["agent_calls"], 1)
        self.assertEqual(summary["agent_usage"]["api_calls"], 2)
        self.assertEqual(summary["agent_usage"]["total_tokens"], 140)
        self.assertEqual(summary["agent_usage"]["remaining_agent_calls"], 2)

    def test_records_role_usage_and_failure_classes(self) -> None:
        ledger = BudgetLedger()

        ledger.record_command(
            action="execute",
            role="executor",
            status="timed_out",
            duration_seconds=2.0,
            failure_class="zero_call_timeout",
            output=None,
        )

        summary = ledger.summary()
        self.assertEqual(summary["role_stats"]["executor"]["calls"], 1)
        self.assertEqual(summary["role_stats"]["executor"]["timeouts"], 1)
        self.assertEqual(summary["role_stats"]["executor"]["failure_classes"]["zero_call_timeout"], 1)

    def test_family_and_probe_stats_generate_budget_recommendations(self) -> None:
        ledger = BudgetLedger(max_probe_calls=5)
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="parent", title="Parent", core_hypothesis="baseline"),
            evaluation_result=EvaluationResult(True, 1.0, {}, "baseline"),
        )
        for index in range(2):
            node = SearchNode(
                id=f"node-000{index + 2}",
                parent_id=parent.id,
                depth=1,
                hypothesis=Hypothesis(
                    id=f"branch-{index}",
                    title="Branch",
                    core_hypothesis="compile failure branch",
                    family="fusion",
                ),
                evaluation_result=EvaluationResult(False, 0.8, {}, "compile error"),
                status="failed",
                error="compile failed",
            )
            ledger.record_node(node=node, parent=parent)
        for _ in range(2):
            ledger.record_probe(
                action="execute",
                result={
                    "kind": "expensive_profile",
                    "status": "completed",
                    "summary": "profile produced no objective signal",
                    "data": {},
                },
            )

        summary = ledger.summary()
        self.assertEqual(summary["family_stats"]["fusion"]["compile_failures"], 2)
        self.assertIn(
            "cool down fusion; compile failures are recurring",
            summary["recommendations"],
        )
        self.assertIn(
            "avoid repeated expensive_profile probes unless uncertainty remains high",
            summary["recommendations"],
        )

    def test_planner_packet_receives_budget_context(self) -> None:
        ledger = BudgetLedger(max_agent_calls=2)
        ledger.record_command(action="plan", status="completed", duration_seconds=0.5, output={})
        task_snapshot = TaskSnapshot(
            task_type="fake",
            objective="improve score",
            target_path="/tmp/fake.py",
            editable_paths=["candidate.py"],
            constraints=[],
            reference_paths=[],
        )
        parent = SearchNode(
            id="node-0001",
            parent_id=None,
            depth=0,
            hypothesis=Hypothesis(id="parent", title="Parent", core_hypothesis="baseline"),
        )

        packet = Planner(budget_ledger=ledger).build_packet(
            BudgetTask(),
            task_snapshot,
            RootMemory(objective=task_snapshot.objective),
            parent,
            1,
        )

        self.assertIn("budget_context", packet.history_context)
        self.assertEqual(
            packet.history_context["budget_context"]["agent_usage"]["remaining_agent_calls"],
            1,
        )
        self.assertNotIn("command_stats", packet.history_context["budget_context"])


if __name__ == "__main__":
    unittest.main()
