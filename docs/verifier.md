# Verifier

The verifier is a thin layer above sensing.

It does not collect facts. It compresses facts into a simple judgment.

## What It Returns

Today the verifier returns:

- `keep`
- `reject`
- `need_more_evidence`

It also returns supporting fields:

- evidence
- regressions
- uncertainty
- probe health

## Why It Is Thin

The goal is to keep this layer stable while probes and evidence sources evolve.

That means:

- sensing can grow new probes
- tasks can add new domain-specific evidence
- planner and executor can still read one stable verifier shape

The verifier should not become a second analytics system.

## Current Inputs

Today the verifier mainly looks at:

- recent probe results
- sensing's `objective_evaluation`
- current node execution state
- current node evaluation result
- parent score
- best known score
- task-specific verifier augmentation when the task exposes it

For pipeline-like tasks, sensing can build a small objective-evidence summary
from metrics such as end-to-end latency, throughput, stage imbalance,
communication time, sync time, overlap ratio, memory headroom, hardware fit, and
GPU utilization. The verifier consumes that summary without becoming the owner
of metric collection.

## Current Boundary

The verifier still does not run benchmarks, probes, or trace tools.
It does not replace the evaluator.
It only compresses evidence that already exists.

The pipeline evidence path is useful but still thin:

- it compares available scalar metrics
- it reports missing objective signals
- it can downgrade a branch when objective evidence regresses
- it cannot infer a full cross-kernel optimization diagnosis by itself

## Direction

The target is simple:

- richer evidence sources
- same thin output contract

That keeps the framework understandable while still allowing more serious system-level optimization.
