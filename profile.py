#!/usr/bin/env python3
"""Compatibility CLI without shadowing Python's profiler for cProfile/torch."""
from __future__ import annotations

if __name__ == "__main__":
    from model_profile import main
    raise SystemExit(main())
else:
    import importlib.util
    import sysconfig
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location("_autopipeline_stdlib_profile", Path(sysconfig.get_path("stdlib")) / "profile.py")
    _stdlib_profile = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_stdlib_profile)
    # cProfile imports _Utils as well as run/runctx; expose the full stdlib API.
    for _name in dir(_stdlib_profile):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_stdlib_profile, _name)

    def __getattr__(name):
        # Historical programmatic model-profiler access stays lazy.
        import model_profile
        return getattr(model_profile, name)
