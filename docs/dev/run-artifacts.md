# Run Artifacts

Start with `autopipeline report RUN_DIR` or the run's `view/summary.md`. Open `result.json`
for the product outcome and final verdict. Use the paired verification reports to diagnose
reference or candidate failures. [Result guide](../quickstart.md#read-results)

| Path | Contents |
| --- | --- |
| `result.json` | Run status, selected candidate, hashes, elapsed workflow time, and final verification. An early failure may leave only this file. |
| `input/workload/` and `input/pipeline.json` | Frozen reference workspace and spec. |
| `environment.json` | Python, platform, and package versions. |
| `search_state.json` and `tree_events.jsonl` | Tree state, individual edit and evaluation events, and role usage. |
| `nodes/*/agent/` | Agent requests, responses, timeouts, and errors. |
| `nodes/*/verification/` | Reference and candidate metrics, verifier report, and evaluator logs for each node. |
| `baseline/trial.json` and `trials/*/trial.json` | Trial metadata and a path to the verifier report under `nodes/`. |
| `promoted_pipeline/` | Selected code when promotion succeeds. The baseline can be selected. |
| `promoted_pipeline/.autopipeline/` | Profiler trace and operator statistics. |
| `final_verification/` | Final reference/candidate comparison and verdict. |
| `view/` | Regenerable dashboard, Markdown summary, iteration SVG, and structured views. |

New evaluator subprocesses skip Python bytecode caches. Baseline and trial metadata point to the
original verifier report. Older run directories can be passed directly to `autopipeline report`.

`search_counts` counts distinct non-baseline candidates. A proposal is a node created by
the scheduler. Execution means its executor started, including an execution that failed.
Evaluation means it produced a verdict, including a rejection. A budget skip means the
executor never started for that node. Agent calls are counted separately across configured
roles. An executor can make multiple calls while refining the same branch. The legacy
receipt field `attempts` counts proposals. Reports use node snapshots to read older runs.
An older run without a role ledger can omit its agent call count.

Native evaluator artifacts use `forward_latency_ms` for measured latency and retain
`end_to_end_ms` for older specs. The verifier stores raw values and the pair's ranking
score separately. The report overview reads the final verification pair, the receipt's
`duration_seconds`, and the budget ledger. The iteration SVG under **Branch details** displays
search measurements. To compare close gains, rerun finalists under the same conditions.

For wheel acceptance, run the following outside the source tree with the target Python
environment. It evaluates a real CPU workload, exports and rechecks it, and tests rejection.

```sh
python /path/to/source/scripts/check_installed_workflow.py --work-dir /new/output/directory
```
