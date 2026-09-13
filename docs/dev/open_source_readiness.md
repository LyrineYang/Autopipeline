# Open Source Readiness Notes

This is a maintainer checklist for publishing this repository as an independent
project built from the AutoKernel codebase. It is intentionally not part of the
public quickstart path.

## Current Strengths

- Clear MIT license.
- Runnable unit test suite.
- Agent runtime supports both Codex app-server sessions and legacy command
  scripts.
- Sensing has one shared probe runtime for dynamic tools and file probes.
- KernelBench and generic pipeline task surfaces plug into the same tree core.
- User docs now separate quickstart / architecture / runtime contract from
  development notes.
- CI covers the Python CLI, unit tests, wheel build, website lint/build, and a
  high-severity production dependency audit.
- Dependabot monitors Python, website, and GitHub Actions dependencies.

## Release Checklist

Use this as the release decision record. A public research-preview launch is
ready only when every required item is checked or explicitly waived with a
reason.

### Repository And Legal

- [x] Public identity is `AutoPipeline` / `auto-pipeline` and repository URLs
  point to `LyrineYang/Autopipeline`.
- [x] The upstream AutoKernel lineage is stated and the existing MIT copyright
  notice is preserved.
- [x] `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue
  templates, and the pull request template exist.
- [x] Runtime output, build output, local environments, credentials, and macOS
  metadata are ignored.
- [ ] Review the complete staged diff and repository history for private data
  immediately before making the repository public.

### Product Positioning And Documentation

- [x] README states what is stable, experimental, and not implemented.
- [x] Quickstart uses the supported `autopipeline` CLI and includes a CPU-safe
  path.
- [x] Architecture, agent contract, sensing, verifier, and maintainer notes are
  separated by audience.
- [x] The repo-defined KernelBench Level 5 direction is not presented as an
  upstream KernelBench feature.
- [ ] Add one reproducible, real GPU run artifact and a compact result example
  after the CUDA release smoke succeeds.

### Automated Quality Gates

- [x] Unit tests, Python compilation, CLI smoke checks, and whitespace checks
  run in CI.
- [x] Wheel construction runs in CI with explicit build dependencies.
- [x] Website dependency install, lint, production build, and high-severity
  production dependency audit run in CI.
- [x] Dependabot monitors Python, website, and GitHub Actions dependencies.
- [ ] Confirm the first CI run is green in the independent GitHub repository.

### Hardware And Agent Validation

- [ ] Run the documented profile / extract / tree smoke on a supported NVIDIA
  GPU and save the reproducible run artifacts.
- [ ] Run at least one KernelBench smoke and report correctness plus measured
  speedup or the concrete failure reason.
- [ ] Run one Codex app-server planner / executor / reflector smoke with valid
  credentials and review its event and request logs.
- [ ] Record profiler evidence (`ncu`, `nsys`, torch profiler, or an explicit
  unavailable reason) for the release example.

### Publication

- [ ] Authenticate GitHub CLI and push the reviewed release-prep branch.
- [ ] Open a draft PR, let CI complete, and review the final diff.
- [ ] Decide whether the first public release is an untagged research preview
  or a new version; do not attach current `Unreleased` changes to the existing
  `v1.3.0` tag.
- [ ] Make the repository public only after the preceding required gates pass.
- [ ] Publish the website only after its source is reviewed and the desired
  access level is confirmed.
- [ ] Keep PyPI publication blocked until the import namespace is finalized.

## Release Blockers

- Public identity now uses the default independent name `AutoPipeline`:
  - GitHub repository: `LyrineYang/Autopipeline`
  - Python distribution name: `auto-pipeline`
  - GitHub owner: personal account `LyrineYang`, not `RightNow-AI`.
  - GitHub URLs in `pyproject.toml`: `LyrineYang/Autopipeline`.
  If the personal GitHub username changes, update `pyproject.toml`, `origin`,
  and the upload commands before publishing.
- The old AutoKernel remote has been renamed locally to `autokernel-base`.
  Reserve `origin` for the new independent personal project repository.
- Verify GitHub CLI authentication locally with `gh auth status` before push /
  PR creation. If it fails, run `gh auth login -h github.com` and verify again.
- Current work is on `codex/open-source-release-prep`; publish through a draft
  PR or initial branch in the new repository, not the AutoKernel base remote.
- Package metadata is aligned with the latest changelog release, `1.3.0`.
  Confirm the next public release tag only when this work is ready to announce.
- Built wheels include library packages (`tree`, `kernelbench`, `pipeline`,
  `kernels`, and `models`) plus the `autopipeline` CLI. The command-line
  workflow is still source-checkout oriented; historical top-level scripts
  remain compatibility entrypoints.
- Confirm the public package name and PyPI strategy before uploading wheels.
- Do not publish to PyPI until the import namespace is finalized. The current
  wheel still exposes legacy top-level modules such as `tree`, which is fine for
  source-checkout development but too collision-prone for a broad package
  release. Prefer a future namespace such as `autopipeline.*` before public
  package distribution.

## Recommended Before Public Upload

- Verify the local release-prep branch:

  ```bash
  python3 -m unittest discover -s tests
  python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py
  python3 -m autopipeline --help
  python3 -m autopipeline doctor --no-gpu
  python3 -m autopipeline tree --help
  git diff --check HEAD~1..HEAD
  python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel-final
  cd site
  npm ci
  npm run lint
  npm run build
  npm audit --omit=dev --audit-level=high
  ```

  Install `setuptools` and `wheel` in the active Python environment before the
  no-build-isolation wheel command.

- Use the already-created private GitHub repository for the independent
  project:

  ```bash
  gh auth status
  git remote add origin https://github.com/LyrineYang/Autopipeline.git
  git push -u origin codex/open-source-release-prep
  ```

  Keep the repository private until the release checklist is complete. If
  authentication is missing, run `gh auth login -h github.com` or configure a
  Git credential that can push to the private repository, then rerun the push.
- If `origin` already exists but points elsewhere, correct it before pushing:

  ```bash
  git remote set-url origin https://github.com/LyrineYang/Autopipeline.git
  git push -u origin codex/open-source-release-prep
  ```

- Create a draft PR or initial release branch review from
  `codex/open-source-release-prep`.

  Suggested PR title:

  ```text
  Prepare AutoPipeline open source release workflow
  ```

  Suggested PR summary:

  ```markdown
  ## Summary

  - publish the project identity as AutoPipeline / auto-pipeline
  - split public user docs from maintainer development notes under docs/dev
  - add Codex app-server session runtime support for planner / executor / reflector roles
  - add CI, issue templates, PR template, contributing, security, and code of conduct files
  - harden tree search around accepted baselines, reverted candidates, final verify, timeouts, and role failures
  - fix package metadata and wheel contents for tree, KernelBench, pipeline, kernels, and models packages

  ## Validation

  - python3 -m unittest discover -s tests
  - python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py
  - python3 -m autopipeline --help
  - python3 -m autopipeline doctor --no-gpu
  - python3 -m autopipeline tree --help
  - git diff --check HEAD~1..HEAD
  - python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel-final
  ```

- Check README examples on a CUDA machine.
- Run at least one KernelBench smoke on GPU.
- Run one Codex app-server role smoke with valid Codex credentials.
- Review logs for secrets before sharing run artifacts.

## Nice To Have

- Add badges after CI is live.
- Add screenshots or a compact run-output example.
- Add a short roadmap issue for pipeline evaluator / profiler maturity.
- Add issue labels for `tree-runtime`, `kernelbench`, `pipeline`, `agent-runtime`,
  `sensing`, and `docs`.
