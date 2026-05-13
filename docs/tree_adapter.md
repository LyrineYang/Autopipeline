# Tree Adapter

This page explains how to use the experimental tree-search path exposed by the bundled AutoKernel CLI.

The framework core behind this path is now task-agnostic. This page is narrower: it documents the current shipped CLI path, which still runs the AutoKernel single-kernel task.

## What It Does

The tree adapter runs short, isolated search branches for one extracted kernel.

Its current split is:

- `planner` decides high-level route
- the planner proposes branches under that route
- the executor performs one local edit step at a time
- `BranchEngine` keeps or reverts inside a short local loop
- the tree controller keeps the best branches and prunes the rest

## CLI

Basic example:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name matmul-tree \
  --beam-width 2 \
  --max-rounds 3 \
  --children-per-expansion 2 \
  --bench-arg=--quick
```

Important flags:

- `--beam-width`
- `--max-rounds`
- `--children-per-expansion`
- `--bench-arg`
- `--planner-command`
- `--executor-command`
- `--reflector-command`
- `--verify-command`

The beam, round, and child settings are hard upper bounds.  
`planner` can tighten the local budget inside those limits.

## External Commands

The adapter can call up to three external commands:

- planner
- executor
- reflector

Each command receives:

- `TREE_PROMPT_PATH`
- `TREE_PAYLOAD_PATH`
- `TREE_TELEMETRY_PATH`
- `TREE_SENSING_PATH`
- `TREE_PROBE_REQUESTS_PATH`
- `TREE_PROBE_RESULTS_PATH`
- `TREE_OUTPUT_PATH`

The current contract is intentionally file-based and simple.
It is also a same-step active-sensing loop, not just a one-shot request:

- read prompt / payload / sensing
- if evidence is missing, write probe requests
- read probe results during the same step
- then write the final action output

## Outputs

The tree run writes:

- `search_state.json`
- `tree_events.jsonl`
- per-node sandboxes and agent logs
- probe history under `nodes/.../sensing/`
- promoted output next to the source kernel when a node wins

You can build the first human-facing run view after a run finishes:

```bash
python3 scripts/tree_report.py <run-dir>
```

This writes:

- `view/run_summary.json`
- `view/branches.jsonl`
- `view/attempts.jsonl`
- `view/metrics_catalog.json`
- `view/human_timeline.jsonl`
- `view/iteration_effect.svg`
- `view/dashboard.html`
- `view/summary.md`

## Current Scope

What this adapter is good at:

- branch-based search experiments
- planner / executor / reflector integration
- active sensing and probe-guided decisions
- agent-authored theoretical-limit calculations built on top of `theoretical_limit_inputs`
- controlled single-kernel work

What it is not yet:

- a full pipeline optimizer
- a resume-capable large orchestration system
- a shipped multi-surface pipeline task
