# Branch and Worktree Map

| Branch | Scope |
| --- | --- |
| `main` | Installable AutoPipeline, its verification workflow, and source compatibility tools. This is the default branch on `origin`. |
| `research/level5` | Repository-defined Level 5 tasks, benchmark baselines, and apimetric research. |

Keep product changes on `main`. The research branch combines the former `codex/level5` and
`codex/apimetric` lines. Its merge parents retain both histories. The source KernelBench
adapter in `main` uses the product task protocol and has its own benchmark verdicts.
[Architecture](../architecture.md)

Use `git worktree list` for checkout paths on the current machine. Local weights, virtual
environments, and run outputs belong in `workspace/` or a configured external location.
[Handoff](handoff.md)

## History

The March AutoKernel base is `7843582`. Earlier release-preparation and tree worktrees were
retired after their code entered `main`. The old release-preparation remote branch is historical.
Git preserves the merged commits. Local snapshots and run artifacts are separate from the release.
[Cleanup review](cleanup-review.md)

- `origin` is the AutoPipeline repository for contributions.
- `autokernel-base` is the upstream comparison remote.
- The project website is maintained separately from wheel builds and library CI.
