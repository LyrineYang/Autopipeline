# Tree Adapter

This page explains the current tree-search runtime. Use `autopipeline tree run`
for the public AutoKernel-compatible task path; direct adapter scripts are
advanced compatibility entrypoints.

The framework core is task-agnostic. The AutoKernel-compatible CLI still optimizes one
extracted kernel at a time, but the same tree engine now also backs KernelBench
and generic pipeline tasks.

## What It Does

The tree adapter runs short, isolated search branches for one task surface.

Its current split is:

- `planner` decides high-level route
- the planner proposes branches under that route
- the executor performs one local edit step at a time
- `BranchEngine` keeps or reverts inside a short local loop
- the tree controller keeps the best branches and prunes the rest
- `BudgetLedger` records agent, probe, timeout, and family-level signals
- `RunArchive` writes motif summaries and a planner prior for later rounds

## CLI Path

AutoKernel-compatible extracted-kernel example:

```bash
uv run autopipeline tree run \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name matmul-tree \
  --beam-width 2 \
  --max-rounds 3 \
  --children-per-expansion 2 \
  --bench-arg=--quick
```

Advanced adapter entrypoints remain available for local experiments:

KernelBench example:

```bash
uv run kernelbench/tree_run.py \
  --source hf \
  --level 1 \
  --problem 1 \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1
```

Generic pipeline smoke:

```bash
uv run pipeline/bridge.py smoke
```

Generic pipeline spec run:

```bash
uv run pipeline/bridge.py run \
  --spec path/to/pipeline_task.json \
  --beam-width 2 \
  --max-rounds 1 \
  --children-per-expansion 2
```

Important flags:

- `--beam-width`
- `--max-rounds`
- `--children-per-expansion`
- `--bench-arg`
- `--planner-handoff-mode`
- `--planner-command`
- `--executor-command`
- `--reflector-command`
- `--verify-command`
- `--command-timeout-seconds`
- `--max-agent-calls`
- `--max-probe-calls`
- `--max-parallel-branches`
- `--branch-resource-policy`
- role-specific flags such as `--executor-model`, `--executor-provider`, and `--executor-timeout-seconds`

The beam, round, and child settings are hard upper bounds.  
`planner` can tighten the local budget inside those limits.

## Agent Runtime

The adapter has two role backends:

- Codex app-server sessions for Codex planner / executor / reflector roles
- the legacy JSON command bridge for local scripts and non-Codex agents

For Codex, prefer provider-based role config:

```bash
uv run autopipeline tree run \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name codex-session-smoke \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick \
  --planner-provider codex \
  --executor-provider codex \
  --reflector-provider codex
```

The app-server path records per-role session files, event logs, request logs,
turn status, token usage, and dynamic sensing tool calls.

The legacy command bridge can call up to three external commands:

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
- `TREE_CANCEL_PATH`
- `TREE_ACTIVITY_PATH`
- `TREE_AGENT_ROLE`

Role config may also be passed through environment variables:

- `TREE_AGENT_PROVIDER`
- `TREE_AGENT_MODEL`
- `TREE_AGENT_BASE_URL`
- `TREE_AGENT_REASONING_EFFORT`
- `TREE_AGENT_MAX_OUTPUT_TOKENS`
- `TREE_AGENT_TEMPERATURE`
- `TREE_AGENT_SUBAGENT`
- `TREE_AGENT_TIMEOUT_SECONDS`

The legacy command contract is file-based, but it is no longer just a one-shot
file drop. For each command, [tree/command.py](../tree/command.py) writes:

- `prompt.txt`
- `payload.json`
- `telemetry.json`
- `output.json`
- `stdout.log`
- `stderr.log`
- `command_status.json`
- `activity.jsonl`
- `timeout_diagnostic.json`
- `env_redacted.json`

It then launches the configured command in the target cwd, polls for activity,
serves probe requests, handles cancellation, enforces timeout, validates
`TREE_OUTPUT_PATH`, and records usage into the budget ledger.

The active-sensing loop works during the same command step:

- read prompt / payload / sensing
- if evidence is missing, write probe requests
- read probe results during the same step
- then write the final action output

If a command times out, returns nonzero, fails to write output, or writes invalid
JSON, the bridge writes command status and a diagnostic file before surfacing the
failure to the planner/search layer.

## Codex CLI Wrapper

[scripts/codex_tree_agent.py](../scripts/codex_tree_agent.py)
is the legacy Codex CLI helper for command-bridge testing.

Current boundary:

- it supports the executor role
- it reads the tree prompt, payload, output, and sensing paths
- it invokes `codex exec -C <sandbox> --sandbox workspace-write`
- it strips `TREE_*` variables before launching Codex
- it fingerprints watched sandbox files before and after the Codex run
- it writes the required tree output JSON after Codex exits

This wrapper is a subprocess wrapper around the Codex CLI. It is not an
app-server session manager and does not keep a multi-turn Codex thread alive.

## Outputs

The tree run writes:

- `search_state.json`
- `tree_events.jsonl`
- per-node sandboxes and agent logs
- probe history under `nodes/.../sensing/`
- archive files under `archive/`
- command status, activity, timeout diagnostics, and redacted env snapshots under per-node `agent/` logs
- promoted output next to the source kernel when a node wins

You can build the first human-facing run view after a run finishes:

```bash
uv run autopipeline report <run-dir>
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
- controlled AutoKernel-compatible extracted-kernel work
- KernelBench problem surfaces
- spec-driven pipeline workspace tasks
- command timeout, cancellation, diagnostics, budget tracking, and archive-backed planner priors

What it is not yet:

- a long-running issue-tracker daemon
- a durable resume system for in-flight subprocesses after process exit
- a production pipeline profiler / cross-kernel editor / fusion or layout-propagation engine
