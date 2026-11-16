# Developer Guide

The [Quickstart](../quickstart.md) covers the installed workflow. This guide covers runtime
extensions, agent protocols, compatibility tools, and maintenance.

## Runtime and Interfaces

- [Architecture](../architecture.md) describes the task, search, evaluator, and verifier.
- [Agent entrypoints](../agent_entry.md) and the [agent contract](../agent_contract.md) cover executor,
  planner, and reflector integrations.
- [Sensing](../sensing.md) and [verification](../verifier.md) describe evidence collection and verdicts.
- [Run artifacts](run-artifacts.md) lists logs, receipt fields, and installed-workflow checks.
- [Product engineering standard](product-engineering-standard.md) describes packaging and extension boundaries.

Use `autopipeline --help-all` and `autopipeline pipeline --help-all` to discover developer commands.
`pipeline info` inspects a spec, `pipeline setup` prepares a workspace, and `pipeline smoke`
exercises the synthetic search harness.

## Connect a Custom Agent

Pass `--executor-command 'YOUR_COMMAND'`. AutoPipeline starts the command in the candidate
workspace and passes the prompt and payload via `TREE_PROMPT_PATH` and `TREE_PAYLOAD_PATH`.
The command edits declared files and writes its result to `TREE_OUTPUT_PATH`.

```json
{"status":"completed","changed":true,"summary":"Describe the implementation change"}
```

The external command handles its own authentication. AutoPipeline verifies the candidate separately.
[Agent contract](../agent_contract.md)

## Compatibility Tools

The original kernel workflow runs from a source checkout with GPU dependencies and an agent.
It profiles a model, extracts kernels, and runs search against an extracted implementation.

```sh
uv run autopipeline prepare
uv run autopipeline profile --model models/gpt2.py --class-name GPT2 --input-shape 1,1024 --dtype float16
uv run autopipeline extract --top 1
uv run autopipeline kernel run --kernel-file workspace/kernel_matmul_1.py \
  --beam-width 1 --max-rounds 1 --children-per-expansion 1 \
  --bench-arg=--quick --executor-backend codex-app-server
```

`autopipeline tree run` remains a compatibility alias. The native pipeline starts directly from
the supplied model implementation. See the
[tree adapter guide](../tree_adapter.md) for the compatibility commands.

## External Benchmarks

Benchmark adapters provide a fixed workspace, editable paths, and evaluation command. The
benchmark owns task versions, correctness, baselines, and statistics. Before an agent comparison,
run the same task through the benchmark harness and the adapter and check that their verdicts agree.

Level 5 and apimetric are developed separately from the product. See the
[branch and worktree map](worktree-map.md) for their history.

## Maintenance

- [Handoff](handoff.md) gives the current state and the RTX 4060 experiment entrypoint.
- [Release checks](open_source_readiness.md) cover validation, packaging, and publication.
- [Contributing](../../CONTRIBUTING.md) documents local tests and change boundaries.
- [Cleanup review](cleanup-review.md) and [interactions](run_interaction_metrics.md) record earlier decisions.

### Historical Design

- [Agent design context](../agent_design_context.md)
- [Planner v1](planner_v1.md)
- [Tree planner consensus](tree_planner_consensus.md)
- [Agentic optimization lessons](agentic_optimization_lessons.md)
- [App-server integration research](symphony_agent_runtime_gap_report.md)
