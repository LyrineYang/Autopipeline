#!/usr/bin/env python3
"""Compatibility entrypoint; prefer `autopipeline report`."""
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autopipeline.report import build_parser, main

if __name__ == "__main__":
    raise SystemExit(main())
