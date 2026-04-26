# Agent Contract

This page is the dense runtime contract for framework-driven agents.

Read [docs/agent_entry.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/agent_entry.md) first if you need navigation. Read [program.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/program.md) instead if you are operating the original long-running AutoKernel path.

## Action Contract

### Planner

- reads:
  - prompt
  - payload
  - sensing
  - optional telemetry
- writes:
  - one JSON object to `TREE_OUTPUT_PATH`
- owns:
  - branch identities
  - scope / target
  - branch budget hints
  - stop conditions
  - frozen decisions

### Executor

- reads:
  - prompt
  - payload
  - sensing
  - optional telemetry
- writes:
  - one JSON object to `TREE_OUTPUT_PATH`
- owns:
  - one local step
  - local diagnostic probes when needed
- must not:
  - reopen planner route
  - edit control files unless the task explicitly allows it

### Reflector

- reads:
  - prompt
  - payload
  - sensing
  - optional telemetry
- writes:
  - one JSON object to `TREE_OUTPUT_PATH`
- owns:
  - one compressed node insight

## File Contract

### Inputs

- `TREE_PROMPT_PATH`
  Human-readable instruction for the current action.

- `TREE_PAYLOAD_PATH`
  Structured JSON for task facts, node facts, planner context, and current contract details.

- `TREE_TELEMETRY_PATH`
  Richer environment snapshot. Optional. Read only when payload and sensing are insufficient.

- `TREE_SENSING_PATH`
  Current sensing manifest. This is the first place to look for probe history, verifier output, and probe budget/examples.

### Outputs

- `TREE_OUTPUT_PATH`
  Required. Write one JSON object only.

- `TREE_PROBE_REQUESTS_PATH`
  Optional. Write a JSON list of diagnostic probe requests when you need more evidence.

- `TREE_PROBE_RESULTS_PATH`
  Optional. Read structured probe results written by the sensing runtime during the same step.

## Output Shape

The exact JSON varies by role, but these are the stable expectations:

- `status`
  Use a concrete lifecycle word such as `completed` or `failed`.

- `summary`
  One short sentence.

- `changed`
  Execution only. Set `false` when the branch is exhausted or no safe change was made.

- `metadata`
  Optional structured extras. Keep it small.

Do not write free-form logs into `TREE_OUTPUT_PATH`.

## Sensing Contract

Use `TREE_SENSING_PATH` first.

Important fields:

- `recent_probe_results`
- `verifier`
- `budgets`
- `probe_guidance`

Probe requests are diagnostic-only. They inform decisions; they do not directly change acceptance.

### Probe classes

- inspection probes
  `read_text`, `read_json`, `list_dir`, `grep`

- generic experiment probes
  `command`, `python`

- stable-input probes
  `theoretical_limit_inputs`

- task probes
  domain-specific tools such as compile facts, `ncu`, `nsys`, runtime breakdowns

### Derived calculation flow

When you need roofline or theoretical headroom:

1. request `theoretical_limit_inputs`
2. read the returned stable inputs
3. write a built-in `python` probe containing the calculation script
4. return the computed bound/headroom in your own action output if it matters to the decision

This keeps formulas and assumptions in agent-authored code while the framework owns stable inputs and execution.

## Verifier Contract

Verifier is thin. It compresses current evidence into:

- `verdict`
  `keep | reject | need_more_evidence`

- `status`
  `healthy | warning | blocked`

- `evidence`
- `regressions`
- `uncertainty`
- `probe_health`

Planner should start from verifier before asking for more diagnostics.

## Runtime Invariants

These are the current framework invariants:

- search control belongs to the framework
- high-level route freezing belongs to the planner
- branch-local change belongs to the executor
- one-node insight belongs to the reflector
- probe execution and recording belong to sensing
- thin evidence compression belongs to verifier

Do not assume task-specific keys beyond what is present in the current payload or sensing manifest.

## Relationship Map

- architecture and module ownership:
  [docs/architecture.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/architecture.md)
- sensing details and probe model:
  [docs/sensing.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/sensing.md)
- verifier details:
  [docs/verifier.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/verifier.md)
- AutoKernel-specific long-loop task path:
  [program.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/program.md)
