# AutoKernel

[![Discord](https://img.shields.io/badge/Discord-Join%20us-5865F2?logo=discord&logoColor=white)](https://discord.gg/UfEyc72t)

**Autoresearch for GPU kernels.** Give it any PyTorch model, go to sleep, wake up to optimized Triton or CUDA C++ kernels.

![AutoKernel Progress](progress.png)

Inspired by [@karpathy/autoresearch](https://github.com/karpathy/autoresearch) -- which demonstrated autonomous AI agents for LLM training research. AutoKernel applies the same philosophy to GPU kernel optimization: agent modifies one file, runs a fixed evaluation, keeps or reverts, repeats forever.

## How It Works

Give AutoKernel any PyTorch model. It will:

1. **Profile** the model to find which GPU kernels are bottlenecks
2. **Extract** each bottleneck as a standalone Triton or CUDA C++ kernel
3. **Optimize** each kernel autonomously (edit, benchmark, keep/revert -- forever)
4. **Verify** end-to-end correctness and report the total speedup

The agent reads `program.md` -- the "research org code" -- which contains comprehensive instructions for autonomous operation. It edits `kernel.py` one kernel at a time, runs `bench.py` (fixed benchmark with 5-stage correctness checks + roofline analysis), and either keeps or reverts the change. The orchestrator decides when to move to the next kernel using Amdahl's law.

Each experiment takes ~90 seconds. That's ~40 experiments/hour, ~320 overnight, across all kernels.

## Quick Start

**Requirements:** NVIDIA GPU (tested on H100/A100/RTX 4090), Python 3.10+, [uv](https://docs.astral.sh/uv/).
This repo already ships a `.python-version` file pinned to `3.10`; use that as the default interpreter target for both the main pipeline and the tree-search adapter.

```bash
# Install uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone https://github.com/RightNow-AI/autokernel.git
cd autokernel
uv sync

# One-time setup: test data + baselines
uv run prepare.py

# Profile a model (ships with GPT-2, LLaMA, BERT -- no transformers needed)
uv run profile.py --model models/llama_7b.py --class-name LlamaModel \
 --input-shape 1,512 --dtype float16

# Extract top bottleneck kernels
uv run extract.py --top 5

# Verify benchmark works
uv run bench.py
```

`profile.py` now writes `pipeline_profile.json` next to the selected profile report. By default that means both files land in `workspace/`.  
`extract.py` reads that sibling pipeline profile and copies those hints into each extracted kernel header so the tree adapter can use them later.

## Running the Agent

Spin up Claude, Codex, or any coding agent in this directory:

```
Read program.md and let's kick off a new experiment. Start with setup.
```

The agent will:
1. Profile your model and present the optimization plan
2. Create a branch (e.g., `autokernel/mar10-llama7b`)
3. Optimize each bottleneck kernel in priority order
4. Verify end-to-end correctness and report total speedup

`program.md` is intentionally comprehensive so the agent can run 10+ hours without getting stuck. It includes a 6-tier optimization playbook, decision framework, crash handling, and Amdahl's law reasoning.

## Tree Search Adapter (Experimental)

The default path above is the long-running single-agent loop driven by `program.md`.
The tree adapter is a separate experimental path for single-kernel search with short, isolated branches.

Current scope:

- single-kernel only
- copy-based sandbox only
- no resume yet
- `watchman` can read lightweight pipeline-style profile hints, but this adapter still edits only sandbox-local `kernel.py` and therefore only emits kernel-local response families
- good for controlled search experiments, not a replacement for `orchestrate.py`

### Setup

Use the same environment as the main pipeline:

```bash
uv python install $(cat .python-version)
uv venv --python $(cat .python-version)
uv sync
```

Optional extras are unchanged:

- `uv sync --extra models`
- `uv sync --extra cuda`
- `uv sync --extra kernelbench`

### Smoke Tests

Structural smoke test:

```bash
python -m unittest discover -s tests -v
```

GPU smoke test:

```bash
uv run prepare.py
uv run profile.py --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run extract.py --top 1

uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name smoke-tree \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick
```

Expected outputs:

- `workspace/tree_runs/<timestamp>-<kernel>/search_state.json`
- `workspace/tree_runs/<timestamp>-<kernel>/tree_events.jsonl`
- per-node benchmark logs under `nodes/`
- a promoted `*_optimized.py` next to the source kernel

### Run Tree Search

Basic run:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name matmul-tree \
  --beam-width 2 \
  --max-rounds 3 \
  --children-per-expansion 2 \
  --bench-arg=--quick
```

Useful flags:

- `--bench-arg` can be repeated and is passed directly to `bench.py`
- use `--bench-arg=...` when the argument starts with `-`
- add `--verify-command` to run end-to-end verification after promotion

### External Agent Commands

The adapter can call three optional commands:

- `--planner-command`
- `--executor-command`
- `--reflector-command`

Each command receives:

- `TREE_PROMPT_PATH`
- `TREE_PAYLOAD_PATH`
- `TREE_OUTPUT_PATH`

Expected JSON outputs:

- planner: a list of hypotheses with `id`, `title`, `core_hypothesis`; it may also return `family`, `rationale`, `target_signals`, `supporting_evidence`, and `disallowed_directions`
- executor: `status`, `summary`, `changed`
- reflector: `polarity`, `summary`, `evidence`

If the planner returns only the older minimal hypothesis fields, the adapter backfills the missing watchman branch identity fields automatically.

Example:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name matmul-tree-agent \
  --beam-width 2 \
  --max-rounds 3 \
  --children-per-expansion 2 \
  --planner-command 'python scripts/tree_agent.py propose' \
  --executor-command 'python scripts/tree_agent.py execute' \
  --reflector-command 'python scripts/tree_agent.py reflect'
```

## The Pipeline

```
                 profile.py              extract.py           bench.py (loop)         verify.py
Any PyTorch  ──>  Rank kernels  ──>  Generate baseline  ──>  Optimize each  ──>  End-to-end
   model          by GPU time       Triton/CUDA kernels     kernel (agent)       verification
```

| Tool | What it does |
|------|-------------|
| `profile.py` | Profiles any PyTorch model with `torch.profiler`, ranks kernels by GPU time, classifies as compute/memory-bound |
| `extract.py` | Extracts top-N bottleneck kernels into standalone Triton or CUDA C++ kernel files (`--backend triton\|cuda`) |
| `orchestrate.py` | Multi-kernel scheduler: decides which kernel to optimize next using Amdahl's law, tracks aggregate progress |
| `bench.py` | Fixed benchmark: 5-stage correctness (smoke, shape sweep, numerical stability, determinism, edge cases) + performance + roofline |
| `verify.py` | Plugs optimized kernels back into the model, checks end-to-end correctness, reports total speedup |

## Supported Kernels

9 kernel types covering the core operations of modern deep learning:

| Kernel | Description | Key Metric |
|--------|-------------|------------|
| **matmul** | Dense matrix multiplication (M x K) @ (K x N) | TFLOPS |
| **softmax** | Row-parallel numerically stable softmax | GB/s |
| **layernorm** | Layer normalization with affine transform | GB/s |
| **rmsnorm** | RMS normalization (LLaMA-style) | GB/s |
| **flash_attention** | Scaled dot-product attention with causal masking | TFLOPS |
| **fused_mlp** | SwiGLU-style fused MLP (gate + up + down) | TFLOPS |
| **cross_entropy** | Fused cross entropy loss | GB/s |
| **rotary_embedding** | Rotary position embeddings (RoPE) | GB/s |
| **reduce** | Parallel reduction (sum) | GB/s |

Each has a PyTorch reference in `reference.py`, a starter Triton kernel in `kernels/`, and a starter CUDA C++ kernel in `kernels/cuda/`.

## Example Models

Self-contained model definitions ship with AutoKernel (no `transformers` library needed):

| Model | File | Params | Usage |
|-------|------|--------|-------|
| GPT-2 Small | `models/gpt2.py` | 124M | `--class-name GPT2 --input-shape 1,1024` |
| LLaMA (compact) | `models/llama_7b.py` | 160M | `--class-name LlamaModel --input-shape 1,512` |
| LLaMA 7B | `models/llama_7b.py` | 7B | `--class-name LlamaModel7B --input-shape 1,2048` |
| BERT-base | `models/bert_base.py` | 110M | `--class-name BertModel --input-shape 8,512` |
| Custom | `models/custom.py` | -- | Template for your own model |

For HuggingFace models (`uv sync --extra models`):

```bash
uv run profile.py --module transformers --class-name AutoModelForCausalLM \
 --pretrained meta-llama/Llama-2-7b-hf --input-shape 1,2048 --dtype float16
```

## KernelBench Integration

AutoKernel integrates with [KernelBench](https://github.com/ScalingIntelligence/KernelBench),
the standard benchmark for evaluating AI-generated GPU kernels (250+ problems across 4 difficulty
levels). While most KernelBench evaluations use one-shot LLM generation, AutoKernel runs
**50-300+ iterative refinement experiments per problem** -- systematically exploring the
optimization space instead of guessing.

```bash
# Install KernelBench dependencies
uv sync --extra kernelbench

# Fetch Level 1 problems from HuggingFace
uv run kernelbench/bridge.py fetch --source hf --level 1

# Set up a specific problem for optimization
uv run kernelbench/bridge.py setup --level 1 --problem 1 --source hf

# Evaluate (correctness + speedup vs PyTorch reference)
uv run kernelbench/bench_kb.py

# Batch score an entire level (computes fast_p metric)
uv run kernelbench/scorer.py --level 1
```

The agent reads `kernelbench/program_kb.md` for KernelBench-specific optimization instructions:
how to write `ModelNew` classes, when to use CUDA C++ vs Triton, fusion strategies per problem
level, and the edit-bench-keep/revert loop adapted for the KernelBench `fast_p` metric.

| Tool | What it does |
|------|-------------|
| `kernelbench/bridge.py` | Loads problems from HuggingFace or local repo, caches them, generates starter `kernel.py` |
| `kernelbench/bench_kb.py` | Evaluates `ModelNew` vs `Model`: 5-trial correctness + CUDA event timing + stability + determinism |
| `kernelbench/scorer.py` | Batch evaluation across a level, computes `fast_p` at thresholds (1.0x, 1.5x, 2.0x, 3.0x, 5.0x) |
| `kernelbench/program_kb.md` | Agent instructions for KernelBench mode |

## HuggingFace Kernels Export

Export optimized kernels to the [HuggingFace Hub](https://huggingface.co/docs/kernels/en/index)
for easy distribution. Users can then load your kernels with a single line:

```python
from kernels import get_kernel
module = get_kernel("your-username/kernel-name")
```

```bash
# Export an optimized CUDA kernel
uv run export_hf.py --name my_matmul

# Upload to Hub (requires `pip install kernels` and `huggingface-cli login`)
cd workspace/hf_export/my_matmul
kernels upload . --repo_id your-username/my_matmul
```

## Project Structure

The original project structure is still centered on the fixed evaluation loop: profile, extract, benchmark, verify.
What the tree-search work adds is a thin orchestration layer around that loop, not a replacement for it.
In plain terms, the new pieces do four jobs:

- they keep search control separate from task logic, so beam expansion, frontier updates, pruning, state dumps, and promotion all live in one place
- they adapt the existing AutoKernel benchmark path to node-local sandboxes, so each branch edits its own `kernel.py` without touching `bench.py` or `verify.py`
- they package short per-node context and expose a simple command bridge, so Claude, Codex, or another external agent can plug in without changing this repo's runtime core
- they add focused tests around the tree engine, command adapter, and AutoKernel task adapter, so the new path can be smoke-tested independently from a full overnight optimization run

```
autokernel/
  .python-version       pinned default Python version for local env setup
  kernel.py             the file the agent modifies (one kernel at a time)
  program.md            agent instructions -- the "research org code"

  bench.py              fixed benchmark + 5-stage correctness harness
  reference.py          PyTorch reference implementations (ground truth)
  prepare.py            one-time setup: test data, baselines

  profile.py            profile any PyTorch model, rank kernels by GPU time
  extract.py            extract bottleneck kernels into workspace/
  orchestrate.py        multi-kernel scheduler (Amdahl's law)
  verify.py             end-to-end model verification + speedup report
  tree_optimize.py      single-kernel tree-search entrypoint
  export_hf.py          export optimized kernels to HuggingFace Kernels format
  analysis.py           experiment visualization (generates progress.png)

  tree/                 tree-search adapter (engine, task adapter, prompts, command bridge)
  tests/                tree adapter and task smoke tests
  kernels/              starter Triton kernels (9 types)
  kernels/cuda/         starter CUDA C++ kernels (9 types, tensor core accelerated)
  kernelbench/          KernelBench integration (bridge, eval harness, scorer)
  models/               self-contained model definitions (GPT-2, LLaMA, BERT)
  workspace/            runtime artifacts (gitignored)
```

## Design Choices

**Dual backend: Triton + CUDA C++.** Triton for fast iteration (Python-like syntax, compiles in seconds). CUDA C++ for maximum performance (direct access to tensor cores via `wmma`, PTX intrinsics, shared memory bank-conflict-free layouts). Triton regularly reaches 80-95% of cuBLAS; CUDA C++ can match or exceed it. Both backends share the same `kernel_fn()` interface -- `bench.py` runs identically on either.

**Correctness first.** The benchmark checks kernel output against PyTorch before measuring performance. A fast but wrong kernel is immediately reverted. This prevents the agent from "optimizing" by producing garbage.

**Amdahl's law orchestration.** The orchestrator prioritizes by impact. A 1.5x speedup on a 60% kernel (1.25x end-to-end) beats a 3x speedup on a 5% kernel (1.03x end-to-end). It moves on when diminishing returns set in.

**Single file to modify.** The agent only touches `kernel.py`. Scope stays manageable, diffs reviewable, reverts clean.

**TSV logging.** Results go to a plain `results.tsv` file. Human-readable, git-friendly, trivially parseable, no infrastructure.

## Results Format

Every experiment is logged to `results.tsv` (tab-separated):

| Column | Description |
|--------|-------------|
| `experiment` | Sequential experiment number (0 = baseline) |
| `tag` | Short identifier |
| `kernel_type` | Which kernel (e.g., `matmul`) |
| `throughput_tflops` | Measured throughput (higher is better) |
| `latency_us` | Execution time in microseconds |
| `pct_peak` | Percentage of GPU theoretical peak |
| `speedup_vs_pytorch` | Speedup vs PyTorch/cuBLAS |
| `correctness` | PASS, FAIL, TIMEOUT, or CRASH |
| `peak_vram_mb` | Peak GPU memory usage |
| `description` | What was tried |

## Credits

This project is **autoresearch for GPU kernels** -- directly inspired by Andrej Karpathy's [autoresearch](https://github.com/karpathy/autoresearch), the original experiment in autonomous AI research agents for LLM training. Karpathy showed that an AI agent can run hundreds of experiments overnight, methodically exploring a search space and logging every result. AutoKernel applies that same loop -- agent edits one file, runs a fixed evaluation, keeps or reverts -- to the domain of GPU kernel optimization with Triton and native CUDA C++.

**KernelBench** integration is based on the work of Simon Guo, Sean Resta, et al. at Stanford's Scaling Intelligence Lab. Their paper ["KernelBench: Can LLMs Write GPU Kernels?"](https://arxiv.org/abs/2502.10517) (2025) established the standard benchmark for evaluating AI-generated GPU kernels. AutoKernel extends this by applying iterative optimization (300+ experiments per problem) instead of one-shot generation. KernelBench dataset and evaluation protocol: [ScalingIntelligence/KernelBench](https://github.com/ScalingIntelligence/KernelBench).

Built by [RightNow AI](https://www.rightnowai.co). For enterprise GPU optimization, check out [RightNow Enterprise](https://www.rightnowai.co/forge).

## Changelog

### v1.3.0
- AMD ROCm GPU support: MI300X, MI325X, MI350X, MI355X detection and specs (thanks [@andyluo7](https://github.com/andyluo7))
- Fixed `verify.py` SyntaxError on Python 3.13+
- Fixed CUDA flash_attention ignoring `sm_scale` parameter
- Fixed CUDA cross_entropy returning wrong dtype
- Fixed Triton rotary_embedding broadcasting truncation
- Fixed Triton reduce output shape for non-last-dim reductions

### v1.2.0
- Enhanced profiler: `--export-trace`, `--memory-snapshot`, `--torch-compile-log` flags
- HuggingFace Kernels export via `export_hf.py`

### v1.1.0
- Native CUDA C++ backend with 9 starter kernels (tensor cores, warp intrinsics, shared memory tiling)
- KernelBench integration (250+ standardized GPU kernel problems)
- `--backend triton|cuda` flag for `extract.py`

### v1.0.0
- Initial release: Triton kernel optimization pipeline with 5-stage correctness harness

See [CHANGELOG.md](CHANGELOG.md) for full details.

## License

MIT
