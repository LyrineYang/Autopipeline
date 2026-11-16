# Architecture

This page explains the current layer model in plain language.

## Current Shape

The repository has one preferred user entrypoint:

- `autopipeline`, which wraps setup, profiling, extraction, tree search, run
  reports, benchmarking, and verification

Under that CLI, the tree framework is the shared runtime for planner, sensing,
verifier, branch-local execution, budget tracking, archive reuse, and run
viewing. The default product flow is a pipeline workspace with a fixed reference
and an editable candidate. AutoKernel-compatible extracted kernels and
KernelBench problems remain task adapters using the same runtime.

`autopipeline/pipeline_cli.py` owns initialization, preflight checks, frozen run
inputs, status, and export. `autopipeline/evaluate.py` is the installed PyTorch
harness: it loads user models, checks every tensor output across seeded inputs,
measures synchronized forward latency, and writes torch profiler artifacts.
`pipeline/verification_backend.py` judges those measurements. `pipeline/task.py`
adapts that contract to the existing tree engine and enforces protected files.
The evaluator executes outside the editable workload's source files.

The `research/level5` branch holds the Level 5 task definitions, reference
baselines and scoring policy, along with apimetric benchmark instrumentation.
The product budget ledger records its own execution. The benchmark can
independently measure the same run.

The current research stage is pipeline co-optimization in a fixed hardware and
workload setting. That means the framework should help an agent optimize the
end-to-end pipeline for the current task, not claim general infrastructure
automation across arbitrary hardware yet.

## Runtime Ownership Model

The current runtime follows a small ownership split inspired by modern
long-running agent systems:

- Brain: planner, executor, and reflector roles. These roles reason and act,
  but they do not own evaluation truth.
- Hands: task sandboxes, editable workspaces, shell commands, profilers, and
  probes. These perform actions against a concrete workspace.
- Session: app-server thread / turn state, event logs, request logs, token
  usage, and resumable role state. Session is an agent management interface,
  not the optimization algorithm.
- Evidence: sensing records, evaluator outputs, verifier summaries, archive
  motifs, and run-view artifacts. Evidence is independent of any one agent
  session and is the durable substrate for planning and review.

This split is important for future-proofing. As coding agents improve, the
framework should be able to swap harnesses or agent backends without changing
what counts as evidence or how candidates are judged.

Transport is separate from ownership:

- app-server dynamic tools expose a transport for Codex sessions to request
  sensing probes
- the legacy file bridge exposes a transport for command-based agents to
  request the same probes
- sensing still owns probe execution, recording, and verifier refresh
- evaluators and verifiers still own candidate judgment, not agent self-report

## Tree Framework Layers

### Task

The task defines:

- what is being optimized
- which files are editable
- how to build a sandbox
- how to evaluate a candidate
- how to promote a winning candidate

Current task implementations:

- [tree/autokernel.py](../tree/autokernel.py): AutoKernel-compatible extracted-kernel task
- [kernelbench/task.py](../kernelbench/task.py): KernelBench problem task
- [pipeline/task.py](../pipeline/task.py): generic pipeline workspace task

Framework core note:

- the framework no longer assumes a single `kernel.py`
- checkpoint / restore and execution-surface preparation now live at the task boundary
- each task owns sandbox creation, evaluation, promotion, task-specific probes, and checkpoint semantics

Supporting task-specific probes:

- [tree/autokernel_probes.py](../tree/autokernel_probes.py)
- `PipelineTask.handle_probe(...)`
- task-local KernelBench context files and benchmark output

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

- [tree/planner.py](../tree/planner.py)

### Search Controller

The search controller manages:

- baseline
- branch expansion
- pruning
- frontier retention
- per-node state

Main file:

- [tree/search.py](../tree/search.py)

It also owns:

- run-level state persistence
- event logging
- `BudgetLedger` wiring
- `RunArchive` motif recording
- optional parallel branch execution

### Branch Engine

The branch engine runs a short branch-local keep/revert loop under one fixed branch identity.

It does not decide the high-level route. It just executes a small local loop for that branch.

Main file:

- [tree/branch_engine.py](../tree/branch_engine.py)

### Agent Runtime

