from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tree.viewer import build_run_view, write_run_view


class ViewerTests(unittest.TestCase):
    def test_builds_dashboard_artifacts_from_tree_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            (run_dir / "search_state.json").write_text(
                json.dumps(
                    {
                        "task_snapshot": {
                            "task_type": "autokernel",
                            "objective": "Optimize matmul",
                            "target_path": "kernel.py",
                            "metadata": {},
                        },
                        "search_config": {
                            "beam_width": 2,
                            "max_rounds": 1,
                            "children_per_expansion": 2,
                        },
                        "root_memory": {
                            "baseline_score": 1.0,
                            "best_score": 2.2,
                            "best_node_id": "node-0002",
                        },
                        "nodes": {
                            "node-0001": {
                                "id": "node-0001",
                                "parent_id": None,
                                "depth": 0,
                                "hypothesis": {
                                    "id": "baseline",
                                    "title": "Baseline",
                                    "core_hypothesis": "baseline",
                                    "family": "baseline",
                                },
                                "status": "completed",
                                "evaluation_result": {
                                    "accepted": True,
                                    "score": 1.0,
                                    "metrics": {
                                        "correctness": "PASS",
                                        "speedup_vs_pytorch": 1.0,
                                        "throughput_tflops": 18.3,
                                    },
                                    "summary": "baseline",
                                    "raw_log_paths": {},
                                },
                            },
                            "node-0002": {
                                "id": "node-0002",
                                "parent_id": "node-0001",
                                "depth": 1,
                                "hypothesis": {
                                    "id": "memory",
                                    "title": "Memory Optimization",
                                    "core_hypothesis": "memory path",
                                    "family": "memory_hierarchy",
                                    "idea_id": "memory-access",
                                    "implementation_variant": "coalesced-loads",
                                },
                                "status": "completed",
                                "evaluation_result": {
                                    "accepted": True,
                                    "score": 2.2,
                                    "metrics": {
                                        "correctness": "PASS",
                                        "speedup_vs_pytorch": 2.2,
                                        "throughput_tflops": 40.1,
                                    },
                                    "summary": "improved",
                                    "raw_log_paths": {"stdout": "bench.stdout.log"},
                                },
                            },
                            "node-0003": {
                                "id": "node-0003",
                                "parent_id": "node-0001",
                                "depth": 1,
                                "hypothesis": {
                                    "id": "bad",
                                    "title": "Bad Variant",
                                    "core_hypothesis": "bad path",
                                    "family": "instruction_selection",
                                },
                                "status": "failed",
                                "error": "compile error",
                                "evaluation_result": None,
                            },
                        },
                        "frontier_node_ids": ["node-0002"],
                        "budget": {
                            "agent_usage": {
                                "api_calls": 3,
                                "total_tokens": 1200,
                                "cost_usd": 0.12,
                                "agent_seconds": 14.0,
                            }
                        },
                        "final_result": {
                            "best_node_id": "node-0002",
                            "promoted_path": "kernel_optimized.py",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "tree_events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-05-03T00:00:00+00:00",
                                "event": "baseline_completed",
                                "payload": {
                                    "node_id": "node-0001",
                                    "score": 1.0,
                                    "accepted": True,
                                    "summary": "baseline",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-03T00:01:00+00:00",
                                "event": "evaluation_completed",
                                "payload": {
                                    "node_id": "node-0002",
                                    "accepted": True,
                                    "score": 2.2,
                                    "summary": "improved",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-03T00:02:00+00:00",
                                "event": "candidate_completed",
                                "payload": {
                                    "node_id": "node-0003",
                                    "status": "failed",
                                    "error": "compile error",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            view = build_run_view(run_dir)
            self.assertEqual(view["run_summary"]["counts"]["实验次数"], 2)
            self.assertEqual(view["run_summary"]["counts"]["变更被采纳"], 1)
            self.assertEqual(view["run_summary"]["display_metric"]["key"], "throughput_tflops")

            paths = write_run_view(run_dir)
            self.assertTrue(paths.dashboard.exists())
            self.assertTrue(paths.iteration_effect.exists())
            self.assertTrue(paths.attempts.exists())
            self.assertIn("AutoKernel", paths.dashboard.read_text(encoding="utf-8"))
            self.assertIn("<svg", paths.iteration_effect.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
