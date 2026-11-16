from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from autopipeline.pipeline.verification_backend import ExecutableRunRequest, ExecutableVerificationBackend


class VerificationBackendTests(unittest.TestCase):
    def test_tensor_error_ratio_requires_matching_tolerances_and_full_count(self):
        reference = {"correctness": "PASS", "throughput": 1}
        candidate = {**reference, "max_normalized_error": 0.9,
                     "numerical_atol": 1e-5, "numerical_rtol": 1e-4, "compared_elements": 100}
        self.assertTrue(self.verify_metrics(reference, candidate)["accepted"])
        for field, value in (("max_normalized_error", 1.01), ("max_normalized_error", float("nan")),
                             ("max_normalized_error", -1), ("numerical_rtol", 1),
                             ("compared_elements", 0), ("compared_elements", "100"),
                             ("compared_elements", True), ("max_abs_error", float("nan"))):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.verify_metrics(reference, {**candidate, field: value})["accepted"])

    def verify_metrics(
        self, reference, candidate, *, score_metric="throughput", score_direction="higher", rank_by_speedup=False,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requests = []
            for label, metrics in (("reference", reference), ("candidate", candidate)):
                workspace = root / label
                workspace.mkdir()
                (workspace / "input.json").write_text(json.dumps(metrics), encoding="utf-8")
                requests.append(ExecutableRunRequest(
                    label=label,
                    workspace=workspace,
                    command=[sys.executable, "-c", "from pathlib import Path; Path('metrics.json').write_text(Path('input.json').read_text())"],
                    metrics_path="metrics.json",
                ))
            report = ExecutableVerificationBackend().verify(
                reference=requests[0], candidate=requests[1], artifact_dir=root / "artifacts",
                score_metric=score_metric, score_direction=score_direction, rank_by_speedup=rank_by_speedup,
            )
            saved = json.loads((root / "artifacts" / "verification_report.json").read_text())
            self.assertEqual(saved["accepted"], report["accepted"])
            self.assertEqual(saved["failure_reason"], report["failure_reason"])
            return report

    def test_requires_explicit_correctness_on_both_runs(self):
        valid = {"correctness": "PASS", "throughput": 1, "output_value": 1}
        for side in ("reference", "candidate"):
            for status in (None, "", "UNKNOWN", "unexpected", False, "FAIL"):
                with self.subTest(side=side, status=status):
                    invalid = {**valid, "correctness": status}
                    if status is None:
                        del invalid["correctness"]
                    report = self.verify_metrics(
                        invalid if side == "reference" else valid,
                        invalid if side == "candidate" else valid,
                    )
                    self.assertFalse(report["accepted"])
                    self.assertFalse(report["correctness"]["passed"])

    def test_empty_metrics_are_missing_evidence(self):
        report = self.verify_metrics({}, {})
        self.assertFalse(report["accepted"])
        self.assertEqual(report["failure_reason"], "correctness_evidence_missing")
        self.assertFalse(report["numerical_error"]["available"])
        self.assertFalse(report["performance"]["available"])

    def test_requires_output_or_error_evidence(self):
        valid = {"correctness": "PASS", "throughput": 1}
        for reference, candidate in ((valid, valid), ({**valid, "output_value": 1}, valid), (valid, {**valid, "output_value": 1})):
            with self.subTest(reference=reference, candidate=candidate):
                report = self.verify_metrics(reference, candidate)
                self.assertFalse(report["accepted"])
                self.assertEqual(report["failure_reason"], "numerical_evidence_missing")

    def test_single_error_metric_is_checked_without_defaulting_the_other_to_zero(self):
        reference = {"correctness": "PASS", "throughput": 1}
        for key, limit in (("max_abs_error", 1e-5), ("max_rel_error", 1e-4)):
            for error, accepted in ((100, False), (limit, True), (limit / 2, True)):
                with self.subTest(key=key, error=error):
                    report = self.verify_metrics(reference, {**reference, "throughput": 2, key: error})
                    self.assertEqual(report["accepted"], accepted)
                    absent_key = "max_rel_error" if key == "max_abs_error" else "max_abs_error"
                    self.assertIsNone(report["numerical_error"][absent_key])

    def test_invalid_error_metrics_cannot_be_hidden_by_a_passing_metric(self):
        valid = {"correctness": "PASS", "throughput": 1}
        for value in (float("nan"), float("inf"), -float("inf"), -1, True, None, "bad"):
            with self.subTest(value=value):
                report = self.verify_metrics(valid, {**valid, "max_abs_error": value, "max_rel_error": 0})
                self.assertFalse(report["accepted"])
                self.assertEqual(report["failure_reason"], "invalid_error_metric")

    def test_non_finite_outputs_are_rejected_on_either_side(self):
        valid = {"correctness": "PASS", "throughput": 1, "output_value": [1]}
        for side in ("reference", "candidate"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(side=side, value=value):
                    invalid = {**valid, "output_value": [value], "max_abs_error": 0}
                    report = self.verify_metrics(
                        invalid if side == "reference" else valid,
                        invalid if side == "candidate" else valid,
                    )
                    self.assertFalse(report["accepted"])
                    self.assertEqual(report["failure_reason"], "non_finite_output")

    def test_output_structure_and_non_numeric_fields_are_preserved(self):
        valid = {"correctness": "PASS", "throughput": 1}
        for reference, candidate in (
            ([[1, 2]], [1, 2]), ({"a": 1}, {"b": 1}),
            ([1, "cat"], [1, "dog"]), ([1, 2], [1]), (True, 1), (None, None),
        ):
            with self.subTest(reference=reference, candidate=candidate):
                report = self.verify_metrics({**valid, "output_value": reference}, {**valid, "output_value": candidate})
                self.assertFalse(report["accepted"])

    def test_passing_error_summary_cannot_override_wrong_output(self):
        valid = {"correctness": "PASS", "throughput": 1, "output_value": 1}
        report = self.verify_metrics(valid, {**valid, "throughput": 2, "output_value": 2, "max_abs_error": 0})
        self.assertFalse(report["accepted"])
        self.assertEqual(report["numerical_error"]["max_abs_error"], 1)

    def test_tolerance_is_applied_per_element_with_absolute_and_relative_terms(self):
        valid = {"correctness": "PASS", "throughput": 1}
        report = self.verify_metrics(
            {**valid, "output_value": [0, 1000, 1]},
            {**valid, "output_value": [5e-6, 1000.05, 1.000105]},
        )
        self.assertTrue(report["accepted"])

    def test_declared_performance_metric_must_be_finite_and_present_on_both_runs(self):
        valid = {"correctness": "PASS", "throughput": 1, "end_to_end_ms": 10, "output_value": 1}
        for side in ("reference", "candidate"):
            for value in (None, float("nan"), float("inf"), -float("inf"), True, "bad"):
                with self.subTest(side=side, value=value):
                    invalid = {**valid, "throughput": value}
                    if value is None:
                        del invalid["throughput"]
                    report = self.verify_metrics(
                        invalid if side == "reference" else valid,
                        invalid if side == "candidate" else valid,
                    )
                    self.assertFalse(report["accepted"])
                    self.assertEqual(report["failure_reason"], "performance_evidence_missing_or_invalid")

    def test_correct_candidates_obey_both_score_directions(self):
        valid = {"correctness": "PASS", "output_value": {"value": [1], "label": "ok"}}
        for direction, improved, regressed in (("higher", 12, 8), ("lower", 8, 12)):
            for score, accepted in ((10, True), (improved, True), (regressed, False)):
                with self.subTest(direction=direction, score=score):
                    report = self.verify_metrics(
                        {**valid, "custom_score": 10}, {**valid, "custom_score": score},
                        score_metric="custom_score", score_direction=direction,
                    )
                    self.assertEqual(report["accepted"], accepted)
                    if not accepted:
                        self.assertEqual(report["failure_reason"], "performance_regressed")

    def test_native_ranking_compares_each_candidate_to_its_own_reference(self):
        valid = {"correctness": "PASS", "output_value": [1]}
        def evaluate(reference_ms, candidate_ms):
            return self.verify_metrics(
                {**valid, "forward_latency_ms": reference_ms},
                {**valid, "forward_latency_ms": candidate_ms},
                score_metric="forward_latency_ms", score_direction="lower", rank_by_speedup=True,
            )

        first, second = evaluate(100, 80), evaluate(30, 27)
        self.assertTrue(first["accepted"] and second["accepted"])
        self.assertEqual(first["performance"]["ranking_score"], 1.25)
        self.assertGreater(first["performance"]["ranking_score"], second["performance"]["ranking_score"])
        self.assertEqual(first["performance"]["candidate_score_raw"], 80)
        self.assertEqual(evaluate(30, 31)["failure_reason"], "performance_regressed")
        self.assertEqual(evaluate(30, 0)["failure_reason"], "performance_evidence_missing_or_invalid")

    def test_invalid_tolerance_configuration_is_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            for keyword in ("numerical_atol", "numerical_rtol"):
                with self.subTest(keyword=keyword, value=value):
                    with self.assertRaises(ValueError):
                        ExecutableVerificationBackend(**{keyword: value})

    def test_rejects_candidate_with_output_mismatch_even_when_faster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluator = root / "evaluate_workload.py"
            evaluator.write_text(
                textwrap.dedent(
                    """
                    import json
                    from pathlib import Path

                    config = json.loads(Path("workload.json").read_text(encoding="utf-8"))
                    latency_ms = float(config["latency_ms"])
                    metrics = {
                        "accepted": True,
                        "correctness": "PASS",
                        "output_value": float(config["output_value"]),
                        "end_to_end_ms": latency_ms,
                        "throughput": 1000.0 / latency_ms,
                    }
                    Path("metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
                    print(json.dumps(metrics))
                    """
                ),
                encoding="utf-8",
            )
            reference = root / "reference"
            candidate = root / "candidate"
            reference.mkdir()
            candidate.mkdir()
            (reference / "workload.json").write_text(
                json.dumps({"latency_ms": 100.0, "output_value": 1.0}),
                encoding="utf-8",
            )
            (candidate / "workload.json").write_text(
                json.dumps({"latency_ms": 40.0, "output_value": 2.0}),
                encoding="utf-8",
            )

            report = ExecutableVerificationBackend().verify(
                reference=ExecutableRunRequest(
                    label="reference",
                    workspace=reference,
                    command=[sys.executable, str(evaluator)],
                    metrics_path="metrics.json",
                ),
                candidate=ExecutableRunRequest(
                    label="candidate",
                    workspace=candidate,
                    command=[sys.executable, str(evaluator)],
                    metrics_path="metrics.json",
                ),
                artifact_dir=root / "artifacts",
                score_metric="throughput",
                score_direction="higher",
            )

            self.assertFalse(report["accepted"])
            self.assertEqual(report["verdict"], "reject")
            self.assertEqual(report["failure_reason"], "numerical_error_exceeded")
            self.assertGreater(report["performance"]["speedup"], 1.0)
            self.assertEqual(report["numerical_error"]["max_abs_error"], 1.0)
            self.assertIn("stdout", report["candidate"])
            self.assertIn("stderr", report["candidate"])
            self.assertTrue(Path(report["artifact_paths"]["report"]).exists())
            self.assertTrue(Path(report["artifact_paths"]["candidate_stdout"]).exists())


if __name__ == "__main__":
    unittest.main()
