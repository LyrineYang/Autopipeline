# Docs Index

This repository uses two public doc lines:

- human docs: what the project is, how to run the `autopipeline` CLI, and how
  to inspect outputs
- agent docs: a navigation entry plus a dense runtime contract for planner /
  executor / reflector

## Human Docs

- [README.md](../README.md): human entrypoint
- [Examples](../examples/README.md): runnable workloads, local pretrained models, and resource preparation
- [docs/quickstart.md](quickstart.md): shortest practical CLI path
- [docs/architecture.md](architecture.md): current layer model and file map
- [docs/tree_adapter.md](tree_adapter.md): tree runtime outputs and command bridge
- [docs/sensing.md](sensing.md): active sensing, probes, and result recording
- [docs/verifier.md](verifier.md): thin verifier and current verdict model
- [docs/agent_design_context.md](agent_design_context.md): current design
  boundaries for development agents
- [CONTRIBUTING.md](../CONTRIBUTING.md): local checks and contribution boundaries
- [Worktree inventory](dev/worktree-map.md): local branch relationships and uncommitted-work boundaries

## Agent Docs

- [docs/agent_design_context.md](agent_design_context.md): design boundaries
  and common misreads to avoid
- [docs/agent_entry.md](agent_entry.md): runtime navigation entry for framework-driven agents
- [docs/agent_contract.md](agent_contract.md): dense runtime contract for framework-driven agents
- [program.md](../program.md): long-running AutoKernel-compatible agent instructions
- [kernelbench/program_kb.md](../kernelbench/program_kb.md): KernelBench-specific runtime instructions
- [scripts/codex_tree_agent.py](../scripts/codex_tree_agent.py): Codex CLI executor wrapper for tree command contract
