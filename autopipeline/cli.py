from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from typing import Any, Iterable


COMMANDS = {
    "prepare": ("prepare.py", "Prepare cache data and benchmark baselines."),
    "profile": ("profile.py", "Profile a model and write workspace/profile_report.json."),
    "extract": ("extract.py", "Extract starter kernels from a profile report."),
    "bench": ("bench.py", "Benchmark the current kernel.py candidate."),
    "verify": ("verify.py", "Run end-to-end verification."),
}


HELP = """\
AutoPipeline — optimize and verify model implementations

Workflow:
  autopipeline pipeline examples                 Choose a workload
  autopipeline pipeline init DIRECTORY --device cpu
  autopipeline pipeline check --spec DIRECTORY/pipeline.json
  autopipeline pipeline evaluate --spec DIRECTORY/pipeline.json
  autopipeline pipeline run --spec DIRECTORY/pipeline.json --executor-backend codex-app-server
  autopipeline report RUN_DIR                    View results
  autopipeline pipeline export RUN_DIR --output-dir DIRECTORY

init/check do not execute the model or call an agent. evaluate executes local code.
run may contact your agent service and use quota; call limits are not spending limits.
Start on CPU without a GPU. Verified results do not guarantee a speedup.

Help:
  autopipeline pipeline --help                   Workload commands
  autopipeline pipeline COMMAND --help           Command options
  autopipeline pipeline COMMAND --help-all       Advanced options and integration
  autopipeline --version                         Installed version
  autopipeline --help-all                        Developer and compatibility tools
"""

DEVELOPER_HELP = """\
Developer and compatibility tools:
  autopipeline doctor [--no-gpu] [--json]          Environment diagnostics
  autopipeline pipeline --help-all               Spec utilities and synthetic smoke
  autopipeline kernel run [args...]              Extracted-kernel search from source
  autopipeline prepare [args...]
  autopipeline profile [args...]
  autopipeline extract [args...]
  autopipeline bench [args...]
  autopipeline verify [args...]

Kernel compatibility tools require a source checkout and GPU dependencies.
The old autopipeline tree run command is an alias for autopipeline kernel run.
Existing commands and top-level scripts remain supported.
Developer guide: docs/dev/README.md
"""


TREE_HELP = """\
AutoPipeline extracted-kernel commands

Usage:
  autopipeline kernel run [tree_optimize.py args...]
  autopipeline tree run [args...]                 Compatibility alias

Example:
  autopipeline kernel run --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick --executor-backend codex-app-server
"""


def _forwarded_help(command: str, relative_path: str, description: str) -> str:
    return f"""\
autopipeline {command}

{description}

Arguments are forwarded to `{relative_path}` inside the source checkout.
Run this command after `uv sync --extra gpu` so runtime dependencies are present.
"""


def _find_source_root() -> Path:
    starts = [Path(__file__).resolve(), Path.cwd()]
    for start in starts:
        current = start if start.is_dir() else start.parent
        for candidate in [current, *current.parents]:
            if (candidate / "tree_optimize.py").is_file() and (candidate / "autopipeline" / "cli.py").is_file():
                return candidate
    return Path.cwd()


def _script_path(relative_path: str) -> Path:
    return _find_source_root() / relative_path


def _run_source_script(relative_path: str, args: Iterable[str]) -> int:
    path = _script_path(relative_path)
    if not path.is_file():
        print(
            f"ERROR: {relative_path} was not found. This command requires an AutoPipeline source checkout.",
            file=sys.stderr,
        )
        return 2

    repo_root = str(path.parent if "/" not in relative_path else _find_source_root())
    old_argv = sys.argv[:]
    old_path = sys.path[:]
    try:
        sys.argv = [str(path), *list(args)]
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        runpy.run_path(str(path), run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    finally:
        sys.argv = old_argv
        sys.path = old_path


def _optional_module(name: str) -> tuple[bool, str | None]:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False, None
    try:
        module = __import__(name)
    except Exception as exc:
        return False, f"import failed: {exc}"
    return True, getattr(module, "__version__", None)


def _doctor(argv: list[str]) -> int:
    require_gpu = True
    as_json = False
    for arg in argv:
        if arg == "--no-gpu":
            require_gpu = False
        elif arg == "--json":
            as_json = True
        elif arg in {"-h", "--help"}:
            print("Usage: autopipeline doctor [--no-gpu] [--json]")
            return 0
        else:
            print(f"ERROR: unknown doctor option: {arg}", file=sys.stderr)
            return 2

    repo_root = _find_source_root()
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "source_root": str(repo_root),
        "has_pyproject": (repo_root / "pyproject.toml").is_file(),
        "has_readme": (repo_root / "README.md").is_file(),
        "has_workspace": (repo_root / "workspace").exists(),
    }

    torch_ok, torch_version = _optional_module("torch")
    checks["torch"] = {"available": torch_ok, "version": torch_version}
    cuda_available = False
    gpu_name = None
    if torch_ok:
        try:
            import torch

            cuda_available = bool(torch.cuda.is_available())
            if cuda_available:
                gpu_name = torch.cuda.get_device_name(torch.cuda.current_device())
        except Exception as exc:
            checks["torch"]["error"] = str(exc)

    checks["cuda"] = {"available": cuda_available, "gpu": gpu_name}
    triton_ok, triton_version = _optional_module("triton")
    checks["triton"] = {"available": triton_ok, "version": triton_version}

    # The installed pipeline runtime works outside a source checkout.
    ok = importlib.util.find_spec("autopipeline.pipeline") is not None and importlib.util.find_spec("autopipeline.search") is not None
    if require_gpu:
        ok = ok and torch_ok and triton_ok and cuda_available

    checks["ok"] = bool(ok)

    if as_json:
        print(json.dumps(checks, indent=2, sort_keys=True))
    else:
        print("AutoPipeline doctor")
        print(f"  source root: {checks['source_root']}")
        print(f"  Python: {checks['python']}")
        print(f"  torch: {'ok ' + str(torch_version) if torch_ok else 'missing'}")
        print(f"  triton: {'ok ' + str(triton_version) if triton_ok else 'missing'}")
        print(f"  CUDA: {'ok ' + str(gpu_name) if cuda_available else 'not available'}")
        if not require_gpu and not cuda_available:
            print("  GPU requirement: skipped")
        print(f"  status: {'ok' if ok else 'failed'}")

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        from . import __version__
        print(__version__)
        return 0
    if not args or args[0] in {"-h", "--help"}:
        print(HELP)
        return 0

    if args == ["--help-all"]:
        print(HELP + "\n" + DEVELOPER_HELP)
        return 0

    command = args[0]
    rest = args[1:]

    if command == "doctor":
        return _doctor(rest)

    if command == "pipeline":
        from .pipeline_cli import main as pipeline_main
        return pipeline_main(rest)

    if command == "report":
        from .report import main as report_main
        return report_main(rest)

    if command in {"kernel", "tree"}:
        if not rest or rest[0] in {"-h", "--help"}:
            print(TREE_HELP)
            return 0
        if rest[0] != "run":
            print(f"ERROR: unknown kernel command: {rest[0]}", file=sys.stderr)
            return 2
        return _run_source_script("tree_optimize.py", rest[1:])

    if command in COMMANDS:
        if rest and rest[0] in {"-h", "--help"}:
            relative_path, description = COMMANDS[command]
            print(_forwarded_help(command, relative_path, description))
            return 0
        return _run_source_script(COMMANDS[command][0], rest)

    print(f"ERROR: unknown command: {command}", file=sys.stderr)
    print("Run `autopipeline --help` for available commands.", file=sys.stderr)
    return 2
