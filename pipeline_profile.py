#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR / "workspace"
DEFAULT_REPORT_PATH = WORKSPACE_DIR / "profile_report.json"
DEFAULT_OUTPUT_PATH = WORKSPACE_DIR / "pipeline_profile.json"


def load_profile_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_pipeline_profile(profile: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2)


def pipeline_profile_path_for_report(report_path: Path) -> Path:
    return report_path.resolve().with_name("pipeline_profile.json")


def _shape_key(kernel: dict[str, Any]) -> str:
    return str(kernel.get("shape_info") or kernel.get("shape") or "").strip()


def _region_label(op_type: str, shape_key: str) -> str:
    if shape_key:
        return f"{op_type} region"
    return op_type


def _build_regions(kernels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for kernel in kernels:
        op_type = str(kernel.get("op_type", "unknown"))
        shape_key = _shape_key(kernel)
        key = (op_type, shape_key)
        region = grouped.setdefault(
            key,
            {
                "id": f"region-{len(grouped) + 1}",
                "label": _region_label(op_type, shape_key),
                "shape_key": shape_key,
                "op_types": set(),
                "kernel_ranks": [],
                "kernel_names": [],
                "pct_total": 0.0,
                "rooflines": [],
                "first_rank": int(kernel.get("rank", 10**9)),
            },
        )
        region["op_types"].add(op_type)
        region["kernel_ranks"].append(int(kernel.get("rank", 0)))
        region["kernel_names"].append(str(kernel.get("name", op_type)))
        region["pct_total"] += float(kernel.get("pct_total", 0.0) or 0.0)
        region["rooflines"].append(str(kernel.get("roofline", "")).lower())
        region["first_rank"] = min(region["first_rank"], int(kernel.get("rank", 10**9)))

    regions = list(grouped.values())
    regions.sort(key=lambda region: region["first_rank"])
    for region in regions:
        region["op_types"] = sorted(region["op_types"])
        region["kernel_ranks"] = sorted(region["kernel_ranks"])
    return regions


def _is_pipeline_pattern(kernels: list[dict[str, Any]], regions: list[dict[str, Any]]) -> bool:
    many_small = [kernel for kernel in kernels if float(kernel.get("pct_total", 0.0) or 0.0) <= 4.0]
    shared_shape = any(
        region["shape_key"] and sum(1 for other in regions if other["shape_key"] == region["shape_key"]) > 1
        for region in regions
    )
    return (len(many_small) >= 3 and sum(float(kernel.get("pct_total", 0.0) or 0.0) for kernel in many_small) >= 8.0) or shared_shape


def _infer_region_scope(region: dict[str, Any], focus_region: dict[str, Any], pipeline_mode: bool) -> str:
    if pipeline_mode and region["id"] == focus_region["id"]:
        return "pipeline"
    if len(region["kernel_ranks"]) > 1:
        return "region"
    return "kernel"


def _infer_region_bottlenecks(
    region: dict[str, Any],
    regions: list[dict[str, Any]],
    kernels: list[dict[str, Any]],
) -> list[str]:
    hints: list[str] = []
    rooflines = " ".join(region["rooflines"])
    if "memory" in rooflines:
        hints.append("memory")
    elif "compute" in rooflines:
        hints.append("compute")

    if len(region["kernel_ranks"]) > 1:
        hints.append("launch")

    if region["shape_key"] and sum(1 for other in regions if other["shape_key"] == region["shape_key"]) > 1:
        hints.extend(["boundary", "layout"])

    small_kernels = [
        kernel for kernel in kernels
        if int(kernel.get("rank", 10**9)) in region["kernel_ranks"]
        and float(kernel.get("pct_total", 0.0) or 0.0) <= 4.0
    ]
    if len(small_kernels) >= 1 and len(region["kernel_ranks"]) == 1 and float(region["pct_total"]) <= 4.0:
        hints.append("launch")

    normalized: list[str] = []
    for hint in hints:
        if hint not in normalized:
            normalized.append(hint)
    return normalized or ["compute"]


def _nearby_region_labels(region: dict[str, Any], regions: list[dict[str, Any]], *, direction: str) -> list[str]:
    index = next((idx for idx, item in enumerate(regions) if item["id"] == region["id"]), -1)
    if index < 0:
        return []
    if direction == "upstream":
        nearby = regions[max(0, index - 2):index]
    else:
        nearby = regions[index + 1:index + 3]
    return [item["label"] for item in nearby]


def _region_summary(region: dict[str, Any]) -> str:
    bottlenecks = ", ".join(region["profile_bottlenecks"])
    return (
        f"Ranks {region['kernel_ranks']} account for {region['pct_total']:.1f}% of profiled GPU time; "
        f"current profile hints point to {bottlenecks}."
    )


def build_pipeline_profile(report: dict[str, Any], *, max_kernels: int = 12) -> dict[str, Any]:
    top_kernels = report.get("top_kernels", [])
    supported = [
        kernel for kernel in top_kernels
        if isinstance(kernel, dict) and kernel.get("autokernel_supported", False)
    ][:max_kernels]
    if not supported:
        return {
            "model": report.get("model"),
            "gpu_name": report.get("gpu_name"),
            "focus_region_id": "",
            "regions": [],
            "kernel_hints": {},
        }

    regions = _build_regions(supported)
    focus_region = max(regions, key=lambda region: (region["pct_total"], -region["first_rank"]))
    pipeline_mode = _is_pipeline_pattern(supported, regions)

    for region in regions:
        region["scope"] = _infer_region_scope(region, focus_region, pipeline_mode)
        region["profile_bottlenecks"] = _infer_region_bottlenecks(region, regions, supported)
        region["upstream_regions"] = _nearby_region_labels(region, regions, direction="upstream")
        region["downstream_regions"] = _nearby_region_labels(region, regions, direction="downstream")
        region["summary"] = _region_summary(region)

    kernel_hints: dict[str, dict[str, Any]] = {}
    for region in regions:
        hint = {
            "optimization_scope": region["scope"],
            "pipeline_region_label": region["label"],
            "upstream_regions": region["upstream_regions"],
            "downstream_regions": region["downstream_regions"],
            "profile_bottlenecks": region["profile_bottlenecks"],
        }
        for rank in region["kernel_ranks"]:
            kernel_hints[str(rank)] = hint

    return {
        "model": report.get("model"),
        "gpu_name": report.get("gpu_name"),
        "focus_region_id": focus_region["id"],
        "regions": [
            {
                "id": region["id"],
                "label": region["label"],
                "scope": region["scope"],
                "kernel_ranks": region["kernel_ranks"],
                "op_types": region["op_types"],
                "pct_total": round(region["pct_total"], 1),
                "profile_bottlenecks": region["profile_bottlenecks"],
                "upstream_regions": region["upstream_regions"],
                "downstream_regions": region["downstream_regions"],
                "summary": region["summary"],
            }
            for region in regions
        ],
        "kernel_hints": kernel_hints,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a lightweight pipeline profile from profile_report.json.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="Path to profile_report.json")
    parser.add_argument("--output", default=None, help="Path to write pipeline_profile.json")
    parser.add_argument("--max-kernels", type=int, default=12, help="Maximum number of top kernels to consider")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_path = Path(args.report)
    output_path = Path(args.output) if args.output else pipeline_profile_path_for_report(report_path)
    report = load_profile_report(report_path)
    pipeline_profile = build_pipeline_profile(report, max_kernels=args.max_kernels)
    save_pipeline_profile(pipeline_profile, output_path)
    print(f"Pipeline profile saved to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
