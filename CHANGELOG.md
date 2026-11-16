# Changelog

## Unreleased

### Task-oriented user workflow (1.4.0a5)

- Subcommand help separates common options from `COMMAND --help-all`; existing
  options remain accepted, with device defaults and call limits visible.
- Init, check, evaluation and export explain effects and print copyable next
  commands. Generated project guides complete the no-agent path before search.
- Terminal, Markdown and HTML reports lead with the product outcome, including
  no improvement, failure and interruption. Early failures can be inspected
  before search state exists; raw result status and JSON readiness remain compatible.
- Separate CLI definitions and presentation from workload execution. Preserve
  legacy commands, design documents and independent benchmark lines.
- Correct the build-backend lower bound for the declared license metadata.

### User and developer entrypoints (1.4.0a4)

- Default CLI help now presents the product workflow; `--help-all` exposes
  developer utilities and source-based compatibility commands.
- Separated user navigation from agent protocols, benchmark integration,
  compatibility tooling and maintainer documentation. Existing commands and
  design documents remain available.

### Product cleanup (1.4.0a3)

- Unified the installed report command and legacy script, including diagnostics
  for missing run data; preserved the historical script entrypoint.
- Moved synthetic harness fixtures out of the pipeline task implementation and
  load them only for smoke runs, with compatibility for existing imports.
- Retired eight merged historical worktrees and separated the website from the
  library checkout. Design documents and independent benchmarks are retained.

### Product workflow and examples

- Consolidated the installable pipeline flow: initialize, check, evaluate, search,
  report, export, and independently recheck candidates.
- Added packaged residual MLP, attention, and offline local encoder examples.
- Added checks for changed external model assets and protected evaluator files.
- Separated the legacy profile CLI from Python's standard profiler so importing
  Transformers / torch compilation works from the source checkout.
- Established an independent product mainline while retaining Level 5 and
  apimetric as separate benchmark development lines.


### Tree Runtime And Agent Sessions

- Added a task-agnostic tree runtime path for AutoKernel-compatible,
  KernelBench, and generic pipeline workspace tasks.
- Added Codex app-server session support for planner, executor, and reflector
  roles, while keeping the legacy JSON command bridge for scripts and other
  coding agents.
- Added shared sensing probe handling for app-server dynamic tools and
  `probe_requests.json` file probes.
- Added branch-local search hardening around accepted baselines, reverted
  candidates, final verification, timeouts, and role failures.

### Open Source Readiness

- Added the `autopipeline` CLI as the preferred user entrypoint while keeping
  historical top-level scripts available for compatibility.
- Split user-facing docs from maintainer development notes under `docs/dev/`.
- Added contribution, security, code of conduct, GitHub issue, pull request,
  and CI scaffolding for public repository maintenance.
- Updated package metadata so bundled kernel and model packages are included in
  built wheels.
- Prepared public project identity for the independent AutoPipeline repository.

## v1.3.0 -- 2026-03-13

### AMD ROCm GPU Support (PR #3 by @andyluo7)

- Added AMD Instinct MI300X, MI325X, MI350X, MI355X to GPU database with correct peak FP16 TFLOPS, memory bandwidth, and L2 cache specs
- Added `gcnArchName`-based GPU detection for ROCm (device name is often empty on ROCm; `gcnArchName` like `gfx942` is always available)
- Guarded `clock_rate` access behind `hasattr` + `> 0` check (ROCm devices report `clock_rate=0`)
- Applied same fixes to `profile.py` fallback detector
- Tested on AMD Instinct MI300X (gfx942, ROCm 6.3) and MI350X (gfx950, ROCm 7.2)

### Bug Fixes

- **Fixed `verify.py` SyntaxError on Python 3.13+**: moved `global` declaration before variable usage in `main()` -- file would not even import on Python 3.13/3.14
- **Fixed CUDA flash_attention `sm_scale` parameter being ignored**: the `sm_scale` argument was accepted but the kernel hardcoded `rsqrtf(D)` instead of using it -- now correctly passes `sm_scale` through to the CUDA kernel
- **Fixed CUDA cross_entropy returning wrong dtype**: loss was cast back to input dtype instead of always returning `float32` (matching `F.cross_entropy` behavior)
- **Fixed Triton rotary_embedding broadcasting truncation**: `cos`/`sin` repeat used integer division which truncated when `n_rows` was not a multiple of `cos.shape[0]` -- now uses ceiling division and slices to exact size
- **Fixed Triton reduce output shape for non-last-dim reductions**: after permuting to move the reduce dim last, the output was reshaped using the original dim order instead of the permuted order

## v1.2.0 -- 2026-03-12

