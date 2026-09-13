from __future__ import annotations

import contextlib
import io
import unittest
import subprocess
import sys
from pathlib import Path

from autopipeline.cli import main


class AutoPipelineCliTests(unittest.TestCase):
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
