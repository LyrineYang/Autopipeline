# Quick Start

This page is the shortest practical path to a working setup.

## 1. Environment

Requirements:

- Python `3.10`
- `uv`
- NVIDIA GPU for the main optimization path

Setup:

```bash
uv python install $(cat .python-version)
uv venv --python $(cat .python-version)
uv sync
uv run prepare.py
```

## 2. AutoKernel-Compatible Path

Profile a model:

```bash
uv run profile.py --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
```

Extract hot kernels:

```bash
uv run extract.py --top 1
```

Check that the benchmark works:

```bash
uv run bench.py
```

At this point the AutoKernel-compatible loop is ready. See
[program.md](../program.md) for the
long-running agent path.

## 3. Tree Adapter Smoke Test

Run structural tests:

```bash
python3 -m unittest discover -s tests -v
```

Run a minimal tree smoke:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name smoke-tree \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick
```

Expected outputs:

- `workspace/tree_runs/<timestamp>-<kernel>/search_state.json`
- `workspace/tree_runs/<timestamp>-<kernel>/tree_events.jsonl`
- per-node files under `workspace/tree_runs/.../nodes/`
- a promoted `*_optimized.py` next to the source kernel

Other shipped entrypoints:

```bash
uv run kernelbench/tree_run.py --source hf --level 1 --problem 1 --beam-width 1 --max-rounds 1 --children-per-expansion 1
uv run pipeline/bridge.py smoke
```

## 4. Agent Runtime

Codex roles default to the app-server session backend when configured with a
Codex provider and no explicit legacy command:

```bash
uv run tree_optimize.py \
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

The session backend records thread id, turn id, turn status, token usage,
events, dynamic tool calls, and per-role session state under the node's
`agent/` directory.

The legacy command bridge is still available for local scripts and other
coding agents. It can shell out to three optional commands:

- `--planner-command`
- `--executor-command`
- `--reflector-command`

These commands receive:

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

This means an external planner / executor / reflector does not have to answer blindly.
Within the same step it can read sensing, request more diagnostics, read probe results, and only then write its final output.

The command bridge also writes `command_status.json`, `activity.jsonl`,
`timeout_diagnostic.json`, and `env_redacted.json` under the current step's
agent log directory.

The Codex CLI helper remains as a legacy command-bridge smoke path:

```bash
uv run tree_optimize.py \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name codex-wrapper-smoke \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick \
  --executor-command 'python3 scripts/codex_tree_agent.py execute --dry-run'
```

For the practical details, see
[docs/tree_adapter.md](tree_adapter.md).

## 5. What To Run Regularly

For day-to-day work, these are the most useful checks:

```bash
python3 -m unittest discover -s tests -v
uv run profile.py --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run extract.py --top 1
uv run tree_optimize.py --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick
```
