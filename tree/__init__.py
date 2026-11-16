"""Source checkout aliases for pre-a7 imports. Use autopipeline.search in new code."""

from importlib import import_module
import sys

from autopipeline.search import *  # noqa: F403
from autopipeline.search import __all__ as _exports

_modules = {
    name: f"autopipeline.search.{name}"
    for name in (
        "agent", "agent_backend", "archive", "branch_engine", "budget",
        "codex_app_server", "codex_dynamic_tools", "codex_session", "command",
        "planner", "prior", "prompts", "roles", "sensing", "task", "types", "viewer",
    )
}
_modules.update({
    "search": "autopipeline.search.engine",
    "verifier": "autopipeline.search.feedback",
    "autokernel": "kernel_tools.task",
    "autokernel_probes": "kernel_tools.probes",
})
for _name, _target in _modules.items():
    _module = import_module(_target)
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

from autopipeline.search.agent import LocalAgentAdapter as NoOpAgentAdapter
from autopipeline.search.feedback import build_agent_feedback as build_verifier_summary

agent.NoOpAgentAdapter = NoOpAgentAdapter
verifier.build_verifier_summary = build_verifier_summary
__all__ = [*_exports, "NoOpAgentAdapter", "build_verifier_summary"]
