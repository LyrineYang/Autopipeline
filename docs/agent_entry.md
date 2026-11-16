# Agent Entry

Start here when integrating an agent or changing search. Use the [architecture map](architecture.md)
to find the owner of a change and the [agent contract](agent_contract.md) for the wire format.

1. [Planner](../autopipeline/search/planner.py) reads factual task context and returns branch proposals.
2. [Tree search](../autopipeline/search/engine.py) schedules and ranks candidates under the configured caps.
3. [Branch execution](../autopipeline/search/branch_engine.py) invokes the executor, evaluates the edited
   workspace, and records its result. A task can supply checkpoints for branch-local refinement.
4. [Role adapter](../autopipeline/search/agent.py) and [backend dispatch](../autopipeline/search/agent_backend.py)
   connect Codex app-server or an external command. Both use the same [sensing runtime](../autopipeline/search/sensing.py).
5. [Agent feedback](../autopipeline/search/feedback.py) summarizes measurements and diagnostic results.
   [Verification](verifier.md) defines correctness, acceptance, and export.

An executor edits only declared candidate files. The runtime fixes the reference and evaluates the
candidate independently. Agent output describes changes, and `result.json` records the product verdict.
[Run Artifacts](dev/run-artifacts.md)
