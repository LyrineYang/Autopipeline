# Contributing

AutoPipeline is an independent project built from AutoKernel. It is moving from
a single-kernel agent workspace toward a task-agnostic tree runtime for kernel
and pipeline optimization.

## Local Checks

Run these before opening a pull request:

```bash
python3 -m unittest discover -s tests
git diff --check
```

GPU-dependent smoke tests are useful when you have a CUDA machine:

```bash
uv run prepare.py
uv run profile.py --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run extract.py --top 1
uv run tree_optimize.py --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick
```

## Boundaries

- Keep benchmark-layer, agent-runtime-layer, and evaluation-layer changes
  separate when possible.
- Keep the tree core task-agnostic; task-specific behavior belongs in task
  adapters or task-specific probes.
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
