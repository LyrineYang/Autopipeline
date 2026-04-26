# Architecture

This page explains the current layer model in plain language.

## Current Shape

The repository has two practical paths:

- the original AutoKernel loop
- the experimental tree framework

The original path is still the production path for extracted kernels.  
The tree framework is the place where planner, sensing, and verifier work is happening.

## Tree Framework Layers

### Task

The task defines:

- what is being optimized
- which files are editable
- how to build a sandbox
- how to evaluate a candidate
- how to promote a winning candidate

Current task implementation:

- [tree/autokernel.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/autokernel.py)

Framework core note:

- the framework no longer assumes a single `kernel.py`
- checkpoint / restore and execution-surface preparation now live at the task boundary
- the bundled task is still AutoKernel-specific

Supporting task-specific probes:

- [tree/autokernel_probes.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/autokernel_probes.py)

### Planner

The planner is represented by `planner`.

It reads:

- task facts
- profile hints
- hardware context
- history
- sensing output

It returns:

- branch identities
- high-level scope
- budget hints
- frozen decisions for the executor

Main file:

- [tree/planner.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/planner.py)

### Search Controller

The search controller manages:

- baseline
- branch expansion
- pruning
- frontier retention
- per-node state

Main file:

- [tree/search.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/search.py)

### Branch Engine

The branch engine runs a short branch-local keep/revert loop under one fixed branch identity.

It does not decide the high-level route. It just executes a small local loop for that branch.

Main file:

- [tree/branch_engine.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/branch_engine.py)

### Agent Bridge

The agent bridge shells out to planner / executor / reflector commands and exchanges data through files and environment variables.

Main files:

- [tree/agent.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/agent.py)
- [tree/command.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/command.py)
- [tree/prompts.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/prompts.py)

### Sensing

Sensing is the diagnostic runtime.

It does three things:

- writes a passive snapshot
- runs probe requests
- records probe history and recent events

It also supports one explicit two-step ceiling-analysis path:

- sensing returns stable inputs through `theoretical_limit_inputs`
- the agent writes the actual roofline / headroom calculation with a `python` probe

Main file:

- [tree/sensing.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/sensing.py)

### Verifier

The verifier is intentionally thin.

It does not collect data. It compresses available evidence into a simple judgment that planner and executor can read.

Main file:

- [tree/verifier.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/verifier.py)

## Minimal File Map

- [tree/types.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/types.py): shared dataclasses
- [tree/task.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/task.py): task protocol and sandbox helper
- [tree/search.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/search.py): tree controller
- [tree/branch_engine.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/branch_engine.py): branch-local loop
- [tree/planner.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/planner.py): planner layer
- [tree/sensing.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/sensing.py): sensing runtime
- [tree/verifier.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/verifier.py): thin verifier summary
- [tree/autokernel.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/autokernel.py): current task
- [tree/autokernel_probes.py](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/tree/autokernel_probes.py): task-specific probes

## Current Boundary

Today the tree framework core is:

- task-agnostic at the framework layer
- copy-sandbox based by default
- evaluator-driven
- experimental

Today the bundled task and CLI path are still:

- AutoKernel-specific
- single-kernel at the task surface

So the framework already supports active sensing and branch-local search beyond a single-file worldview, but it is not yet a full pipeline or infra optimizer.