### Enhanced Profiler (Issue #1)

- Added `--export-trace` flag to export Chrome trace JSON for HTA/trace-blame analysis
- Added `--memory-snapshot` flag to capture CUDA memory snapshots for mosaic analysis
- Added `--torch-compile-log` flag to save torch.compile logs for tlparse analysis
- Added optional HTA (Holistic Trace Analysis) integration -- when installed, runs temporal and kernel breakdown analysis
- Added exported artifacts summary with suggested next steps for each tool
- Added `HolisticTraceAnalysis` as optional `profiling` dependency

### HuggingFace Kernels Export (Issue #2)

- Added `export_hf.py` -- exports optimized kernels to HuggingFace Kernels format
- Supports CUDA C++ kernels: auto-extracts CUDA source, parses function signatures, generates `build.toml` + `torch_binding.cpp` + `__init__.py`
- Supports Triton kernels: packages as a Python module with pyproject.toml
- Generates ready-to-upload project structure compatible with `kernels upload` CLI
- Added `kernels` and `huggingface-hub` as optional `hf-kernels` dependencies

## v1.1.0 -- 2026-03-12

### Native CUDA C++ Backend

- Added 9 CUDA C++ starter kernels with advanced GPU features:
  - **matmul** -- Tensor core GEMM via `wmma` API, 128x128 tiles, double-buffered shared memory
  - **softmax** -- Warp shuffle reductions, `half2` vectorized loads, grid-stride loop
  - **layernorm** -- Welford's single-pass algorithm, `float4` vectorized loads, warp shuffle stats
  - **rmsnorm** -- Warp shuffle cascade, `rsqrtf` fast inverse sqrt, `half2` vectorization
  - **flash_attention** -- Tiled online softmax, double-buffered shared memory, causal mask support
  - **fused_mlp** -- Fused SwiGLU (gate + up + SiLU + mul), shared memory tiling
  - **cross_entropy** -- Fused online log-sum-exp + NLL in single pass, warp reductions
  - **rotary_embedding** -- `__sincosf` intrinsic, `half2` read-modify-write
  - **reduce** -- Hierarchical warp shuffle + shared memory + grid-level atomic
- Added `kernels/cuda/_compile.py` -- shared compilation utility:
  - Hash-based caching (recompile only when source changes)
  - GPU architecture auto-detection via `torch.cuda.get_device_capability()`
  - Forward declaration extraction for cross-translation-unit linking
  - Thread-safe compilation with file locking
  - Detailed error diagnostics with source line numbers
- Added `--backend triton|cuda` flag to `extract.py`
- Added CUDA C++ optimization playbook to `program.md`
- Added `ninja` as optional dependency for faster compilation

### KernelBench Integration

- Added `kernelbench/bridge.py` -- problem loader supporting 3 sources:
  - HuggingFace datasets (`--source hf`)
  - Local KernelBench repo clone (`--source local`)
  - Individual Python files (`--source file`)
  - Automatic problem analysis (50+ operation patterns)
  - Starter `ModelNew` generation with CUDA/Triton templates
- Added `kernelbench/bench_kb.py` -- 4-stage evaluation pipeline:
  - Stage 1: Correctness (5 random input trials, atol/rtol=1e-2)
  - Stage 2: Stability (NaN/Inf detection)
  - Stage 3: Determinism (3 identical runs)
  - Stage 4: Performance (CUDA event timing, trimmed median)
  - Greppable output with `fast_p` at 7 thresholds
- Added `kernelbench/scorer.py` -- batch evaluation and metrics:
  - `fast_p` metric at thresholds: 1.0x, 1.1x, 1.25x, 1.5x, 2.0x, 3.0x, 5.0x
  - Incremental scoring with JSON persistence
  - Leaderboard-style reports with progress bars
- Added `kernelbench/program_kb.md` -- agent instructions for KernelBench mode:
  - Optimization playbook per difficulty level (L1-L4)
  - CUDA C++ and Triton strategy examples
  - Decision framework and anti-patterns

### Other

- Updated README with KernelBench section, dual-backend docs, and Discord link
- Added `datasets>=2.16.0` as optional `kernelbench` dependency

## v1.0.0 -- Initial Release

- Triton kernel optimization pipeline (profile, extract, bench, orchestrate, verify)
- 9 starter Triton kernels (matmul, softmax, layernorm, rmsnorm, flash_attention, fused_mlp, cross_entropy, rotary_embedding, reduce)
- 5-stage correctness harness + roofline analysis
- Amdahl's law orchestration for multi-kernel optimization
- Self-contained model definitions (GPT-2, LLaMA, BERT)
- TSV logging and experiment visualization
