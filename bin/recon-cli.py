#!/usr/bin/env python3
"""Launcher used by ./run; makes the package importable without PYTHONPATH."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from recon.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
