"""Generic pipeline task and bridge entry points."""

from .task import (
    PipelineSpec,
    PipelineTask,
    create_pipeline_smoke_fixture,
    default_pipeline_run_dir,
    run_pipeline_smoke,
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
