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

## 2. Main AutoKernel Path

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

At this point the original AutoKernel loop is ready. See [program.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/program.md) for the long-running agent path.

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

## 4. External Agent Commands

The tree adapter can shell out to three optional commands:

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

This means an external planner / executor / reflector does not have to answer blindly.
Within the same step it can read sensing, request more diagnostics, read probe results, and only then write its final output.

For the practical details, see [docs/tree_adapter.md](/Users/nianzhen/.codex/worktrees/3fc8/autokernel/docs/tree_adapter.md).

## 5. What To Run Regularly

For day-to-day work, these are the most useful checks:

```bash
python3 -m unittest discover -s tests -v
uv run profile.py --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run extract.py --top 1
uv run tree_optimize.py --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick
```
