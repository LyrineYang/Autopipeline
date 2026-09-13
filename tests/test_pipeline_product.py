from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest

from autopipeline.cli import main
from autopipeline.project import initialize
from pipeline.task import PipelineSpec, create_pipeline_smoke_fixture


class PipelineProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(list(args))
        return code, output.getvalue()

    def fixture(self):
        # Synthetic numbers test command/error handling only; tensor execution is below.
        return create_pipeline_smoke_fixture(self.root / "fixture")

    def test_init_and_offline_check_do_not_need_compute_or_agent(self):
        project = self.root / "project"
        self.assertEqual(self.command("pipeline", "init", str(project))[0], 0)
        code, output = self.command("pipeline", "check", "--spec", str(project / "pipeline.json"), "--no-runtime", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["runtime"], "not_checked")
        self.assertEqual(json.loads(output)["executor"], "not_configured")
        self.assertTrue((project / "workload" / "reference.py").exists())
        self.assertEqual(self.command("pipeline", "init", str(project))[0], 2)

    def test_init_accepts_real_user_source(self):
        source = self.root / "source"
        source.mkdir()
        (source / "reference.py").write_text("# user workload\n")
        spec = initialize(self.root / "project", source=source, device="cpu", dtype="float32")
        self.assertEqual((spec.parent / "workload/reference.py").read_text(), "# user workload\n")
        self.assertIn("ModelNew", (spec.parent / "workload/candidate.py").read_text())

    def test_spec_rejects_unsafe_paths_and_missing_commands(self):
        path = self.fixture()
        original = json.loads(path.read_text())
        for key, value in (("target_path", "../outside"), ("editable_paths", ["/tmp"]),
                           ("metrics_path", "../metrics.json"), ("evaluate_command", []),
                           ("score_direction", "typo"), ("timeout_seconds", -1)):
            with self.subTest(key=key):
                path.write_text(json.dumps({**original, key: value}))
                with self.assertRaises(ValueError):
                    PipelineSpec.load(path)

    def test_missing_executor_is_an_error_before_creating_run(self):
        path = self.fixture()
        run = self.root / "run"
        code, output = self.command("pipeline", "run", "--spec", str(path), "--run-dir", str(run))
        self.assertEqual(code, 2)
        self.assertIn("No executor configured", output)
        self.assertFalse(run.exists())

    def test_setup_rejects_recursive_destination(self):
        path = self.fixture()
        workspace = PipelineSpec.load(path).workspace_source
        code, output = self.command("pipeline", "setup", "--spec", str(path), "--output-dir", str(workspace / "nested"))
        self.assertEqual(code, 2, output)
        self.assertFalse((workspace / "nested").exists())

    def test_whole_workspace_baseline_is_not_reported_as_an_edit(self):
        path = self.fixture()
        data = json.loads(path.read_text())
        data.update(target_path=".", editable_paths=["."])
        path.write_text(json.dumps(data))
        run = self.root / "directory-target"
        code, output = self.command("pipeline", "evaluate", "--spec", str(path), "--run-dir", str(run))
        self.assertEqual(code, 0, output)
        report = json.loads((run / "final_verification/verification_report.json").read_text())
        self.assertFalse(report["require_no_regression"])

    def test_added_undeclared_python_file_is_rejected(self):
        path = self.fixture()
        candidate = self.root / "candidate"
        shutil.copytree(PipelineSpec.load(path).workspace_source, candidate)
        (candidate / "unexpected.py").write_text("# undeclared implementation")
        code, _ = self.command("pipeline", "evaluate", "--spec", str(path), "--candidate", str(candidate), "--run-dir", str(self.root / "bad"))
        self.assertEqual(code, 1)

    def test_evaluation_export_and_recheck_preserve_fixed_reference(self):
        path = self.fixture()
        run = self.root / "run"
        code, output = self.command("pipeline", "evaluate", "--spec", str(path), "--run-dir", str(run))
        self.assertEqual(code, 0, output)
        receipt = json.loads((run / "result.json").read_text())
        self.assertEqual(receipt["status"], "verified")
        self.assertTrue((run / "view" / "dashboard.html").is_file())
        self.assertEqual(self.command("pipeline", "evaluate", "--spec", str(path), "--run-dir", str(run))[0], 2)
        exported = self.root / "exported"
        code, output = self.command("pipeline", "export", str(run), "--output-dir", str(exported))
        self.assertEqual(code, 0, output)
        code, output = self.command("pipeline", "evaluate", "--spec", str(exported / "pipeline.json"),
                                    "--candidate", str(exported / "candidate"), "--run-dir", str(self.root / "recheck"))
        self.assertEqual(code, 0, output)
        (run / "promoted_pipeline/pipeline_config.json").write_text("{}")
        self.assertEqual(self.command("pipeline", "export", str(run), "--output-dir", str(self.root / "bad-export"))[0], 2)

    def test_protected_reference_change_is_rejected(self):
        path = self.fixture()
        candidate = self.root / "candidate"
        shutil.copytree(PipelineSpec.load(path).workspace_source, candidate)
        (candidate / "evaluate.py").write_text("raise RuntimeError('tampered')")
        # Use an existing fixed file name from the fixture, not an assumed harness name.
        for file in candidate.glob("*.py"):
            if file.name != "evaluate.py":
                file.write_text("raise RuntimeError('tampered')")
        code, _ = self.command("pipeline", "evaluate", "--spec", str(path), "--candidate", str(candidate), "--run-dir", str(self.root / "bad"))
        self.assertEqual(code, 1)
        state = json.loads((self.root / "bad/search_state.json").read_text())
        metrics = state["nodes"]["node-0001"]["evaluation_result"]["metrics"]
        self.assertEqual(metrics["verification_failure_reason"], "protected_files_changed")

    def test_failed_agent_is_not_reported_as_successful_optimization(self):
        path = self.fixture()
        run = self.root / "failed-agent"
        command = f"{shlex.quote(sys.executable)} -c 'raise SystemExit(7)'"
        code, output = self.command("pipeline", "run", "--spec", str(path), "--run-dir", str(run),
                                    "--executor-command", command, "--max-rounds", "1", "--children-per-expansion", "1")
        self.assertEqual(code, 1, output)
        self.assertEqual(json.loads((run / "result.json").read_text())["status"], "failed")
        self.assertTrue(list(run.glob("nodes/*/agent/execute*/stderr.log")))

    def test_invalid_baseline_does_not_start_paid_agent(self):
        path = self.fixture()
        data = json.loads(path.read_text())
        data["evaluate_command"] = ["{python}", "-c", "raise SystemExit(9)"]
        path.write_text(json.dumps(data))
        marker = self.root / "agent-was-called"
        code, output = self.command("pipeline", "run", "--spec", str(path), "--run-dir", str(self.root / "run"),
                                    "--executor-command", f"touch {shlex.quote(str(marker))}")
        self.assertEqual(code, 1, output)
        self.assertFalse(marker.exists())

    def test_final_evaluator_cannot_mutate_protected_files(self):
        path = self.fixture()
        data = json.loads(path.read_text())
        data["final_verify_command"] = ["{python}", "-c",
            "import runpy; from pathlib import Path; runpy.run_path('evaluate_pipeline.py', run_name='__main__'); Path('evaluate_pipeline.py').write_text('# changed')"]
        path.write_text(json.dumps(data))
        run = self.root / "run"
        code, output = self.command("pipeline", "evaluate", "--spec", str(path), "--run-dir", str(run))
        self.assertEqual(code, 1, output)
        report = json.loads((run / "final_verification/verification_report.json").read_text())
        self.assertEqual(report["failure_reason"], "protected_files_changed")
        self.assertFalse((run / "promoted_pipeline").exists())

    @unittest.skipUnless(importlib.util.find_spec("torch"), "Install the cpu extra for actual PyTorch integration")
    def test_real_pytorch_forward_profiler_and_exported_recheck(self):
        path = initialize(self.root / "torch", source=None, device="cpu", dtype="float32")
        config_path = path.parent / "workload/workload.json"
        config = json.loads(config_path.read_text())
        config.update(warmup=1, repeats=2)
        config_path.write_text(json.dumps(config))
        run = self.root / "torch-run"
        code, output = self.command("pipeline", "evaluate", "--spec", str(path), "--run-dir", str(run))
        self.assertEqual(code, 0, output)
        metrics = json.loads((run / "promoted_pipeline/metrics.json").read_text())
        self.assertEqual(metrics["correctness"], "PASS")
        self.assertGreater(metrics["compared_elements"], 0)
        self.assertGreater(metrics["end_to_end_ms"], 0)
        self.assertEqual(metrics["profiler"]["status"], "completed")
        self.assertTrue((run / "promoted_pipeline/.autopipeline/trace.json").is_file())
        editor = self.root / "editor.py"
        editor.write_text(
            "import os,json\nfrom pathlib import Path\n"
            "assert Path(os.environ['TREE_PROMPT_PATH']).is_file()\n"
            "Path('candidate.py').write_text('from reference import Model\\nclass ModelNew(Model):\\n def forward(self, x):\\n  return super().forward(x).contiguous()\\n')\n"
            "Path(os.environ['TREE_OUTPUT_PATH']).write_text(json.dumps({'status':'completed','changed':True,'summary':'Exercise candidate editing and evaluation'}))\n")
        code, output = self.command("pipeline", "run", "--spec", str(path), "--run-dir", str(self.root / "torch-agent"),
                                    "--executor-command", f"{shlex.quote(sys.executable)} {shlex.quote(str(editor))}",
                                    "--max-rounds", "1", "--children-per-expansion", "1")
        # Timing can reject the edited candidate; either outcome must retain evidence.
        self.assertIn(code, (0, 1), output)
        state = json.loads((self.root / "torch-agent/search_state.json").read_text())
        self.assertEqual(len(state["nodes"]), 2)
        child = state["nodes"]["node-0002"]
        self.assertEqual(child["execution_result"]["status"], "completed")
        self.assertEqual(child["evaluation_result"]["metrics"]["correctness"], "PASS")
        self.assertEqual((path.parent / "workload/candidate.py").read_text(),
                         (run / "input/workload/candidate.py").read_text())
        exported = self.root / "torch-export"
        self.assertEqual(self.command("pipeline", "export", str(run), "--output-dir", str(exported))[0], 0)
        code, output = self.command("pipeline", "evaluate", "--spec", str(exported / "pipeline.json"),
                                    "--candidate", str(exported / "candidate"), "--run-dir", str(self.root / "torch-recheck"))
        self.assertEqual(code, 0, output)
        (exported / "candidate/candidate.py").write_text(
            "from reference import Model\nclass ModelNew(Model):\n def forward(self, x):\n  return super().forward(x) + 100\n")
        code, output = self.command("pipeline", "evaluate", "--spec", str(exported / "pipeline.json"),
                                    "--candidate", str(exported / "candidate"), "--run-dir", str(self.root / "torch-wrong"))
        self.assertEqual(code, 1, output)
        self.assertFalse((self.root / "torch-wrong/promoted_pipeline").exists())


if __name__ == "__main__":
    unittest.main()
