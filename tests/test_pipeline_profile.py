from __future__ import annotations

import unittest

from extract import generate_kernel_file
from pipeline_profile import build_pipeline_profile


class PipelineProfileTests(unittest.TestCase):
    def test_build_pipeline_profile_emits_region_and_kernel_hints(self) -> None:
        report = {
            "model": "demo-model",
            "gpu_name": "H100",
            "top_kernels": [
                {
                    "rank": 1,
                    "name": "aten::mm",
                    "op_type": "matmul",
                    "shape_info": "M=4096, N=4096, K=4096",
                    "pct_total": 12.5,
                    "roofline": "memory-bound",
                    "autokernel_supported": True,
                },
                {
                    "rank": 2,
                    "name": "aten::softmax",
                    "op_type": "softmax",
                    "shape_info": "rows=4096, cols=4096",
                    "pct_total": 3.2,
                    "roofline": "memory-bound",
                    "autokernel_supported": True,
                },
                {
                    "rank": 3,
                    "name": "aten::layer_norm",
                    "op_type": "layernorm",
                    "shape_info": "rows=4096, cols=4096",
                    "pct_total": 2.8,
                    "roofline": "compute-bound",
                    "autokernel_supported": True,
                },
            ],
        }

        profile = build_pipeline_profile(report)

        self.assertEqual(profile["focus_region_id"], "region-1")
        self.assertEqual(len(profile["regions"]), 3)
        self.assertEqual(profile["kernel_hints"]["1"]["optimization_scope"], "pipeline")
        self.assertEqual(profile["kernel_hints"]["1"]["pipeline_region_label"], "matmul region")
        self.assertIn("memory", profile["kernel_hints"]["1"]["profile_bottlenecks"])
        self.assertIn("boundary", profile["kernel_hints"]["2"]["profile_bottlenecks"])
        self.assertIn("layout", profile["kernel_hints"]["3"]["profile_bottlenecks"])

    def test_generate_kernel_file_includes_pipeline_hint_header(self) -> None:
        starter_code = "\n".join(
            [
                'KERNEL_TYPE = "matmul"',
                "",
                "def kernel(x=None):",
                "    return x",
            ]
        )

        content = generate_kernel_file(
            op_type="matmul",
            rank=1,
            pct_total=18.5,
            model_shape={"M": 4096, "N": 4096, "K": 4096},
            model_name="demo-model",
            gpu_time_ms=10.0,
            starter_code=starter_code,
            pipeline_hint={
                "optimization_scope": "pipeline",
                "pipeline_region_label": "attention block",
                "upstream_regions": ["qkv projection"],
                "downstream_regions": ["attention output"],
                "profile_bottlenecks": ["launch", "boundary"],
            },
        )

        self.assertIn("Optimization scope: pipeline", content)
        self.assertIn("Pipeline region: attention block", content)
        self.assertIn("Upstream regions: qkv projection", content)
        self.assertIn("Downstream regions: attention output", content)
        self.assertIn("Profile bottlenecks: launch, boundary", content)


if __name__ == "__main__":
    unittest.main()
