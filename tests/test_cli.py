from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from autopipeline.cli import main


class AutoPipelineCliTests(unittest.TestCase):
    def test_product_import_keeps_smoke_lazy_and_legacy_imports_work(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import autopipeline.pipeline_cli, sys; "
             "assert 'pipeline.smoke' not in sys.modules; "
             "from pipeline.task import PipelineSmokeAgent, create_pipeline_smoke_fixture; "
             "from pipeline import run_pipeline_smoke; "
             "from pipeline import smoke; "
             "assert PipelineSmokeAgent is smoke.PipelineSmokeAgent; "
             "assert create_pipeline_smoke_fixture is smoke.create_pipeline_smoke_fixture; "
             "assert run_pipeline_smoke is smoke.run_pipeline_smoke"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_report_missing_run_fails_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            code, stdout, stderr = self._run("report", str(missing))
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("search_state.json", stderr)
            self.assertFalse(missing.exists())

    def test_report_legacy_script_matches_installed_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            (run / "search_state.json").write_text(json.dumps({
                "task_snapshot": {"task_type": "pipeline", "objective": "Report contract"},
                "nodes": {},
            }))
            output = Path(tmp) / "report"
            code, stdout, stderr = self._run("report", str(run), "--output-dir", str(output))
            self.assertEqual(code, 0, stderr)
            summary = (output / "run_summary.json").read_text()
            script = Path(__file__).resolve().parents[1] / "scripts/tree_report.py"
            legacy = subprocess.run(
                [sys.executable, str(script), str(run), "--output-dir", str(output)],
                cwd=tmp, capture_output=True, text=True,
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertEqual(legacy.stdout, stdout)
            self.assertEqual((output / "run_summary.json").read_text(), summary)
            self.assertTrue((output / "dashboard.html").is_file())

    def test_stdlib_profiler_is_not_shadowed_by_source_entrypoint(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import cProfile, sys; assert cProfile.Profile().runcall(lambda: 7) == 7; assert 'torch' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
    def _run(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_top_level_help_names_primary_commands(self) -> None:
        code, stdout, stderr = self._run("--help")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("autopipeline doctor", stdout)
        self.assertIn("autopipeline tree run", stdout)
        self.assertIn("Existing top-level scripts still work", stdout)

    def test_tree_help_does_not_require_gpu_or_workspace(self) -> None:
        code, stdout, stderr = self._run("tree", "--help")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("autopipeline tree run", stdout)

    def test_forwarded_command_help_does_not_import_runtime_dependencies(self) -> None:
        code, stdout, stderr = self._run("profile", "--help")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Profile a model", stdout)
        self.assertIn("profile.py", stdout)

    def test_doctor_can_skip_gpu_requirement(self) -> None:
        code, stdout, stderr = self._run("doctor", "--no-gpu")

        self.assertEqual(stderr, "")
        self.assertIn("AutoPipeline doctor", stdout)
        self.assertIn("GPU requirement: skipped", stdout)
        self.assertIn(code, {0, 1})

    def test_unknown_command_fails_with_help_hint(self) -> None:
        code, stdout, stderr = self._run("unknown")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("unknown command", stderr)
        self.assertIn("autopipeline --help", stderr)


if __name__ == "__main__":
    unittest.main()
