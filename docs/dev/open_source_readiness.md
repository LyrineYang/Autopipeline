# Release Checks

The installable product is AutoPipeline 1.4.0a6. The wheel contains the native
`autopipeline pipeline` and `autopipeline report` workflow. Compatibility commands such as
`tree run`, `profile`, `extract`, and KernelBench use a source checkout.

## Validate a Build

1. Check the version, changelog, and upstream license.
2. Run the unit and CPU integration tests. Exercise correct and incorrect candidates, the fixed
   reference, reports, and portable rechecking.
3. Build a wheel and install it in a fresh environment outside the repository. Run the installed
   checks, including the documented workflow.
4. Review the GitHub Actions results for the exact revision. Archive the source snapshot, wheel
   checksum, commit SHA, and validation outputs when distributing a build.

```sh
python -m pip install -e '.[cpu,models]'
python -m unittest discover -s tests
python -m pip wheel . --no-deps -w dist
```

The CI workflow runs Python unit tests, wheel smoke checks, and CPU model integration.
It also installs the wheel outside the checkout and runs the acceptance entrypoint.
[CI workflow](../../.github/workflows/ci.yml) · [Installed acceptance](../../scripts/check_installed_workflow.py)

## Supported Evidence

CPU installation and real model evaluation have been checked using the residual MLP, Attention,
and a locally initialized Transformers checkpoint. A real-agent CPU pilot exercised edits and
verification. The RTX 4060 still needs a CUDA preflight and an equal-budget evaluation of
repeatable search gains. [Handoff](handoff.md)

The wheel uses the `autopipeline`, `tree`, and `pipeline` top-level package names. A clean
virtual environment prevents collisions with similarly named packages. Revisit the imports and
compatibility strategy before a public package release. [Product engineering standard](product-engineering-standard.md)
