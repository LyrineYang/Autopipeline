# Agent Design Context

This page gives development agents the current design boundaries. It is not a
runtime contract; use [docs/agent_contract.md](agent_contract.md) for exact
planner / executor / reflector I/O.

## Current Stage

The first-stage research target is pipeline co-optimization for a fixed
hardware environment, task, and workload.

In practical terms, the framework should help an agent:

- read task, profile, hardware, sensing, and history evidence
- identify the relevant bottleneck region
- choose branch identities that correspond to distinct optimization mechanisms
- run isolated candidate trials in sandboxes
- let evaluators and verifiers decide keep / reject / need more evidence
- preserve useful success and failure motifs for later planner calls

The longer-term direction is an infrastructure-engineer agent that can transfer
optimization experience across hardware, workloads, and tasks. Do not describe
that broader goal as already implemented.

## Runtime Ownership

Use these ownership boundaries when making changes:

- Brain: planner, executor, and reflector roles. They reason and act inside a
  bounded role.
- Hands: sandboxes, editable workspaces, shell commands, profiler runs, and
  task-specific tools.
- Session: app-server thread / turn lifecycle, event logs, request logs, token
  usage, role state, and resumability. Session is the agent management surface.
- Evidence: sensing records, evaluator outputs, verifier summaries, archive
  motifs, and run-view artifacts.

Session is not the optimization algorithm. It is a modern way to manage
long-running agents. It exists so runs are observable, resumable, debuggable,
and replaceable as model harnesses evolve.

Sensing is not a dynamic-tool layer. Dynamic tools are only one transport for
calling sensing from app-server sessions. The legacy file bridge calls the same
sensing runtime through probe request files.

## Design Rules

- Keep evaluator and verifier judgment outside the agent's self-report.
- Keep sensing as the evidence execution and recording layer.
- Treat `run_sensing_probe` as a transport adapter, not as the owner of probe
  semantics.
- Keep planner focused on route, scope, branch identity, budget hints, and
  stop conditions.
- Keep executor focused on one branch-local implementation step.
- Keep reflection as compressed evidence-backed insight, not as a judge.
- Keep archive records auditable. Do not turn them into unbounded global agent
  memory.

## Pipeline Boundary

Current implementation supports a spec-driven pipeline workspace task. It can:

- copy a declared workspace into a branch sandbox
- run a declared evaluator command
- read objective metrics
- expose simple pipeline probes
- promote and optionally final-verify a winning workspace

It does not yet provide:

- production trace ingestion
- automatic bottleneck-region construction from traces
- cross-kernel editing
- fusion or layout-propagation transforms
- launch consolidation
- stage reordering
- communication / compute overlap scheduling
- cross-hardware generalization

When implementing pipeline work, keep the next abstraction centered on:

```text
observation -> diagnosis -> treatment family -> branch identity
```

Observation is what happened. Diagnosis is why it likely happened. Treatment
family is what class of intervention is worth trying. Branch identity freezes
that choice for executor work.

## Common Misreads To Avoid

- Do not say session replaced sensing.
- Do not say dynamic tools own sensing.
- Do not say the generic pipeline task is a production pipeline optimizer.
- Do not make benchmark results the only planner prior for pipeline work.
- Do not let pipeline claims outrun the current evaluator and workspace
  contract.
- Do not introduce a new scheduler when the tree runtime can carry the work.

## Reference Direction

The session design is influenced by Anthropic Managed Agents, OpenAI Symphony,
and the OpenAI Agents SDK evolution: stable interfaces should outlast any one
agent harness. In this repository, those stable interfaces are the task
boundary, role boundary, session boundary, sensing/evidence boundary, and
evaluator/verifier boundary.
