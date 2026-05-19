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
    "report": ("scripts/tree_report.py", "Summarize a tree run directory."),
}


HELP = """\
AutoPipeline command line

Usage:
  autopipeline doctor [--no-gpu] [--json]
  autopipeline prepare [args...]
  autopipeline profile [args...]
  autopipeline extract [args...]
  autopipeline tree run [args...]
  autopipeline report [args...]
  autopipeline bench [args...]
  autopipeline verify [args...]

Primary flow:
  autopipeline doctor
  autopipeline prepare
  autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
  autopipeline extract --top 1
  autopipeline tree run --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick

Compatibility:
  Existing top-level scripts still work, but new user docs should prefer this CLI.
"""


TREE_HELP = """\
AutoPipeline tree commands

Usage:
  autopipeline tree run [tree_optimize.py args...]

Example:
  autopipeline tree run --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick
"""


def _forwarded_help(command: str, relative_path: str, description: str) -> str:
    return f"""\
autopipeline {command}

{description}

Arguments are forwarded to `{relative_path}` inside the source checkout.
Run this command after `uv sync` so optional runtime dependencies are present.
"""


def _find_source_root() -> Path:
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        current = start if start.is_dir() else start.parent
        for candidate in [current, *current.parents]:
            if (candidate / "pyproject.toml").is_file() and (candidate / "README.md").is_file():
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

    ok = checks["has_pyproject"] and checks["has_readme"]
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
    if not args or args[0] in {"-h", "--help"}:
        print(HELP)
        return 0

    command = args[0]
    rest = args[1:]

    if command == "doctor":
        return _doctor(rest)

    if command == "tree":
        if not rest or rest[0] in {"-h", "--help"}:
            print(TREE_HELP)
            return 0
        if rest[0] != "run":
            print(f"ERROR: unknown tree command: {rest[0]}", file=sys.stderr)
            return 2
        if len(rest) > 1 and rest[1] in {"-h", "--help"}:
            print(_forwarded_help("tree run", "tree_optimize.py", "Run branch-based tree search."))
            return 0
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
