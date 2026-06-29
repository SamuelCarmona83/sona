#!/usr/bin/env python3
"""CLI wrapper for library deduplication."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))

from dedupe_library import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())