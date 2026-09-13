from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autopipeline.cli import main


class WorkloadExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(["pipeline", *args])
        return code, output.getvalue()

    def test_catalog_and_initialization_work_without_compute(self):
        code, output = self.command("examples")
        self.assertEqual(code, 0)
        for name in ("residual-mlp", "attention", "hf-local"):
            self.assertIn(name, output)
        for name in ("residual-mlp", "attention"):
            path = self.root / name
            self.assertEqual(self.command("init", str(path), "--example", name)[0], 0)
            self.assertEqual(self.command("check", "--spec", str(path / "pipeline.json"), "--no-runtime")[0], 0)

    def test_missing_model_assets_fail_without_creating_project(self):
        project = self.root / "project"
        code, output = self.command("init", str(project), "--example", "hf-local")
        self.assertEqual(code, 2)
        self.assertIn("--model-dir", output)
        self.assertFalse(project.exists())

    def test_changed_external_weights_are_detected_before_execution(self):
        # These bytes test resource integrity only, not model loading.
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "config.json").write_text("{}")
        (assets / "model.safetensors").write_bytes(b"fixed asset")
        project = self.root / "project"
        self.assertEqual(self.command("init", str(project), "--example", "hf-local", "--model-dir", str(assets))[0], 0)
        self.assertEqual(self.command("check", "--spec", str(project / "pipeline.json"), "--no-runtime")[0], 0)
        (assets / "model.safetensors").write_bytes(b"changed asset")
        code, output = self.command("check", "--spec", str(project / "pipeline.json"), "--no-runtime")
        self.assertEqual(code, 1)
        self.assertIn("assets changed", output)

    def evaluate_project(self, project):
        config_path = project / "workload/workload.json"
        config = json.loads(config_path.read_text())
        config.update(warmup=1, repeats=2)
        config_path.write_text(json.dumps(config))
        run = project / "runs/baseline"
        code, output = self.command("evaluate", "--spec", str(project / "pipeline.json"), "--run-dir", str(run))
        self.assertEqual(code, 0, output)
        metrics = json.loads((run / "promoted_pipeline/metrics.json").read_text())
        self.assertEqual(metrics["correctness"], "PASS")
        self.assertGreater(metrics["compared_elements"], 0)
        self.assertGreater(metrics["end_to_end_ms"], 0)
        return run

    @unittest.skipUnless(importlib.util.find_spec("torch"), "requires CPU extra")
    def test_attention_executes_real_tensor_graph(self):
        project = self.root / "attention"
        self.assertEqual(self.command("init", str(project), "--example", "attention", "--device", "cpu")[0], 0)
        self.evaluate_project(project)

    @unittest.skipUnless(importlib.util.find_spec("torch") and importlib.util.find_spec("transformers"), "requires CPU and models extras")
    def test_local_transformers_checkpoint_loads_and_exports_offline(self):
        from transformers import BertConfig, BertModel
        # Locally initialized checkpoint validates the actual loader without a download.
        # It does not establish pretrained language quality or a GPU speedup.
        assets = self.root / "bert"
        BertModel(BertConfig(vocab_size=97, hidden_size=16, intermediate_size=32,
                             num_hidden_layers=1, num_attention_heads=2, max_position_embeddings=64)).save_pretrained(assets)
        project = self.root / "encoder"
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}):
            code, output = self.command("init", str(project), "--example", "hf-local", "--model-dir", str(assets), "--device", "cpu")
            self.assertEqual(code, 0, output)
            run = self.evaluate_project(project)
            export = self.root / "export"
            self.assertEqual(self.command("export", str(run), "--output-dir", str(export))[0], 0)
            self.assertEqual(self.command("check", "--spec", str(export / "pipeline.json"))[0], 0)
            self.assertFalse((export / "candidate/model.safetensors").exists())


if __name__ == "__main__":
    unittest.main()
