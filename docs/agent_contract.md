# Agent Contract

This page is the dense runtime contract for framework-driven agents.

Read [docs/agent_entry.md](agent_entry.md) first if you need navigation.
Read [program.md](../program.md) instead if you are operating the original long-running AutoKernel-compatible path.

## Action Contract

### Planner

- reads:
  - prompt
  - payload
  - sensing
  - optional telemetry
- writes:
  - app-server mode: one JSON object in the final message
  - legacy command mode: one JSON object to `TREE_OUTPUT_PATH`
- owns:
  - branch identities
  - scope / target
  - branch budget hints
  - stop conditions
  - frozen decisions
- may:
  - request probes before finalizing a planner packet
  - include `llm_usage` in output for budget accounting

### Executor

- reads:
  - prompt
  - payload
  - sensing
  - optional telemetry
- writes:
  - app-server mode: one focused code change; the runtime summarizes file changes
  - legacy command mode: one JSON object to `TREE_OUTPUT_PATH`
- owns:
  - one local step
  - local diagnostic probes when needed
- must not:
  - reopen planner route
  - edit control files unless the task explicitly allows it
- may:
  - stop with `changed=false` when the branch is exhausted
  - include `llm_usage`, `usage`, or `metadata.usage` for budget accounting

### Reflector

- reads:
  - prompt
  - payload
  - sensing
  - optional telemetry
- writes:
  - app-server mode: one JSON object in the final message
  - legacy command mode: one JSON object to `TREE_OUTPUT_PATH`
- owns:
  - one compressed node insight

## Planner Limits

Within `search_policy.branch_budget`, `max_tree_depth` caps expansion depth and
`max_local_iterations` caps edit attempts inside a branch. Older planner outputs can use
`rounds` to set both. `children` and `beam_width` constrain expansion and retention.
`stop_guidance` is advisory text for the executor. The search engine enforces the numeric caps.

## Backend Contract

Codex roles default to the app-server session backend. The runtime creates or
resumes a per-role thread, starts a turn, records events, and surfaces failed
turns as role failures.

The file contract below applies to the legacy JSON command bridge. App-server
turns receive equivalent prompt / payload / telemetry / sensing content inside
the turn prompt, and use `run_sensing_probe` for dynamic probe calls.

## Legacy File Contract

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

- `TREE_CANCEL_PATH`
  Optional cancellation sentinel. The command bridge creates the path value and
  terminates the subprocess if the file appears.

- `TREE_ACTIVITY_PATH`
  Activity JSONL path. The bridge writes activity observations there; commands
  may also append concise structured progress records if useful.

- `TREE_AGENT_ROLE`
  Current role name: `planner`, `executor`, or `reflector`.

- `TREE_PROBE_REQUESTS_PATH`
  Optional. Write a JSON list of diagnostic probe requests when you need more evidence.

- `TREE_PROBE_RESULTS_PATH`
  Optional. Read structured probe results written by the sensing runtime during the same step.

### Role Config Environment

When role-specific config is provided through CLI flags, the bridge injects:

- `TREE_AGENT_PROVIDER`
- `TREE_AGENT_MODEL`
- `TREE_AGENT_BASE_URL`
- `TREE_AGENT_REASONING_EFFORT`
- `TREE_AGENT_MAX_OUTPUT_TOKENS`
- `TREE_AGENT_TEMPERATURE`
- `TREE_AGENT_SUBAGENT`
- `TREE_AGENT_TIMEOUT_SECONDS`

These are hints for the command wrapper. The framework does not require a
specific model provider protocol.

### Command Log Files

For every external command, [autopipeline/search/command.py](../autopipeline/search/command.py)
writes a per-step log directory containing:

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

`command_status.json` records lifecycle status, return code, duration, timeout,
cancellation, and failure class. `timeout_diagnostic.json` records tails of
stdout/stderr/output/probe files when timeout, cancellation, nonzero exit,
missing output, or invalid output occurs.

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

- `llm_usage`, `usage`, `metadata.llm_usage`, or `metadata.usage`
  Optional usage object. `BudgetLedger` accepts common fields such as
  `api_calls`, `prompt_tokens`, `completion_tokens`, `total_tokens`, and
  `cost_usd`.

Do not write free-form logs into `TREE_OUTPUT_PATH`.

## Command Lifecycle

For a planner / executor / reflector command, the runtime does this:

1. write prompt, payload, telemetry, empty sensing probe files, command status, and redacted env snapshot
2. launch the configured shell command in the role cwd
3. poll stdout, stderr, output, probe request, and probe result file sizes into `activity.jsonl`
4. serve pending probe requests while the subprocess is still running
5. terminate the process group if `TREE_CANCEL_PATH` appears or timeout is reached
6. finalize sensing once the process exits
7. require a zero return code and valid JSON at `TREE_OUTPUT_PATH`
8. record usage, duration, timeout, cancellation, and failure class in `BudgetLedger`

Failure classes are diagnostic hints. Current examples include
`zero_call_timeout`, `probe_wait_loop`, `command_hung`,
`provider_unreachable_or_auth`, `benchmark_or_compile_failure`,
`missing_output`, `invalid_output`, and `cancelled`.

## Sensing Contract

Use `TREE_SENSING_PATH` first.

Important fields:

- `recent_probe_results`
- `verifier`
- `budgets`
- `probe_guidance`

Probe requests are diagnostic-only. They inform decisions; they do not directly
change acceptance.

The command bridge actively serves them while the command runs. A command can
rewrite `TREE_PROBE_REQUESTS_PATH`; the bridge detects the change, executes up
to the configured per-action probe limit, writes `TREE_PROBE_RESULTS_PATH`, and
refreshes `TREE_SENSING_PATH`.

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
- subprocess lifecycle, timeout, cancellation, activity recording, and command diagnostics belong to the command bridge
- API/token/cost/probe/family accounting belongs to `BudgetLedger`

Do not assume task-specific keys beyond what is present in the current payload or sensing manifest.

## Relationship Map

- architecture and module ownership:
  [docs/architecture.md](architecture.md)
- sensing details and probe model:
  [docs/sensing.md](sensing.md)
- verifier details:
  [docs/verifier.md](verifier.md)
- AutoKernel-compatible long-loop task path:
  [program.md](../program.md)
