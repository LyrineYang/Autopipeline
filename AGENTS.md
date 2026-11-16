# AutoPipeline repository rules

Maintain the installed PyTorch search and verification workflow on `main`. The original
extracted-kernel commands use a source checkout. Level 5 and apimetric are independent
research under `research/level5`. Level 5 is a repository-defined benchmark extension.

- Use the `autopipeline` package for new runtime imports. Put app orchestration in
  [workflow.py](autopipeline/workflow.py), search in [search/](autopipeline/search/), and
  fixed-workload assessment in [pipeline/](autopipeline/pipeline/).
- Preserve the fixed reference, declared editable paths, correctness gate, profiler evidence,
  call budgets, and final recheck. Verify both acceptance and rejection after changes.
- Keep measurements, search ranking, advisory feedback, and the product verdict distinct.
  Report proposals, executions, evaluations, budget skips, and agent calls by their actual meaning.
- Use English for public documentation. Put the runtime map and terms in [Architecture](docs/architecture.md),
  stable artifact semantics in [Run Artifacts](docs/dev/run-artifacts.md), and current evidence and the
  next experiment in [Handoff](docs/dev/handoff.md). Keep machine-specific notes and run outputs in `workspace/`.
- Verify package and CLI changes from an installed wheel outside the checkout. Record real CUDA
  and agent results when available, with the workload, comparison method, and tested revision.
