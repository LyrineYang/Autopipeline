## Summary

-

## Validation

- [ ] `python3 -m unittest discover -s tests`
- [ ] `python3 -m py_compile autopipeline/*.py tree/*.py kernelbench/*.py pipeline/*.py kernels/*.py kernels/cuda/*.py models/*.py scripts/*.py tests/*.py *.py`
- [ ] `python3 -m autopipeline --help`
- [ ] `python3 -m autopipeline doctor --no-gpu`
- [ ] `git diff --check`
- [ ] `python3 -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/autopipeline-wheel`

## Notes

-
