# Docs Index

This repository now uses two doc lines:

- human docs: what the project is, what works, how to run it, how to inspect outputs
- agent docs: a navigation entry plus a dense runtime contract for planner / executor / reflector

## Human Docs

- [README.md](../README.md): human entrypoint
- [docs/quickstart.md](quickstart.md): shortest practical smoke path
- [docs/architecture.md](architecture.md): current layer model and file map
- [docs/tree_adapter.md](tree_adapter.md): tree framework CLI, outputs, and command bridge
- [docs/sensing.md](sensing.md): active sensing, probes, and result recording
- [docs/verifier.md](verifier.md): thin verifier and current verdict model
- [CONTRIBUTING.md](../CONTRIBUTING.md): local checks and contribution boundaries
- [kernelbench/tree_run.py](../kernelbench/tree_run.py): KernelBench tree entrypoint
- [pipeline/bridge.py](../pipeline/bridge.py): generic pipeline task entrypoint

## Agent Docs

- [docs/agent_entry.md](agent_entry.md): runtime navigation entry for framework-driven agents
- [docs/agent_contract.md](agent_contract.md): dense runtime contract for framework-driven agents
- [program.md](../program.md): long-running AutoKernel-compatible agent instructions
- [kernelbench/program_kb.md](../kernelbench/program_kb.md): KernelBench-specific runtime instructions
- [scripts/codex_tree_agent.py](../scripts/codex_tree_agent.py): Codex CLI executor wrapper for tree command contract
