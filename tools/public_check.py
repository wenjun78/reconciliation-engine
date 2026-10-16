#!/usr/bin/env python3
"""Ingest the public fixture (optionally in a shuffled order) and compare every
public query against expected/.  Exit 1 on any mismatch.

Usage: tools/public_check.py [--order reverse|shuffle:SEED] [--engine incremental|full] [--keep DIR]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")
RUN = os.path.join(ROOT, "run")


def diff(a, b, path="$"):
    out = []
    if type(a) != type(b):
        return [f"{path}: type {type(a).__name__} != {type(b).__name__}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing in actual")
            elif k not in b:
                out.append(f"{path}.{k}: unexpected in actual")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", default="sorted")
    ap.add_argument("--engine", default="incremental")
    ap.add_argument("--keep", default=None)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(FIX, "data", "batch_*.jsonl")))
    if args.order == "reverse":
        files.reverse()
    elif args.order.startswith("shuffle:"):
        random.Random(int(args.order.split(":")[1])).shuffle(files)

    tmp = args.keep or tempfile.mkdtemp(prefix="recon-public-")
    state = os.path.join(tmp, "state")
    outdir = os.path.join(tmp, "out")
    os.makedirs(outdir, exist_ok=True)
    for f in files:
        r = subprocess.run([RUN, "ingest", "--input", f, "--state", state], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ingest {os.path.basename(f)} failed rc={r.returncode}\n{r.stderr}")
            return 1
    bad = 0
    for q in sorted(glob.glob(os.path.join(FIX, "queries", "*.json"))):
        name = os.path.basename(q)
        out = os.path.join(outdir, name)
        r = subprocess.run([RUN, "snapshot", "--query", q, "--state", state, "--out", out, "--engine", args.engine], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"snapshot {name} failed rc={r.returncode}\n{r.stderr}")
            bad += 1
            continue
        actual = json.load(open(out))
        expected = json.load(open(os.path.join(FIX, "expected", name)))
        d = diff(actual, expected)
        if d:
            bad += 1
            print(f"MISMATCH {name}:")
            for line in d[:25]:
                print("   ", line)
        else:
            print(f"ok {name}: {len(actual['settlements'])} groups, {len(actual['trace'])} trace, {len(actual['exceptions'])} exceptions")
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PUBLIC QUERIES MATCH" if bad == 0 else f"{bad} query file(s) differ")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
