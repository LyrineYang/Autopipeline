# AutoKernel

AutoKernel is an agent-first GPU optimization workspace.

It currently has two usable paths:

- the original long-running single-agent loop for extracted kernels
- an experimental tree framework for controlled branch-based search

The repository started as a kernel optimizer. It is now being refit into a more general framework for pipeline co-optimization. The framework core is already moving in that direction; the bundled task is still AutoKernel-specific.

## Status

- stable enough to use: the original AutoKernel loop
- experimental but usable: the tree framework core
- still in progress: generic pipeline co-optimization tasks and objectives

## What You Can Use Today

### Main path

The default path is still the original AutoKernel loop:

1. profile a PyTorch model
2. extract hot kernels
3. optimize one kernel at a time with a fixed benchmark loop
4. verify end-to-end correctness

This path is driven by [program.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/program.md).

### Tree path

The tree framework is a separate path for short, isolated search:

- `planner` freezes high-level branch identity
- `TreeSearchEngine` manages expansion, pruning, and state
- `BranchEngine` runs a short branch-local keep/revert loop
- `SensingRuntime` runs diagnostic probes and records results
- the thin verifier compresses current evidence into `keep | reject | need_more_evidence`

Current tree scope:

- the framework core is task-agnostic
- the bundled task is still AutoKernel single-kernel
- external planner / executor / reflector commands are optional
- active sensing, generic command/python probes, and `theoretical_limit_inputs` are available
- good for search experiments and framework development
- not yet a complete pipeline optimizer

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

- [docs/sensing.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/sensing.md)
- [docs/verifier.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/verifier.md)

## Agent Entry

This repository is agent-first, but the top-level page should still stay readable for humans.

If you are wiring an external agent into the framework, start with:

- [docs/agent_entry.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/agent_entry.md): agent runtime entry and navigation page
- [docs/agent_contract.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/agent_contract.md): dense runtime contract for planner / executor / reflector
- [program.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/program.md): long-running AutoKernel agent instructions for the original path

## Documentation Map

- [docs/index.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/index.md): documentation entrypoint
- [docs/quickstart.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/quickstart.md): practical setup and smoke tests
- [docs/architecture.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/architecture.md): current layer model and module map
- [docs/tree_adapter.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/tree_adapter.md): tree framework usage and command bridge
- [docs/sensing.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/sensing.md): how active sensing and probes work
- [docs/verifier.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/verifier.md): what the thin verifier does today
- [docs/tree_planner_consensus.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/tree_planner_consensus.md): design consensus log
- [docs/planner_v1.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/planner_v1.md): planner background and goals

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
