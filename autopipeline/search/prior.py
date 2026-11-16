from __future__ import annotations

from typing import Any


KERNEL_OPTIMIZATION_PRIOR_SOURCE = "fastkernels_taxonomy_v1"

KERNEL_OPTIMIZATION_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "id": "memory_access",
        "summary": "Improve data movement across global memory, shared memory, registers, and cache.",
        "signals": ["low_bandwidth", "uncoalesced_access", "bank_conflict", "excess_load_store"],
        "aliases": ["memory_hierarchy"],
    },
    {
        "id": "latency_hiding_async",
        "summary": "Overlap slow memory or sync operations with useful work.",
        "signals": ["memory_latency", "sync_wait", "pipeline_stall"],
        "aliases": ["latency_hiding"],
    },
    {
        "id": "precision_datatype",
        "summary": "Use cheaper datatypes where correctness tolerance allows it.",
        "signals": ["bandwidth_bound", "tensor_core_available", "wide_dtype"],
        "aliases": ["dtype_precision"],
    },
    {
        "id": "compute_instruction",
        "summary": "Reduce instruction count or use specialized hardware instructions.",
        "signals": ["instruction_overhead", "low_compute_utilization", "expensive_math"],
        "aliases": ["instruction_selection"],
    },
    {
        "id": "parallelism_occupancy",
        "summary": "Improve active work distribution, occupancy, launch geometry, or warp use.",
        "signals": ["low_occupancy", "under_parallelized", "launch_geometry"],
        "aliases": ["parallelization", "occupancy_launch"],
    },
    {
        "id": "control_flow_loop",
        "summary": "Reduce loop, branch, indexing, or specialization overhead.",
        "signals": ["branch_divergence", "loop_overhead", "indexing_overhead"],
        "aliases": ["control_flow_specialization"],
    },
)


def kernel_optimization_prior() -> dict[str, Any]:
    return {
        "source": KERNEL_OPTIMIZATION_PRIOR_SOURCE,
        "families": [dict(item) for item in KERNEL_OPTIMIZATION_FAMILIES],
        "handoff": {
            "idea": "Planner states the natural-language optimization idea and evidence.",
            "variant": "Planner may name a short implementation variant, but executor owns local code details.",
        },
    }


def kernel_prior_family_ids(*, include_aliases: bool = False) -> list[str]:
    families: list[str] = []
    for item in KERNEL_OPTIMIZATION_FAMILIES:
        families.append(str(item["id"]))
        if include_aliases:
            families.extend(str(alias) for alias in item.get("aliases", []))
    return families
