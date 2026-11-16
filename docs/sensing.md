# Sensing

Sensing is the current diagnostic runtime.

It is deliberately small. It does not try to think for the agent.

## What It Does

For each planner, executor, or reflector step, sensing:

1. writes a small passive snapshot
2. accepts probe requests
3. executes those requests safely
4. records results and recent history
5. refreshes verifier evidence after probe results arrive

The intended split is:

- agent decides what evidence it needs
- sensing runs the request and records the result
- app-server dynamic tools or the legacy command bridge serve the requests while the agent step is running

## How To Think About Probes

A probe is a diagnostic request authored by the planner or executor.

Examples:

- read a file
- inspect the current directory
- grep for a pattern
- request stable theoretical-limit inputs
- run a compile smoke check
- run `ncu` or `nsys`
- run a tiny Python experiment

This means active sensing is not just “read a log”.  
It can also mean “run a small diagnostic experiment and read the result”.

## Current Probe Categories

The current system already supports five useful probe views:

- hardware profile
- software audit
- runtime breakdown
- hardware utilization
- active experiment

There is also one important bridge probe:

- theoretical-limit inputs

This one is intentionally split from the calculation itself:

- sensing provides stable inputs
- the agent writes the actual calculation script with the built-in `python` probe

In code, these are implemented by:

- built-in file, command, Python, and theoretical-limit probes in [autopipeline/search/sensing.py](../autopipeline/search/sensing.py)
- task-specific AutoKernel probes in [kernel_tools/probes.py](../kernel_tools/probes.py)
- pipeline metric probes in [autopipeline/pipeline/task.py](../autopipeline/pipeline/task.py)

Probe execution is budget-aware. `BudgetLedger` can reject new probes after
`--max-probe-calls` is exhausted, and sensing records those rejections as
diagnostic results.

## Practical Model

In plain language, the runtime works like this:

- the agent starts its current step
- it reads the current sensing manifest
- if evidence is missing, it asks for a probe
- sensing runs the probe
- the result is written back during the same command step
- the manifest is refreshed with recent probe results and verifier evidence
- the agent can continue using that result immediately

That is the current active-sensing loop.

Mechanically, [autopipeline/search/command.py](../autopipeline/search/command.py)
does the legacy file serving loop. It initializes empty probe request/result
files, launches the role command, notices when `TREE_PROBE_REQUESTS_PATH`
changes, calls `SensingRuntime.serve_pending(...)`, and finally calls
`finalize(...)` once the subprocess exits.

For Codex app-server sessions, [autopipeline/search/codex_dynamic_tools.py](../autopipeline/search/codex_dynamic_tools.py)
registers `run_sensing_probe` as a dynamic tool. It calls the same
`SensingRuntime` probe handlers and records results into the same manifest and
history files. Dynamic tools are an additional transport, not a replacement for
the microbench / probe design.

For theoretical ceiling or roofline-style reasoning, the intended loop is:

1. request `theoretical_limit_inputs`
2. read the stable hardware / workload / observed facts
3. write a `python` probe that computes the bound or headroom
4. use the result in planning or execution

## Why It Is Structured This Way

This design keeps three things separate:

- the agent decides what to inspect
- the framework owns safety, isolation, and recording
- the task provides domain-specific probe implementations when needed

This keeps the sensing layer thin and leaves room for stronger agents later.

## Current Boundary

Today sensing is not:

- a general event bus
- a complete infra observability platform
- a full interactive shell

It is a small, usable diagnostic runtime. That is intentional.
