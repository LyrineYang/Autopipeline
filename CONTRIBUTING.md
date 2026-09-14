# Contributing

AutoPipeline is an independent project built from AutoKernel. The alpha product provides
an installable pipeline workflow backed by a task-agnostic tree runtime, while
retaining the source-based kernel tools.

## Agent-Led Engineering

The human supplies intent, priorities, access, and consequential commitments.
The agent owns implementation, routine technical decisions, verification,
repair, and planning the next useful experiment. Incomplete ideas can start
with concrete usage scenarios and a runnable version; research and review are
not approval gates for each component.

Preserve the requested outcome while adjusting the implementation from evidence.
Small increments should lead to a complete working path, not stop at isolated
scaffolding. Scale context gathering and verification to the change and risk.

At a meaningful handoff, provide the runnable result and connect evidence to
what happens next: which claim matters most to validate, why it affects the
next decision, what prevents validation, and which experiment would resolve it.
Do not leave the human with only a list of verified and unverified claims.

Request critical resources early. Specify the smallest useful hardware, data,
access, or targeted human research; how the human can obtain it; estimated
cost/effort; the decision that belongs to them; and a recommended path. If the
resource is unavailable, explain what the fallback can establish and what still
requires the original resource. Continue independent work without overstating
proxy evidence or silently changing the goal.

Use a persistent goal for a complete outcome with auditable completion criteria,
not a fixed implementation recipe. Use explicitly scheduled follow-ups for
external waits and authorized continuation: inspect existing jobs, avoid
restarting live work, respect budgets and pauses, and stop obsolete follow-ups.
Notify the human about decisions or actionable changes rather than unchanged
status. Technical readiness is not human acceptance when acceptance is required.
See the official [Goals guide](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)
and [scheduled tasks guide](https://learn.chatgpt.com/docs/automations).

## Local Checks

Run these before opening a pull request:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py
python3 -m autopipeline --help
python3 -m autopipeline doctor --no-gpu
git diff --check
python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel
cd site
npm ci
npm run lint
npm run build
npm audit --omit=dev --audit-level=high
```

The wheel command requires `setuptools` and `wheel` in the active Python
environment. CI installs both explicitly before running the package checks.

GPU-dependent smoke tests are useful when you have a CUDA machine:

```bash
uv run autopipeline doctor
uv run autopipeline prepare
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run autopipeline extract --top 1
uv run autopipeline tree run --kernel-file workspace/kernel_matmul_1.py --run-name smoke-tree --beam-width 1 --max-rounds 1 --children-per-expansion 1 --bench-arg=--quick --executor-backend codex-app-server
```

## Branch ownership

`main` is the independent AutoPipeline product line. Submit product changes to
this repository's `origin`, not `autokernel-base`. The upstream remote is for
lineage and read-only comparison.

`codex/level5` and `codex/apimetric` are independent benchmark development lines.
Keep their task definitions, baselines, scoring and benchmark metrics there.
Share a callable task/evaluator contract with the product; do not merge benchmark
experiments into the product to make an integration test convenient. Existing
`codex/tree` and release-preparation branches are historical integration work.

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
