# Quickstart

Run a CPU evaluation first, then connect a coding agent. The built-in examples use random weights
and can be evaluated offline after installing dependencies. These examples use a macOS or Linux shell.

## Installation

Use Python 3.10 or later. Python 3.11 or later is recommended for a new environment.
In a source checkout, run the following.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[cpu]'
autopipeline --version
```

For an alpha wheel from the maintainers, replace the install line with the path to your file.

```sh
python -m pip install '/path/to/auto_pipeline-1.4.0a7-py3-none-any.whl[cpu]'
```

| Environment | What to install |
| --- | --- |
| CPU evaluation | The `[cpu]` extra installs PyTorch and NumPy. On Linux you can install CPU-only PyTorch first. |
| Existing PyTorch | Reuse the environment if PyTorch meets the package requirement. Install AutoPipeline and any workload dependencies there. |
| Local Transformers encoder | Install `[cpu,models]` and provide local model weights. |
| NVIDIA CUDA | Select a PyTorch build for your driver, then install AutoPipeline. See [NVIDIA preflight](#nvidia-preflight). |
| Configuration only | Install the base package and run `pipeline check --no-runtime`. |

The wheel provides the native pipeline, search engine, and report. The original AutoKernel and
KernelBench tools use a source checkout. [PyTorch installation selector](https://pytorch.org/get-started/locally/)

## Evaluate a Baseline

```sh
autopipeline pipeline init my-pipeline --example residual-mlp --device cpu
autopipeline pipeline check --spec my-pipeline/pipeline.json
autopipeline pipeline evaluate --spec my-pipeline/pipeline.json --run-dir my-pipeline/runs/baseline
autopipeline report my-pipeline/runs/baseline
```

`check` validates files and device availability. `evaluate` runs the code, compares outputs,
measures latency, collects a profiler trace, and rechecks the result. Both commands run without
contacting an agent service. Open `view/dashboard.html` or `view/summary.md` in the run directory.
You can [export and recheck](#export-and-recheck) this baseline before continuing.

## Connect a Coding Agent

Install Codex CLI on the machine running the workload, sign in, and verify the login.
[Official Codex CLI setup](https://learn.chatgpt.com/docs/codex/cli)

```sh
codex --version
codex login
codex login status
```

For a headless host, `codex login --device-auth` is available when enabled for your account.
AutoPipeline starts the app-server process on demand.
[Official Codex authentication guide](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)

```sh
autopipeline pipeline run --spec my-pipeline/pipeline.json \
  --run-dir my-pipeline/runs/search --executor-backend codex-app-server \
  --max-rounds 2 --beam-width 2 --children-per-expansion 2 \
  --max-agent-calls 4 --max-probe-calls 4
