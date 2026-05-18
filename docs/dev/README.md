# Development Notes

This directory contains design notes, research logs, and architecture rationale.
These files are useful for maintainers and research planning, but they are not
part of the stable user-facing documentation path.

User-facing docs live one level up:

- `../quickstart.md`
- `../architecture.md`
- `../tree_adapter.md`
- `../sensing.md`
- `../verifier.md`
- `../agent_entry.md`
- `../agent_contract.md`

Current development notes:

- `tree_planner_consensus.md`: historical design consensus for the tree planner
- `planner_v1.md`: planner background and early design framing
- `agentic_optimization_lessons.md`: external agent orchestration lessons mapped to this runtime
- `run_interaction_metrics.md`: run viewer and interaction metric semantics
- `symphony_agent_runtime_gap_report.md`: Codex app-server / Symphony runtime gap analysis
- `open_source_readiness.md`: maintainer checklist for public release readiness

Keep new files here when they explain why the system is designed a certain way,
what tradeoffs were considered, or how future research should be framed. Put
stable how-to material in the user docs instead.
