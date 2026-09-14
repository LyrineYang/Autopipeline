"""Generic pipeline task and bridge entry points."""

from .task import (
    PipelineSpec,
    PipelineTask,
    default_pipeline_run_dir,
    setup_pipeline_workspace,
)
from .verification_backend import ExecutableRunRequest, ExecutableVerificationBackend
from .region import BottleneckRegionContract

__all__ = [
    "BottleneckRegionContract",
    "ExecutableRunRequest",
    "ExecutableVerificationBackend",
    "PipelineSpec",
    "PipelineTask",
    "create_pipeline_smoke_fixture",
    "default_pipeline_run_dir",
    "run_pipeline_smoke",
    "setup_pipeline_workspace",
]


def __getattr__(name: str):
    if name in {"create_pipeline_smoke_fixture", "run_pipeline_smoke"}:
        from . import smoke
        return getattr(smoke, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