autopipeline report my-pipeline/runs/search
```

The first round can try two directions. When there is a call cap, the scheduler queues a first child
for each surviving parent before it queues more siblings. Remaining budget and verification determine
how many candidates actually run. This example uses the model for code edits and local strategies for
planning and reflection. A failed baseline stops search before the first agent call.

| Flag | Default | Controls |
| --- | ---: | --- |
| `--beam-width` | 2 | Maximum branches retained after a round |
| `--children-per-expansion` | 2 | Maximum candidates tried per parent per round |
| `--max-rounds` | 3 | Maximum rounds |
| `--max-agent-calls` | 12 | Calls to the configured agent service or command. Billing depends on the service. |
| `--max-probe-calls` | 12 | Active evidence probes |
| `--command-timeout-seconds` | 600 | Timeout per agent command |

Set both `--beam-width` and `--children-per-expansion` to 1 for a single-path control. Keep the
workload, agent model, reasoning effort, and call cap fixed across runs. Record actual calls,
tokens, wall time, and repeated latency measurements. The native pipeline evaluates serially.

Use `autopipeline pipeline run --help` for common options and `--help-all` for optional roles and
model overrides. Other coding agents can use `--executor-command`.
[Agent command contract](agent_contract.md)

## Read Results

| Status in `result.json` | Meaning |
| --- | --- |
| `verified` | Evaluation and final verification passed. The selected implementation can be the baseline. |
| `completed_improved` | Search selected a changed implementation with a better score, and final verification passed. Repeat the measurement before reporting a speedup. |
| `completed_no_improvement` | Search finished and the selected implementation verified. See the report for tried and rejected candidates. |
| `failed` or `interrupted` | The run directory holds available evidence. Inspect the reason before starting a new run. |

`promoted_pipeline/` contains the selected code when verification succeeds. The report opens with
final correctness, the reference and candidate latency from that same final check, run time, and
agent calls and recorded tokens. Fields missing from older runs appear as **Unavailable**.

Expand **Branch details** for the iteration map, which plots measured attempts and highlights the
best kept measurement. The tables show each branch's parent, round, and outcome. Candidate proposals,
executions, evaluations, and budget skips have separate counts. The `autopipeline report RUN_DIR`
exit code reports whether the report could be generated. [Run artifact reference](dev/run-artifacts.md)

| Symptom | Check |
| --- | --- |
| `autopipeline` or PyTorch is missing | Activate the environment that contains both packages. |
| CUDA is unavailable | Check the driver, PyTorch build, and device visibility. Initialize a new `--device cpu` workload for CPU evaluation. |
| Agent fails to start or authenticate | Check `nodes/*/agent/` and run `codex login status` on the execution machine. |
| Incorrect output or incompatible `state_dict` | Use the diagnostic log to fix output, structure, or weight compatibility in the candidate. |
| A directory already exists | Choose a new name, or omit `--run-dir` to get a timestamped directory. |
| Weight hashes or verified files changed | Restore the expected files or start a new evaluation using the current files. |

## Export and Recheck

```sh
autopipeline pipeline export my-pipeline/runs/search --output-dir my-candidate
autopipeline pipeline evaluate --spec my-candidate/pipeline.json \
  --candidate my-candidate/candidate
```

Use `my-pipeline/runs/baseline` if you started with evaluation only. The export contains the selected
code, fixed reference, and verification records. Provide the same external weights and dependencies
when rechecking. After an interrupted search you can inspect its records and start a fresh run with
`--candidate PREVIOUS/promoted_pipeline` if that candidate exists.

## Use Your Own PyTorch Model

```sh
autopipeline pipeline examples
autopipeline pipeline init my-model --source /path/to/model-workspace --device cpu
```

| File or interface | Requirements |
| --- | --- |
| `reference.py` | Define `Model(torch.nn.Module)` and `get_inputs()`. Optionally define `get_init_inputs()`. |
| `candidate.py` | Define `ModelNew` with a compatible constructor and `state_dict`. Initialization supplies a default if absent. |
| Inputs and outputs | Return a list or tuple of positional inputs, or a dict of keyword inputs. Outputs may be nested and must contain a nonempty tensor. |
| `workload/workload.json` | Set device, dtype, warmup count, timing repeats, and numerical tolerances. |
| `pipeline.json` | Declare editable files before running. The default is `candidate.py`. |

Always choose a device explicitly in shared examples. The CLI defaults to CUDA when `--device`
is omitted. Fix the weights, input shapes, dtype, and tolerances before search. The native evaluator
measures synchronized `forward()` calls, including host dispatch. Model loading, input preparation,
and serving latency are outside the timing window. Use trusted code and weights.
[Complete model examples](../examples/README.md)

## NVIDIA Preflight

Select CUDA PyTorch for the host driver, then install AutoPipeline in the same environment.
The preflight uses the built-in Attention example and needs no weights or agent credentials.
[PyTorch installation selector](https://pytorch.org/get-started/locally/)

```sh
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
python -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())'
autopipeline pipeline init gpu-pipeline --example attention --device cuda
autopipeline pipeline check --spec gpu-pipeline/pipeline.json
autopipeline pipeline evaluate --spec gpu-pipeline/pipeline.json --run-dir gpu-pipeline/runs/baseline
autopipeline report gpu-pipeline/runs/baseline
```

Check the numerical verdict and CUDA profile before connecting an agent. For a measured comparison
record the driver, PyTorch version, weights revision, input shape, dtype, and peak memory.
[RTX 4060 handoff](dev/handoff.md#rtx-4060-preflight)
