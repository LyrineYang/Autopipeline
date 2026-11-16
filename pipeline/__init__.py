"""Source checkout aliases for pre-a7 imports. Use autopipeline.pipeline in new code."""

from importlib import import_module
import sys

from autopipeline import pipeline as _pipeline

__all__ = _pipeline.__all__

for _name in ("task", "region", "validation", "verification_backend"):
    _module = import_module(f"autopipeline.pipeline.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module


def __getattr__(name: str):
    return getattr(_pipeline, name)
