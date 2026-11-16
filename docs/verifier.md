# Verification and Agent Feedback

[Verification backend](../autopipeline/pipeline/verification_backend.py) runs the reference and
candidate commands independently. It requires usable correctness, numerical error, and objective
evidence before accepting the candidate. [Native evaluator](../autopipeline/evaluate.py) compares
seeded outputs, measures synchronized forward latency, and records profiler evidence.

For native latency workloads, the ranking score is paired speedup. The comparison uses the
reference measurement from the same verification report. Custom evaluators continue to rank by
the normalized objective from their specs. The final recheck evaluates the selected code again
against the fixed reference, and export checks the recorded hashes.

[Agent feedback](../autopipeline/search/feedback.py) surfaces the verifier's result, recent probes,
and potential regressions to the agent. Its advisory verdict can include diagnostic concerns.
The historical wire field `verifier` in sensing payloads carries this summary. Selection and export
use the independent evaluation verdict and final receipt. [Run artifacts](dev/run-artifacts.md)
