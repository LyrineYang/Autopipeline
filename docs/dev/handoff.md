# AutoPipeline Handoff

## Resume Here

Use `LyrineYang/Autopipeline` on `main` with AutoPipeline 1.4.0a7.
Use an authenticated GitHub account with repository access. Record the checkout SHA in each experiment. Level 5 and apimetric research are maintained
separately from the product. [Source repository](https://github.com/LyrineYang/Autopipeline)

```sh
git clone --branch main https://github.com/LyrineYang/Autopipeline.git autopipeline
cd autopipeline
git rev-parse HEAD
```

The next task is an equal-budget search comparison on an RTX 4060 using a small, fixed-shape,
one-step image generation workload. Tree search is the product's default. Keep the frozen
reference, correctness checks, profiler, budget accounting, and final recheck in the experiment.

## Evidence Already Available

- The a6 CPU baseline was verified from an installed wheel outside the checkout. The a7
  acceptance path exercises the same baseline, report, export, recheck, and rejection flow.
- A controlled two-round acceptance run used four local executor calls. Both surviving parents
  received a second-round trial, and the exported candidate passed rechecking.
- A prior real-agent CPU pilot used three executor calls per arm. All six candidates passed
  correctness. Neither first-round tree candidate advanced, so the pilot measured root-level
  branching only. Repeated measurements did not establish a consistent speedup.

| CPU pilot arm | Executor calls | Wall time | Total tokens | Cached input tokens |
| --- | ---: | ---: | ---: | ---: |
| Single path | 3 | 232.8 s | 573,227 | 453,376 |
| Tree search | 3 | 243.9 s | 646,679 | 527,744 |

The a6 pilot recorded different performance verdicts for byte-identical candidates. In a7 the
native workflow ranks qualifying candidates by paired speedup from each reference/candidate
comparison. Measure baseline variability and interleave finalist rechecks on the same GPU.
[Search engine](../../autopipeline/search/engine.py) · [Native evaluator](../../autopipeline/evaluate.py)

Local pilot outputs and release builds lived under the ignored `workspace/` directory.
A fresh clone can build the alpha wheel with `python -m pip wheel . --no-deps -w dist`.
The installed acceptance entrypoint is [check_installed_workflow.py](../../scripts/check_installed_workflow.py).

## RTX 4060 Preflight

Reuse a CUDA-enabled PyTorch environment or install one for the host driver. In that environment,
install the product from the checkout with `python -m pip install .`.

```sh
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
python -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())'
autopipeline --version

autopipeline pipeline init workspace/4060-attention --example attention --device cuda
autopipeline pipeline check --spec workspace/4060-attention/pipeline.json
autopipeline pipeline evaluate --spec workspace/4060-attention/pipeline.json \
  --run-dir workspace/4060-attention/runs/baseline
autopipeline report workspace/4060-attention/runs/baseline
```

Check the numerical verdict, latency samples, and operator profile. Save the driver, PyTorch
version, and memory reading. The native evaluator holds the reference and candidate models during
correctness checks, so check fit in the evaluator before running agent trials. Then authenticate
Codex CLI on the execution machine and verify the login. [Agent setup](../quickstart.md#connect-a-coding-agent)

## Run the Comparison

1. Pin model code and weight revisions, resolution, dtype, batch size, inputs, and check seeds.
   Keep a single spec for all arms. Record which part of image generation is inside
   `forward()`. Time the same work in every arm.
2. Measure repeated baseline runs on an otherwise idle GPU. Interleave fresh measurements of the
   reference and finalists, and record variability alongside median latency. Define the minimum
   meaningful improvement before search. Keep byte-identical finalists as a single comparison.
3. Run the three arms below with equal call and probe caps and the same agent model and reasoning
   effort. Start with the four-call configuration to confirm the workflow. Expand the budget only
   after confirming runtime and memory. Use a distinct run directory for each execution.

| Arm | Starting configuration |
| --- | --- |
| Single path | `--beam-width 1 --children-per-expansion 1 --max-rounds 4` with 4 calls. |
| Independent proposals | Two fresh runs from the same reference. Use `--beam-width 1 --children-per-expansion 2 --max-rounds 1` with 2 calls per run. Recheck their finalists under the same conditions. |
| Multi-parent tree | `--beam-width 2 --children-per-expansion 2 --max-rounds 2` with 4 calls. Check whether two distinct parents actually expanded. |

For each arm, report the correctness verdicts, final latency samples and median, peak allocated
GPU memory, end-to-end experiment time, actual agent calls, and tokens including cached input.
Keep the report, receipt, environment record, verification logs and finalist hashes with the run.
If the tree run has just one surviving parent, record it as a branching run without a multi-parent
comparison. [Run artifacts](run-artifacts.md)
