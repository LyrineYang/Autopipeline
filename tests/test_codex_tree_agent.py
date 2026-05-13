from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CodexTreeAgentWrapperTests(unittest.TestCase):
    def test_execute_places_global_codex_flags_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = root / "sandbox"
            sandbox.mkdir()
            (sandbox / "kernel.py").write_text("baseline\n", encoding="utf-8")
            prompt_path = root / "prompt.txt"
            payload_path = root / "payload.json"
            output_path = root / "output.json"
            args_path = root / "codex_args.json"
            prompt_path.write_text("execute", encoding="utf-8")
            payload_path.write_text("{}", encoding="utf-8")
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

Path(os.environ["FAKE_CODEX_ARGS_PATH"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
sys.stdin.read()
""",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)

            env = {
                **os.environ,
                "TREE_PROMPT_PATH": str(prompt_path),
                "TREE_PAYLOAD_PATH": str(payload_path),
                "TREE_OUTPUT_PATH": str(output_path),
                "FAKE_CODEX_ARGS_PATH": str(args_path),
                "PYTHONPATH": str(Path.cwd()),
            }
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path.cwd() / "scripts" / "codex_tree_agent.py"),
                    "execute",
                    "--codex-bin",
                    str(fake_codex),
                    "--model",
                    "test-model",
                    "--approval",
                    "never",
                ],
                cwd=str(sandbox),
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = json.loads(output_path.read_text(encoding="utf-8"))
            codex_args = json.loads(args_path.read_text(encoding="utf-8"))
            self.assertEqual(output["status"], "completed")
            self.assertEqual(codex_args[:5], ["--model", "test-model", "--ask-for-approval", "never", "exec"])
            self.assertIn("--skip-git-repo-check", codex_args)

    def test_build_codex_command_supports_current_cli_shape(self) -> None:
        from scripts.codex_tree_agent import _build_codex_command

        command = _build_codex_command(
            codex_bin="/bin/codex",
            cwd=Path("/tmp/node"),
            approval="never",
            model="test-model",
        )

        self.assertEqual(
            command,
            [
                "/bin/codex",
                "--model",
                "test-model",
                "--ask-for-approval",
                "never",
                "exec",
                "-C",
                "/tmp/node",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-",
            ],
        )

    def test_execute_uses_tree_agent_model_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = root / "sandbox"
            sandbox.mkdir()
            (sandbox / "kernel.py").write_text("baseline\n", encoding="utf-8")
            prompt_path = root / "prompt.txt"
            payload_path = root / "payload.json"
            output_path = root / "output.json"
            args_path = root / "codex_args.json"
            prompt_path.write_text("execute", encoding="utf-8")
            payload_path.write_text("{}", encoding="utf-8")
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

Path(os.environ["FAKE_CODEX_ARGS_PATH"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
sys.stdin.read()
""",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)

            env = os.environ.copy()
            env.pop("CODEX_TREE_MODEL", None)
            env.update(
                {
                    "TREE_PROMPT_PATH": str(prompt_path),
                    "TREE_PAYLOAD_PATH": str(payload_path),
                    "TREE_OUTPUT_PATH": str(output_path),
                    "TREE_AGENT_MODEL": "role-model",
                    "FAKE_CODEX_ARGS_PATH": str(args_path),
                    "PYTHONPATH": str(Path.cwd()),
                }
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path.cwd() / "scripts" / "codex_tree_agent.py"),
                    "execute",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=str(sandbox),
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            codex_args = json.loads(args_path.read_text(encoding="utf-8"))
            self.assertEqual(codex_args[:5], ["--model", "role-model", "--ask-for-approval", "never", "exec"])

    def test_execute_ignores_non_editable_benchmark_artifacts_for_change_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = root / "sandbox"
            sandbox.mkdir()
            kernel_path = sandbox / "kernel.py"
            kernel_path.write_text("baseline\n", encoding="utf-8")
            prompt_path = root / "prompt.txt"
            payload_path = root / "payload.json"
            output_path = root / "output.json"
            prompt_path.write_text("execute", encoding="utf-8")
            payload_path.write_text(
                json.dumps(
                    {
                        "task": {"editable_paths": ["kernel.py"]},
                        "candidate_path": str(kernel_path),
                    }
                ),
                encoding="utf-8",
            )
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                """#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

results = Path("workspace/kb_active/results.json")
results.parent.mkdir(parents=True, exist_ok=True)
results.write_text("{}", encoding="utf-8")
sys.stdin.read()
""",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)

            env = {
                **os.environ,
                "TREE_PROMPT_PATH": str(prompt_path),
                "TREE_PAYLOAD_PATH": str(payload_path),
                "TREE_OUTPUT_PATH": str(output_path),
                "PYTHONPATH": str(Path.cwd()),
            }
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path.cwd() / "scripts" / "codex_tree_agent.py"),
                    "execute",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=str(sandbox),
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertFalse(output["changed"])
            self.assertEqual(output["metadata"]["changed_paths"], [])


if __name__ == "__main__":
    unittest.main()
