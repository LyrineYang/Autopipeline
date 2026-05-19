# Contributing

AutoPipeline is an independent project built from AutoKernel. It is moving from
a single-kernel agent workspace toward a task-agnostic tree runtime for kernel
and pipeline optimization.

## Local Checks

Run these before opening a pull request:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py
python3 -m autopipeline --help
python3 -m autopipeline doctor --no-gpu
git diff --check
python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel
```

GPU-dependent smoke tests are useful when you have a CUDA machine:

```bash
uv run autopipeline doctor
uv run autopipeline prepare
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run autopipeline extract --top 1
uv run autopipeline tree run --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick
```

## Boundaries

- Keep benchmark-layer, agent-runtime-layer, and evaluation-layer changes
  separate when possible.
- Keep the tree core task-agnostic; task-specific behavior belongs in task
  adapters or task-specific probes.
- Keep new user-facing commands behind the `autopipeline` CLI. Existing
  top-level scripts are compatibility entrypoints, not the preferred public
  interface.
- Preserve both sensing paths: app-server dynamic tools and the legacy
  `probe_requests.json` file flow should call the same probe handlers.
- Codex roles should use app-server sessions by default. The JSON command bridge
  remains a compatibility backend for scripts and non-Codex agents.

## Documentation

Update the relevant docs when behavior changes:

- `README.md` for human-facing setup and status
- `docs/quickstart.md` for runnable commands
- `docs/architecture.md` for module boundaries
- `docs/agent_contract.md` for role contracts
- `docs/sensing.md` for probe behavior
