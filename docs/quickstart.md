# Quick Start

The pipeline runtime can be installed into an environment and used from any
working directory. Historical AutoKernel scripts require the source checkout.

## Install

```sh
python -m pip install .
autopipeline pipeline --help
```

Add `.[cpu]` for CPU PyTorch, or `.[gpu]` for the compute stack on an NVIDIA Linux
server. Select a CUDA PyTorch build compatible with the server's driver. The uv
configuration selects CUDA 12.8 wheels on Linux; `uv sync --extra gpu` uses it.
If PyTorch is already managed by your server environment, install the base
package and only add missing workload dependencies. No model weights or API
credentials are bundled. Wheels can be installed directly with `pip install PATH.whl`.

## Create a workload

Run `autopipeline pipeline examples` for the installed catalog. Select
`--example attention` for a download-free Transformer block, or `--example hf-local
--model-dir PATH` for an existing local pretrained encoder. The default is
`residual-mlp`. See [usage scenarios and model resources](../examples/README.md)
for complete examples. Example source files are included in the wheel.


```sh
autopipeline pipeline init my-pipeline
# Or bring your own source directory:
autopipeline pipeline init my-model --source path/to/model-workspace
```

Default device is CUDA, default dtype is float32. To exercise the product on a
CPU, initialize a separate workload with `--device cpu`. Device choice is fixed
in `workload/workload.json`; CUDA never silently falls back to CPU.

The source directory contains:

| File | Contract |
| --- | --- |
| `reference.py` | `Model(torch.nn.Module)`, `get_inputs()`, optional `get_init_inputs()` |
| `candidate.py` | `ModelNew` with the same constructor and state-dict keys; generated as a subclass if absent |
| Other local files | Model helpers and local assets; copied as fixed reference files |

`get_inputs()` returns positional arguments as a list/tuple or keyword arguments
as a dictionary. Inputs may contain nested tensors; floating inputs use the
configured dtype while integer inputs preserve theirs. Outputs may be tensors,
lists, tuples, or dictionaries. All output tensors are checked, not a sample or
checksum. Integer outputs require exact equality; floating outputs use
`abs(candidate-reference) <= atol + rtol*abs(reference)` element by element.
Non-finite outputs, incompatible state dicts, and changed output structures fail.
At least one non-empty output tensor is required. Complex tensors and custom
streaming/stateful protocols need a custom evaluator.

Only `candidate.py` is editable by default. For a multi-file implementation,
update `editable_paths` in `pipeline.json` before running; keep reference, input,
and evaluation files fixed. Workspaces must contain regular files, not symlinks.
They are copied for isolation, so keep caches, environments, and unrelated files
outside the workload. Large external model assets should be loaded from a fixed
local path recorded in your workload documentation. Candidates execute as local
code under the invoking account; copied workspaces are not an OS security boundary.

Adjust dtype, warmup, timed repetitions, and input seeds in `workload.json` before
running. If changing tolerances, update `metadata.numerical_tolerances` in the spec
to match. Different input distributions, weights, dtypes, or tolerances define
different comparisons. This contract supports custom real models; the generated
small composed model is only the default onboarding workload.

## Check and evaluate before using an agent

```sh
autopipeline pipeline check --spec my-pipeline/pipeline.json --no-runtime
autopipeline pipeline check --spec my-pipeline/pipeline.json
autopipeline pipeline evaluate --spec my-pipeline/pipeline.json --run-dir my-pipeline/runs/baseline
```

`--no-runtime` validates configuration and files only. A full check verifies
PyTorch and the requested device without making an API call. Evaluation then
executes the reference and candidate, checks several seeded inputs, warms up,
measures median forward latency with device synchronization, and profiles in a
separate pass. Input construction and model loading are excluded from timing.
The baseline is a measurement; a noisy remeasurement of identical code does not
have to beat itself. Edited candidates must pass numerical checks and the
performance gate before selection. Final verification reruns the promoted code.
If a profiler fails, its reason is saved explicitly in metrics.

## Optimize

Install and authenticate Codex on the execution machine, then:

```sh
autopipeline pipeline run --spec my-pipeline/pipeline.json \
  --run-dir my-pipeline/runs/search \
  --executor-backend codex-app-server \
  --max-rounds 3 --children-per-expansion 2 --beam-width 1 \
  --max-agent-calls 12 --max-probe-calls 12
```