Agent roles are reached through a small backend boundary.

Current backends:

- Codex app-server session backend for Codex planner / executor / reflector roles
- legacy JSON command bridge for local scripts and non-Codex agents

The app-server backend owns:

- one thread / turn lifecycle per role session
- event and request logs
- turn status, token usage, and session persistence
- dynamic-tool transport for calls such as `run_sensing_probe`
- failure surfacing for failed turns, invalid JSON, timeout, and cancellation

The legacy command bridge owns subprocess lifecycle:

- writes prompt, payload, telemetry, command status, activity, and redacted environment files
- exports role-specific environment variables
- serves active-sensing probe requests while the subprocess is running
- supports file-based cancellation through `TREE_CANCEL_PATH`
- terminates timed-out process groups
- classifies failures such as timeout, missing output, invalid output, probe wait loops, and provider/auth failures
- records command usage into `BudgetLedger`

Main files:

- [tree/agent.py](../tree/agent.py)
- [tree/agent_backend.py](../tree/agent_backend.py)
- [tree/codex_app_server.py](../tree/codex_app_server.py)
- [tree/codex_session.py](../tree/codex_session.py)
- [tree/codex_dynamic_tools.py](../tree/codex_dynamic_tools.py)
- [tree/command.py](../tree/command.py)
- [tree/prompts.py](../tree/prompts.py)
- [tree/roles.py](../tree/roles.py)
- [scripts/codex_tree_agent.py](../scripts/codex_tree_agent.py)

`scripts/codex_tree_agent.py` is a legacy Codex CLI wrapper for the executor role.
It reads the tree command files, invokes `codex exec` inside the sandbox, then
writes the tree output JSON itself.

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

- [tree/sensing.py](../tree/sensing.py)

### Verifier

The verifier is intentionally thin.

It does not collect data. It compresses available evidence into a simple judgment that planner and executor can read.

Main file:

- [tree/verifier.py](../tree/verifier.py)

## Minimal File Map

- [tree/types.py](../tree/types.py): shared dataclasses
- [tree/task.py](../tree/task.py): task protocol and sandbox helper
- [tree/search.py](../tree/search.py): tree controller
- [tree/branch_engine.py](../tree/branch_engine.py): branch-local loop
- [tree/planner.py](../tree/planner.py): planner layer
- [tree/sensing.py](../tree/sensing.py): sensing runtime
- [tree/verifier.py](../tree/verifier.py): thin verifier summary
- [tree/agent.py](../tree/agent.py): role adapter
- [tree/command.py](../tree/command.py): command process bridge
- [tree/budget.py](../tree/budget.py): usage, timeout, family, and probe budget ledger
- [tree/archive.py](../tree/archive.py): motif archive and planner prior
- [tree/viewer.py](../tree/viewer.py): normalized run viewer data
- [tree/autokernel.py](../tree/autokernel.py): AutoKernel-compatible task
- [kernelbench/task.py](../kernelbench/task.py): KernelBench task
- [pipeline/task.py](../pipeline/task.py): generic pipeline task

## Current Boundary

Today the tree framework core is:

- task-agnostic at the framework layer
- copy-sandbox based by default
- evaluator-driven
- budget-aware
- archive-backed
- instrumented with command status, activity logs, timeout diagnostics, and active sensing
- experimental

Today the shipped task surfaces are:

- AutoKernel-compatible extracted-kernel optimization through
  `autopipeline tree run`
- KernelBench problem optimization through [kernelbench/tree_run.py](../kernelbench/tree_run.py)
- generic pipeline workspace optimization through `autopipeline pipeline`;
  [pipeline/bridge.py](../pipeline/bridge.py) is a compatibility entrypoint

The remaining boundary is narrower:

- the AutoKernel-compatible task still edits `kernel.py` inside a branch sandbox
- the pipeline task evaluates a declared workspace and metrics contract; its
  native PyTorch evaluator measures correctness and latency and records torch
  profiler evidence. Automatic trace diagnosis, fusion planning, and
  layout-propagation engines are not implemented
- the framework is not a long-running issue-tracker daemon
- in-flight subprocesses are not resumed after process exit, though each command has timeout, cancellation, activity, and diagnostic handling
