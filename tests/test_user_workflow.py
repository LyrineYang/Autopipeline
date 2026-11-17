from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest

from autopipeline.cli import main
from autopipeline.pipeline_cli import build_parser
from autopipeline.presentation import outcome


class UserWorkflowTests(unittest.TestCase):
    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), errors.getvalue()

    def test_help_explains_cost_and_defaults_without_removing_advanced_options(self):
        code, common, _ = self.invoke("pipeline", "run", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--max-agent-calls", common)
        self.assertIn("not spending limits", common)
        self.assertNotIn("--planner-handoff-mode", common)
        code, full, _ = self.invoke("pipeline", "run", "--help-all")
        self.assertEqual(code, 0)
        self.assertIn("--planner-handoff-mode", full)
        args = build_parser().parse_args(["run", "--spec", "x", "--planner-handoff-mode", "idea",
                                         "--reflector-model", "custom"])
        self.assertEqual(args.planner_handoff_mode, "idea")
        self.assertEqual(args.reflector_model, "custom")
        _, init, _ = self.invoke("pipeline", "init", "--help")
        self.assertIn("default: cuda", init)
        self.assertNotIn("default: None", init)
        self.assertEqual(self.invoke("pipeline")[0], 0)

    def test_next_command_quotes_paths_and_json_remains_machine_readable(self):
        with tempfile.TemporaryDirectory(prefix="user workflow ") as tmp:
            project = Path(tmp) / "my model"
            code, output, _ = self.invoke("pipeline", "init", str(project), "--device", "cpu")
            self.assertEqual(code, 0)
            next_command = next(line for line in output.splitlines() if line.startswith("autopipeline "))
            parts = shlex.split(next_command)
            self.assertEqual(Path(parts[-1]), project.resolve() / "pipeline.json")
            code, raw, errors = self.invoke(*parts[1:], "--no-runtime", "--json")
            self.assertEqual(code, 0, errors)
            self.assertTrue(json.loads(raw)["ok"])
            self.assertEqual(json.loads(raw)["runtime"], "not_checked")
            guide = (project / "README.md").read_text()
            self.assertLess(guide.index("pipeline export"), guide.index("pipeline run"))

    def test_early_failure_and_interruption_have_usable_reports(self):
        for status in ("failed", "interrupted", "running"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp)
                reason = "candidate import failed <script>alert(1)</script>"
                (run / "result.json").write_text(json.dumps({"status": status, "error": reason}))
                code, output, errors = self.invoke("report", str(run))
                self.assertEqual(code, 0, errors)
                self.assertIn(outcome({"status": status})["title"], output)
                self.assertIn(reason, output)
                html = (run / "view/dashboard.html").read_text()
                self.assertIn("&lt;script&gt;", html)
                self.assertNotIn("<script>alert", html)
                markdown = (run / "view/summary.md").read_text()
                self.assertIn(outcome({"status": status})["title"], markdown)
                self.assertNotIn("pipeline export", output)
                self.assertFalse((run / "search_state.json").exists())

    def test_success_without_verification_is_not_presented_as_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "result.json").write_text(json.dumps({"status": "completed_improved", "verified": False}))
            code, output, _ = self.invoke("report", str(run))
            self.assertEqual(code, 0)
            self.assertIn("Outcome unconfirmed", output)
            self.assertNotIn("pipeline export", output)


if __name__ == "__main__":
    unittest.main()