Only the executor uses a model in this example. Planner/reflector have heuristic
fallbacks; opt into model-backed roles with `--planner-backend codex-app-server`
and `--reflector-backend codex-app-server`. Role-specific model flags are optional.
The maximum agent-call count is a call budget, not a monetary spending cap.
Search stops at the configured limits. Measurements run serially on one device.

Other coding agents can use `--executor-command 'YOUR_COMMAND'`. The command runs
inside the candidate workspace, reads `TREE_PROMPT_PATH` / `TREE_PAYLOAD_PATH`,
edits declared files, and writes a JSON object to `TREE_OUTPUT_PATH`:

```json
{"status":"completed","changed":true,"summary":"Description of the implementation change"}
```

Use the interpreter/environment containing the installed library. Custom agent
commands own their provider authentication. See [Agent Contract](agent_contract.md)
for the full protocol. The built-in `pipeline smoke` command exercises a synthetic
harness fixture; it is not a replacement for model evaluation.

## Inspect, export, and retry

```sh
autopipeline report my-pipeline/runs/search
autopipeline pipeline export my-pipeline/runs/search --output-dir my-candidate
cd my-candidate
autopipeline pipeline evaluate --spec pipeline.json --candidate candidate --run-dir recheck
```

The export is source for integration, not an automatically deployed service.
Import `ModelNew` from its candidate workspace with the same weights and inputs.
The fixed reference remains separate when rechecking an export. External model
assets and custom evaluator dependencies must be present on the target machine.

Run artifacts include:

| Path | Purpose |
| --- | --- |
| `result.json` | Final status, selected candidate, file hashes, verification outcome |
| `input/` | Frozen source, spec, and hashes used for this run |
| `environment.json` | Python, platform, and installed package versions |
| `search_state.json`, `tree_events.jsonl` | Search progress and budget accounting |
| `nodes/*/agent/` | Agent request, output, timeout/failure diagnostics |
| `nodes/*/verification/` | Reference/candidate metrics and evaluator stdout/stderr |
| `promoted_pipeline/` | Verified selected implementation, possibly unchanged baseline |
| `promoted_pipeline/.autopipeline/` | Actual profiler trace and operator summary |
| `final_verification/` | Independent final measurements and verification report |
| `view/dashboard.html`, `view/summary.md` | Human-readable run report |

The CLI exits 0 for a completed evaluation or search, including a clearly labelled
no-improvement search. It exits 1 for runtime/evaluation/agent failure, 2 for
configuration/path errors, and 130 for interruption. Baseline failure prevents
paid agent calls. Total agent failure does not count as a successful optimization.
Never infer a speedup solely from exit code 0; inspect final metrics and status.

Run directories are never reused. Interrupted runs keep partial artifacts and a
status record. There is no full scheduler-state resume yet. Retry into a new run
directory, optionally with `--candidate PREVIOUS/promoted_pipeline`, keeping the
same reference spec. Exports reject source changed after verification.

## First server acceptance

The first server session should resolve these questions in this order:

1. **Can the actual CUDA environment run the library?** Install, run `check`, and
   evaluate the small generated workload. This isolates driver/compiler problems
   before involving model credentials or expensive workloads.
2. **Can the agent change real code and complete evaluation?** Run one round with
   one child and an authenticated executor. Inspect candidate diffs, failure
   logs, profiler availability, and final verification. A lack of speedup is a
   valid result; a no-op or repeated execution error needs repair.
3. **Does it help your workload?** Supply one representative local model, fixed
   weights/inputs, a tolerable search budget, and a latency objective. Repeat the
   baseline and candidate on an idle GPU to assess timing variation before
   expanding to longer searches or comparing optimization methods.

Minimum resources: one NVIDIA CUDA GPU capable of holding the selected workload,
Python environment access, local model assets if needed, and agent credentials
only for step 2 onward. No multi-GPU server or large pretrained model is needed
for the first installation check. If GPU resources remain unavailable, CPU
execution, package installation, numerical rejection, export/recheck, and agent
protocol tests remain useful; they do not predict CUDA optimization gains.

## Original AutoKernel path

Inside the source checkout with GPU dependencies installed:

```sh
uv run autopipeline prepare
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run autopipeline extract --top 1
uv run autopipeline tree run --kernel-file workspace/kernel_matmul_1.py \
  --beam-width 1 --max-rounds 1 --children-per-expansion 1 \
  --bench-arg=--quick --executor-backend codex-app-server
```

The new pipeline flow does not require profile/extract first: its editable
surface is the whole supplied model implementation, while the legacy flow edits
an extracted kernel.
