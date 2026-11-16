"""CLI option definitions and progressive help, separate from execution."""
from __future__ import annotations

import argparse

from autopipeline.search.roles import add_role_config_args
from .workloads import EXAMPLES


WORKLOAD_HELP = """\
AutoPipeline workload commands

  examples                          Choose a workload and check resources
  init DIRECTORY --device cpu       Create a workload (otherwise defaults to CUDA)
  check --spec FILE                 Check files and device; no model execution
  evaluate --spec FILE              Execute and verify without an agent
  run --spec FILE                   Search for optimizations with your configured agent
  export RUN_DIR --output-dir DIR   Export verified code for rechecking

Start with init, check, and evaluate; run requires an agent backend.
init/check do not call an agent or download weights. evaluate executes local code.
run uses agent service quota; call limits are not spending limits.
View results with: autopipeline report RUN_DIR

Options: autopipeline pipeline COMMAND --help
Full options: autopipeline pipeline COMMAND --help-all
Developer utilities: autopipeline pipeline --help-all
"""


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    def _get_help_string(self, action):
        if action.default is None:
            return action.help
        return super()._get_help_string(action)


def build_parser(*, show_all: bool = False):
    parser = argparse.ArgumentParser(prog="autopipeline pipeline", description="Create, verify, optimize, and export inference workloads.")
    commands = parser.add_subparsers(dest="command", required=True)

    def sub(name, description, epilog=""):
        p = commands.add_parser(name, help=description, description=description,
                                formatter_class=HelpFormatter, epilog=epilog)
        p.add_argument("--help-all", action="help", help="Show all options, including advanced integration settings")
        return p

    sub("examples", "List included workloads and their resource requirements")
    init = sub("init", "Create a workload; does not execute code, download weights, or call an agent.",
               "Start on CPU: autopipeline pipeline init my-workload --device cpu\n"
               "CUDA is the existing default; it never silently falls back to CPU.\n"
               "The new directory contains its own README with the next steps.")
    init.add_argument("directory", help="New project directory; existing files are never overwritten")
    init.add_argument("--source", help="Your directory containing reference.py and optional candidate.py")
    init.add_argument("--example", choices=list(EXAMPLES), default="residual-mlp", help="Built-in workload")
    init.add_argument("--model-dir", help="Existing local encoder weights for hf-local; never downloads")
    init.add_argument("--device", choices=["cpu", "cuda"], default="cuda", help="Target device; choose cpu without an NVIDIA GPU")
    init.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], default="float32", help="Fixed workload numeric type")
    for name in ("check", "evaluate", "run"):
        description = {"check": "Check configuration and device; no model execution or agent service call.",
                       "evaluate": "Execute local reference and candidate code, then independently verify; no agent call.",
                       "run": "Search multiple PyTorch implementations with your agent and independently verify results."}[name]
        epilog = "Existing input files and run directories are never overwritten.\n"
        if name == "check":
            epilog += "A passed check does not prove model execution or agent authentication.\nNext: autopipeline pipeline evaluate --spec FILE"
        elif name == "evaluate":
            epilog += "An unchanged baseline can pass; verified does not mean faster.\nNext: autopipeline report RUN_DIR"
        else:
            epilog += (
                "May contact the configured agent service and use quota. Call limits are not spending limits.\n"
                "Executes local code; workspace copies are not an operating-system security sandbox.\n"
                "An invalid baseline prevents agent calls. No speedup is guaranteed.\n"
                "First run:\n  autopipeline pipeline run --spec FILE --executor-backend codex-app-server \\\n"
                "    --max-rounds 2 --beam-width 2 --children-per-expansion 2 --max-agent-calls 4 --max-probe-calls 4\n"
                "Advanced roles and integration: autopipeline pipeline run --help-all")
        p = sub(name, description, epilog)
        p.add_argument("--spec", required=True, help="pipeline.json created by init")
        if name == "check":
            p.add_argument("--no-runtime", action="store_true", help="Validate files/config only; does not assert device readiness")
            p.add_argument("--json", action="store_true", help="Print the machine-readable readiness result")
        else:
            p.add_argument("--run-dir", help="New output directory; omitted: timestamped directory beside the spec")
            p.add_argument("--run-name", help="Optional display name")
            p.add_argument("--candidate", help="Candidate workspace to evaluate/start from against the fixed reference")
        if name in {"check", "run"}:
            connection = p.add_argument_group("Agent connection")
            connection.add_argument("--executor-command", help="Command for another coding agent; requires its own authentication")
            add_role_config_args(connection)
            visible = {"executor_backend": "Agent backend; use codex-app-server after authenticating on this machine",
                       "executor_model": "Optional coding model override; otherwise use the backend default"}
            for action in connection._group_actions:
                if action.dest == "executor_command":
                    continue
                action.help = visible.get(action.dest, action.dest.replace("_", " ") if show_all else argparse.SUPPRESS)
        if name == "run":
            budget = p.add_argument_group("Search and call limits")
            for flag, default, help_text in (
                ("beam-width", 2, "Maximum branches retained after each round"),
                ("max-rounds", 3, "Maximum search rounds"),
                ("children-per-expansion", 2, "Candidate attempts per expansion"),
                ("max-agent-calls", 12, "Maximum agent calls; not a monetary cap"),
                ("max-probe-calls", 12, "Maximum active evidence probes"),
            ):
                budget.add_argument(f"--{flag}", type=int, default=default, help=help_text)
            budget.add_argument("--command-timeout-seconds", type=float, default=600, help="Per-agent-command timeout; evaluator timeout is configured in the spec")
            advanced = p.add_argument_group("Advanced integration")
            for flag in ("planner-command", "reflector-command"):
                advanced.add_argument(f"--{flag}", help="Optional role command" if show_all else argparse.SUPPRESS)
            advanced.add_argument("--planner-handoff-mode", choices=["idea", "variant", "detailed"], default="variant", help="Planner handoff format" if show_all else argparse.SUPPRESS)
            advanced.add_argument("--max-parallel-branches", type=int, choices=[1], default=1, help="Serial measurements only" if show_all else argparse.SUPPRESS)
            advanced.add_argument("--branch-resource-policy", choices=["conservative"], default="conservative", help="Branch resource policy" if show_all else argparse.SUPPRESS)
    export = sub("export", "Export a verified candidate, fixed reference, and validation records.",
                 "Does not deploy code or copy external model weights. Reinstall workload dependencies before rechecking.")
    export.add_argument("run_dir", help="Run directory printed by evaluate or run")
    export.add_argument("--output-dir", required=True, help="New export directory outside the run directory")
    info = sub("info", "Show a validated pipeline spec")
    info.add_argument("--spec", required=True)
    setup = sub("setup", "Copy a spec workspace to a new directory")
    setup.add_argument("--spec", required=True)
    setup.add_argument("--output-dir", required=True)
    smoke = sub("smoke", "Synthetic harness fixture only; not a model or performance experiment")
    smoke.add_argument("--run-dir", required=True)
    return parser
