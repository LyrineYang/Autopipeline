# Agent Entry

This is the first page for framework-driven agents.

Do not use it as a project overview. Human-facing entry stays in
[README.md](../README.md).

Use this page to answer one question fast:

- where am I in the runtime
- which file should I read next
- which document owns the contract I need

## Runtime Graph

- human overview: [README.md](../README.md)
- layer map: [docs/architecture.md](architecture.md)
- framework runtime contract: [docs/agent_contract.md](agent_contract.md)
- sensing details: [docs/sensing.md](sensing.md)
- verifier details: [docs/verifier.md](verifier.md)
- AutoKernel-compatible long-loop task doc: [program.md](../program.md)

## Role Map

- `planner`
  Reads task facts, history, sensing, and verifier.
  Freezes route: scope, target, branch identity, budget hints, stop conditions.

- `executor`
  Makes one local change inside the current branch-local loop.
  Must stay inside the frozen route.

- `reflector`
  Compresses one node outcome into one reusable insight.

## File Map

Codex app-server roles receive the same prompt, payload, telemetry, and sensing
content in a session turn. The runtime records events, requests, token usage,
turn status, and per-role session files under the node's `agent/` directory.

Legacy command-bridge agents receive these paths through environment variables:

- `TREE_PROMPT_PATH`
- `TREE_PAYLOAD_PATH`
- `TREE_TELEMETRY_PATH`
- `TREE_OUTPUT_PATH`
- `TREE_SENSING_PATH`
- `TREE_PROBE_REQUESTS_PATH`
- `TREE_PROBE_RESULTS_PATH`
- `TREE_CANCEL_PATH`
- `TREE_ACTIVITY_PATH`
- `TREE_AGENT_ROLE`

If you need exact semantics, go to
[docs/agent_contract.md](agent_contract.md).

Role-specific config may also be present through `TREE_AGENT_PROVIDER`,
`TREE_AGENT_MODEL`, `TREE_AGENT_BASE_URL`, `TREE_AGENT_REASONING_EFFORT`,
`TREE_AGENT_MAX_OUTPUT_TOKENS`, `TREE_AGENT_TEMPERATURE`,
`TREE_AGENT_SUBAGENT`, and `TREE_AGENT_TIMEOUT_SECONDS`.

The command bridge also writes `command_status.json`, `activity.jsonl`,
`timeout_diagnostic.json`, and `env_redacted.json` in the current step's
agent log directory.

## Read Order

Use this order unless the current prompt says otherwise:

1. `TREE_PROMPT_PATH`
2. `TREE_PAYLOAD_PATH`
3. `TREE_SENSING_PATH`
4. `TREE_TELEMETRY_PATH` only if you need richer context

## Write Rule

For the app-server planner and reflector roles, return one JSON object in the
final message. For the legacy command bridge, write one JSON object to
`TREE_OUTPUT_PATH`.

Do not write prose logs there.

## Probe Rule

If current evidence is insufficient:

1. inspect `TREE_SENSING_PATH`
2. call `run_sensing_probe` in app-server mode, or write diagnostic requests to `TREE_PROBE_REQUESTS_PATH` in legacy command mode
3. read the returned dynamic tool result or `TREE_PROBE_RESULTS_PATH`
4. continue the same step

Two probe flows matter:

- simple inspection: `read_text`, `read_json`, `list_dir`, `grep`
- derived calculations: request `theoretical_limit_inputs`, then use the built-in `python` probe to compute roofline/headroom values from those stable inputs

If you need exact probe semantics, go to
[docs/sensing.md](sensing.md).

## Stable Invariants

Treat these as stable enough to rely on:

- framework owns search control
- planner owns high-level route freezing
- executor owns one local step
- reflector owns one compressed insight
- sensing owns probe execution and recording
- verifier owns thin verdict compression
- agent backend owns app-server sessions or legacy subprocess execution
- command bridge owns subprocess timeout, cancellation, command status, activity, and diagnostics
- budget ledger owns command/probe/family usage accounting

## Current Boundary

The framework core is task-agnostic enough to run AutoKernel-compatible, KernelBench, and
generic pipeline workspace tasks.

The AutoKernel-compatible path still edits a branch-local `kernel.py` surface. The generic pipeline
task is spec-driven; it is not yet a production trace profiler or cross-kernel
editor. New tasks should plug into the same runtime shape instead of changing
the framework contract.
