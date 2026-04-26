# Agent Entry

This is the first page for framework-driven agents.

Do not use it as a project overview. Human-facing entry stays in [README.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/README.md).

Use this page to answer one question fast:

- where am I in the runtime
- which file should I read next
- which document owns the contract I need

## Runtime Graph

- human overview: [README.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/README.md)
- layer map: [docs/architecture.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/architecture.md)
- framework runtime contract: [docs/agent_contract.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/agent_contract.md)
- sensing details: [docs/sensing.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/sensing.md)
- verifier details: [docs/verifier.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/verifier.md)
- AutoKernel long-loop task doc: [program.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/program.md)

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

Framework-driven agent commands receive these paths through environment variables:

- `TREE_PROMPT_PATH`
- `TREE_PAYLOAD_PATH`
- `TREE_TELEMETRY_PATH`
- `TREE_OUTPUT_PATH`
- `TREE_SENSING_PATH`
- `TREE_PROBE_REQUESTS_PATH`
- `TREE_PROBE_RESULTS_PATH`

If you need exact semantics, go to [docs/agent_contract.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/agent_contract.md).

## Read Order

Use this order unless the current prompt says otherwise:

1. `TREE_PROMPT_PATH`
2. `TREE_PAYLOAD_PATH`
3. `TREE_SENSING_PATH`
4. `TREE_TELEMETRY_PATH` only if you need richer context

## Write Rule

Write one JSON object to `TREE_OUTPUT_PATH`.

Do not write prose logs there.

## Probe Rule

If current evidence is insufficient:

1. inspect `TREE_SENSING_PATH`
2. write one or more diagnostic requests to `TREE_PROBE_REQUESTS_PATH`
3. read results from `TREE_PROBE_RESULTS_PATH`
4. continue the same step

Two probe flows matter:

- simple inspection: `read_text`, `read_json`, `list_dir`, `grep`
- derived calculations: request `theoretical_limit_inputs`, then use the built-in `python` probe to compute roofline/headroom values from those stable inputs

If you need exact probe semantics, go to [docs/sensing.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/sensing.md).

## Stable Invariants

Treat these as stable enough to rely on:

- framework owns search control
- planner owns high-level route freezing
- executor owns one local step
- reflector owns one compressed insight
- sensing owns probe execution and recording
- verifier owns thin verdict compression

## Current Boundary

The framework core is moving toward task-agnostic pipeline co-optimization.

The bundled task still reflects AutoKernel. New tasks should plug into the same runtime shape instead of changing the framework contract.
