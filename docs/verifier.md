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
- current node execution state
- current node evaluation result
- parent score
- best known score

That is enough for a usable branch-level summary, but not enough for full infra or pipeline judgment.

## Future Direction

As the framework moves toward pipeline and infra work, the verifier should keep the same outer shape while consuming richer evidence:

- kernel-level results
- stage-level timing
- overlap and communication signals
- end-to-end latency or throughput
- hardware fit for the current machine and software stack

The target is simple:

- richer evidence sources
- same thin output contract

That keeps the framework understandable while still allowing more serious system-level optimization.
