# Quick Start

This page is the shortest practical path through the stable source-checkout
workflow. Use the `autopipeline` CLI for new work; the older top-level scripts
remain as compatibility entrypoints.

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
uv run autopipeline doctor
```

On a machine without a GPU, use the CPU-safe check:

```bash
uv run autopipeline doctor --no-gpu
python3 -m unittest discover -s tests
```

## 2. Default Optimization Flow

Prepare cached data and baselines:

```bash
uv run autopipeline prepare
```

Profile a model:

```bash
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
```

Extract the top hot kernel:

```bash
uv run autopipeline extract --top 1
```

Run a minimal tree search:

```bash
uv run autopipeline tree run \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name smoke-tree \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick
```

Summarize a run:

```bash
uv run autopipeline report workspace/tree_runs/<run-dir>
```

Expected outputs:

- `workspace/profile_report.json`
- `workspace/pipeline_profile.json`
- `workspace/kernel_<type>_<rank>.py`
- `workspace/tree_runs/<run>/search_state.json`
- `workspace/tree_runs/<run>/tree_events.jsonl`
- `workspace/tree_runs/<run>/nodes/<node>/agent/`
- `workspace/tree_runs/<run>/nodes/<node>/sensing/`

## 3. Agent Runtime

Codex roles default to the app-server session backend when configured with a
Codex provider and no explicit legacy command:

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

The legacy command bridge is still available for local scripts and other coding
agents:

```bash
uv run autopipeline tree run \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name codex-wrapper-smoke \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick \
  --executor-command 'python3 scripts/codex_tree_agent.py execute --dry-run'
```

For the full tree runtime details, see [docs/tree_adapter.md](tree_adapter.md).

## 4. Regular Checks

Run these before pushing:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py
git diff --check
python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel
```
