#!/usr/bin/env python3
"""Fail the build (or a run) if the interpreter is not the pinned runtime."""
import sqlite3
import sys

want = (3, 12)
if sys.version_info[:2] != want and "--any-312-plus" not in sys.argv:
    sys.exit(f"expected CPython {want[0]}.{want[1]}, got {sys.version.split()[0]}")
if tuple(map(int, sqlite3.sqlite_version.split("."))) < (3, 35):
    sys.exit(f"sqlite {sqlite3.sqlite_version} too old (need >= 3.35 for UPSERT/partial-index/WITHOUT ROWID support used here)")
print(f"python {sys.version.split()[0]}  sqlite {sqlite3.sqlite_version}")
