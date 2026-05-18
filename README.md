# AutoPipeline

AutoPipeline is an agent-first pipeline and GPU kernel optimization workspace
built from the AutoKernel codebase.

It currently has several usable paths:

- the original long-running single-agent loop for extracted kernels
- an experimental tree framework for controlled branch-based search
- a KernelBench tree adapter
- a generic pipeline workspace bridge

The repository started from AutoKernel's kernel optimizer. It is now an
independent project focused on a general framework for pipeline
co-optimization. The framework core is already task-agnostic; AutoKernel-style
single-kernel tasks, KernelBench, and generic pipeline tasks now plug into the
same tree runtime.

The current agent runtime is session-oriented. Codex roles default to the
Codex app-server backend, which gives each role a resumable thread, event logs,
turn status, token usage, and optional dynamic sensing tools. The older JSON
command bridge remains available for local scripts and non-Codex agents.

## Lineage

AutoPipeline is an independent project built from the AutoKernel implementation
base. The AutoKernel-compatible path remains available for extracted
single-kernel work, while the new tree runtime, sensing layer, KernelBench
adapter, and pipeline bridge move the project toward broader pipeline
co-optimization.

## Status

- stable enough to use: the original AutoKernel-compatible loop
- experimental but usable: the tree framework core
- experimental: KernelBench and generic pipeline tree task surfaces
- still in progress: production pipeline profiler / trace schema / cross-kernel editor

## What You Can Use Today

### Main Path

The default path is still the original AutoKernel-compatible loop:

1. profile a PyTorch model
2. extract hot kernels
3. optimize one kernel at a time with a fixed benchmark loop
4. verify end-to-end correctness

This path is driven by [program.md](program.md).

### Tree path

The tree framework is a separate path for short, isolated search:

- `planner` freezes high-level branch identity
- `TreeSearchEngine` manages expansion, pruning, and state
- `BranchEngine` runs a short branch-local keep/revert loop
- `SensingRuntime` runs diagnostic probes and records results
- the thin verifier compresses current evidence into `keep | reject | need_more_evidence`

Current tree scope:

- the framework core is task-agnostic
- AutoKernel-compatible tree mode still edits one extracted kernel at a time
- KernelBench and generic pipeline task surfaces are available
- external planner / executor / reflector commands are optional
- active sensing, generic command/python probes, and `theoretical_limit_inputs` are available
- Codex planner / executor / reflector roles can run through app-server sessions
- command timeout, cancellation, activity logs, diagnostics, budgets, and archive summaries are available
- good for search experiments and framework development
- not yet a production pipeline optimizer

## Human Quick Start

Requirements:

- NVIDIA GPU for the main optimization path
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)

Setup:

```bash
uv python install $(cat .python-version)
uv venv --python $(cat .python-version)
uv sync
uv run prepare.py
```

Profile and extract one kernel:

```bash
uv run profile.py --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run extract.py --top 1
```

Run one tree smoke:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name smoke-tree \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick
```

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

## Monitoring And Feedback

When you run the tree framework, the useful files are:

- `search_state.json`: current tree state and final result summary
- `tree_events.jsonl`: round-by-round search events
- per-node `agent/` directories: prompt, payload, telemetry, stdout, stderr, structured output
- per-node `sensing/` files: probe history, recent diagnostics, verifier summary

If you want to understand how probe and verifier data are recorded, start with:

- [docs/sensing.md](docs/sensing.md)
- [docs/verifier.md](docs/verifier.md)

## Agent Entry

This repository is agent-first, but the top-level page should still stay readable for humans.

If you are wiring an external agent into the framework, start with:

- [docs/agent_entry.md](docs/agent_entry.md): agent runtime entry and navigation page
- [docs/agent_contract.md](docs/agent_contract.md): dense runtime contract for planner / executor / reflector
- [program.md](program.md): long-running AutoKernel-compatible agent instructions for the original path

For Codex, prefer role config over shell wrappers:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name codex-session-smoke \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick \
  --executor-provider codex
```

Use `--executor-backend command` or an explicit `--executor-command` when you
need the legacy file-based command bridge.

## Documentation Map

- [docs/index.md](docs/index.md): documentation entrypoint
- [docs/quickstart.md](docs/quickstart.md): practical setup and smoke tests
- [docs/architecture.md](docs/architecture.md): current layer model and module map
- [docs/tree_adapter.md](docs/tree_adapter.md): tree framework usage and command bridge
- [docs/sensing.md](docs/sensing.md): how active sensing and probes work
- [docs/verifier.md](docs/verifier.md): what the thin verifier does today
- [CONTRIBUTING.md](CONTRIBUTING.md): local checks and contribution boundaries
- [SECURITY.md](SECURITY.md): vulnerability reporting and local safety notes
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): project conduct expectations

## Current Direction

The near-term direction is:

- keep the framework thin
- make docs and module boundaries clearer
- grow sensing toward hardware-aware and pipeline-aware probes
- keep theoretical-limit formulas in agent-authored scripts while exposing stable inputs through sensing
- keep verifier thin while expanding evidence sources
- move from kernel optimization toward pipeline co-optimization

That means the framework should increasingly help the agent act like a practical ML systems engineer:

- inspect the machine
- inspect the software stack
- inspect runtime breakdown
- run targeted experiments
- choose changes that fit the current hardware and workload
