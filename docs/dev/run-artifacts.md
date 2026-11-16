# Run Artifacts

Start with `autopipeline report RUN_DIR` or read `view/summary.md` in the run directory.
The [result guide](../quickstart.md#read-results) explains the verdicts. The paths below
are relative to one run directory.

| Path | Contents |
| --- | --- |
| `result.json` | Run status, selected candidate, file hashes, and the final verification verdict. Early failures may only leave this file. |
| `input/` and `environment.json` | Frozen workload and spec, runtime, and dependency versions. |
| `search_state.json` and `tree_events.jsonl` | Search rounds, node results, and budget usage. |
| `nodes/*/agent/` | Agent requests, responses, timeouts, and errors. |
| `nodes/*/verification/` | Reference and candidate measurements, evaluator output, and errors. |
| `promoted_pipeline/` | Selected code. It can contain the original implementation. |
| `promoted_pipeline/.autopipeline/` | Profiler trace and operator statistics. |
| `final_verification/` | Independent final measurement and verification report. |
| `view/` | Browser dashboard and text summary. |

The report reads the product verdict from `result.json`. Older tree runs can still display
search history when they lack a product receipt. A `running` receipt describes the last saved
status. Check the execution process separately if it may have stopped.

Use `autopipeline pipeline run --help-all` to see advanced integration options.

For installed-workflow acceptance, run the following with the target Python environment from a
directory outside the source tree.

```sh
python /path/to/source/scripts/check_installed_workflow.py --work-dir /new/output/directory
```

The check exercises a real CPU workload, reports, export, fresh verification, rejection of an
incorrect candidate, and protection against overwriting existing results. It records the command
logs and `acceptance.json` without calling an agent service.
