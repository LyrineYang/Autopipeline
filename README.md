# AutoPipeline

AutoPipeline is an agent-first GPU kernel and pipeline optimization workspace
built from the AutoKernel implementation base. It is now an independent
personal project hosted at `LyrineYang/Autopipeline`.

The default user entrypoint is the `autopipeline` CLI. Historical top-level
scripts still work for compatibility, but new workflows should use the CLI so
the public path stays stable.

## Status

- stable enough to use: AutoKernel-compatible profile / extract / benchmark flow
- experimental but usable: tree search for controlled branch-based optimization
- experimental: KernelBench and generic pipeline task adapters
- not yet public package distribution: use the source checkout; do not publish
  this wheel to PyPI until the import namespace is finalized

## Quick Start

Requirements:

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)
- NVIDIA GPU for the main optimization path

Set up the source checkout:

```bash
uv python install $(cat .python-version)
uv venv --python $(cat .python-version)
uv sync
uv run autopipeline doctor
```

No GPU available? Run the CPU-safe checks:

```bash
uv run autopipeline doctor --no-gpu
python3 -m unittest discover -s tests
```

Prepare the workspace, profile a model, and extract one starter kernel:

```bash
uv run autopipeline prepare
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run autopipeline extract --top 1
```

Run a minimal tree search on the extracted kernel:

```bash
uv run autopipeline tree run \
  --kernel-file workspace/kernel_matmul_1.py \
  --run-name smoke-tree \
  --beam-width 1 \
  --max-rounds 1 \
  --children-per-expansion 1 \
  --bench-arg=--quick
```

Inspect a tree run:

```bash
uv run autopipeline report workspace/tree_runs/<run-dir>
```

Expected run outputs:

- `workspace/profile_report.json`
- `workspace/pipeline_profile.json`
- `workspace/kernel_<type>_<rank>.py`
- `workspace/tree_runs/<run>/search_state.json`
- `workspace/tree_runs/<run>/tree_events.jsonl`
- per-node agent and sensing logs under `workspace/tree_runs/<run>/nodes/`

## What The CLI Wraps

- `autopipeline doctor`: validate the local checkout, Python stack, torch,
  Triton, and optional CUDA access
- `autopipeline prepare`: generate cached data and baseline timings
- `autopipeline profile`: profile a model and write `profile_report.json`
- `autopipeline extract`: create starter kernels from the profile report
- `autopipeline tree run`: run branch-based search through the tree runtime
- `autopipeline report`: summarize a tree run
- `autopipeline bench` / `autopipeline verify`: advanced compatibility commands

## Lineage

AutoPipeline is built from the AutoKernel codebase. The AutoKernel-compatible
single-kernel path remains available, while the tree runtime, sensing layer,
KernelBench adapter, and pipeline bridge move the project toward broader
pipeline co-optimization.

The original AutoKernel repository is tracked locally as `autokernel-base` for
lineage and reference. New project work should target the `origin` remote.

## Documentation

- [docs/quickstart.md](docs/quickstart.md): practical CLI workflow and smoke tests
- [docs/architecture.md](docs/architecture.md): current layer model and module map
- [docs/tree_adapter.md](docs/tree_adapter.md): tree runtime details and outputs
- [docs/sensing.md](docs/sensing.md): active sensing and probe recording
- [docs/verifier.md](docs/verifier.md): thin verifier behavior
- [docs/agent_entry.md](docs/agent_entry.md): agent runtime navigation
- [docs/agent_contract.md](docs/agent_contract.md): planner / executor / reflector contract
- [CONTRIBUTING.md](CONTRIBUTING.md): checks and contribution boundaries
- [SECURITY.md](SECURITY.md): local execution safety notes

Maintainer-only design notes live under `docs/dev/` and are intentionally kept
out of the user quickstart path.
