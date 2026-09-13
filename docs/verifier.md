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

## Pipeline Executable Verification Contract

The installed PyTorch harness computes the largest elementwise normalized error
over all output tensor elements and multiple seeded inputs. The backend accepts
this summary only with a positive element count, finite nonnegative ratio, and
tolerances matching its own configuration; a ratio above 1 fails. Explicit output
payloads, when provided, still take priority over summary statistics.

Public pipeline runs first establish a numerically valid, measurable baseline.
This initial measurement does not have to beat a separate timing of itself.
Unchanged promoted baselines follow that same rule, with the performance result
still recorded. Edited candidates retain the no-regression gate. Reports record
which policy was used; a valid baseline is not an optimization gain.

The pipeline verification backend runs reference and candidate commands. It
accepts a candidate only when both commands succeed and all of the following
evidence is present:

- Both metrics objects explicitly report passing `correctness` (`PASS`,
  `PASSED`, `TRUE`, or `OK`, case-insensitive). A missing status is unknown,
  not success; `accepted: false` also rejects the run.
- Both outputs are comparable, or the evaluator supplies finite, non-negative
  maximum absolute or relative error measurements. When either run provides an
  output, both must provide one and the actual output comparison takes priority
  over error summaries. Outputs must preserve dictionary keys, list structure,
  and non-numeric values. Numeric leaves must be finite and satisfy
  `abs(candidate - reference) <= atol + rtol * abs(reference)`.
- Both runs provide the finite numeric metric named by `score_metric`, and the
  candidate does not regress in the declared direction. Missing metrics do not
  fall back to a different objective.

For error-summary-only evaluators, at least one supplied maximum error must
meet its corresponding tolerance; absent errors stay absent, and every supplied
error must be valid. Default tolerances are `atol=1e-5` and `rtol=1e-4`.
The evaluator remains responsible for producing trustworthy output/error and
performance measurements; this backend does not independently measure GPU time.

Missing evidence produces a rejected backend result with an explicit failure
reason. It must not be used to promote a candidate. A configured
`final_verify_command` must produce the same correctness, output/error, and
score contract as the evaluator. Final verification uses the same backend and
removes old metrics before executing, so an exit code of zero alone cannot pass.
Detailed reports and command logs are saved under `final_verification/`.

## Direction

The target is simple:

- richer evidence sources
- same thin output contract

That keeps the framework understandable while still allowing more serious system-level optimization.
