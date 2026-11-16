## Summary

-

## Validation

Select applicable checks and explain omissions. For documentation-only changes,
review links and usage and run `git diff --check`.

- [ ] `python3 -m unittest discover -s tests`
- [ ] `python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py`
- [ ] `python3 -m autopipeline --help`
- [ ] `python3 -m autopipeline doctor --no-gpu`
- [ ] `git diff --check`
- [ ] `python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel`

## User Impact and Compatibility

<!-- Apply docs/dev/product-engineering-standard.md. State the user scenario and
any change to public commands, configuration, artifacts, or extension contracts.
Include migration guidance when needed; write "No interface change" otherwise. -->

-

## Next Validation or Resource Need

<!-- If a material question remains, explain the next test, why it matters,
the minimum resource needed, and the fallback. Write "None" when not applicable. -->

-
