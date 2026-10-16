#!/usr/bin/env python3
"""Mutation harness: break one specification rule at a time and prove the test suite notices.

Each mutant is a single, exact source substitution that implements a plausible
*wrong* reading of the spec (ties away from zero, closed intervals, last-writer
wins, masking only at later cutoffs, ...).  The suite is run against a copy of
the repository with that one change applied.  A mutant is KILLED when at least
one test fails, SURVIVED otherwise.  Survivors mean the tests are blind to that
rule and are reported honestly, never hidden.

Usage:  python3 tests/mutation/run_mutants.py [--fast] [--only NAME ...]
  --fast  run only the test modules each mutant is expected to trip
          (the default runs the whole suite, which is the real evidence)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# name, file, old, new, rule violated, expected killer modules
MUTANTS = [
    ("ties-away-from-zero", "recon/money.py",
     "if twice > denominator or (twice == denominator and (q & 1)):",
     "if twice > denominator or twice == denominator:",
     "spec 3.4: round ties to even", ["test_money", "test_semantics"]),
    ("float-division", "recon/money.py",
     "    q, r = divmod(numerator, denominator)  # floor division, 0 <= r < den\n    twice = r * 2",
     "    q = int(round(numerator / denominator))\n    return q\n    twice = 0",
     "spec 2: integers are exact above 2**53", ["test_money", "test_semantics"]),
    ("last-arrival-wins", "recon/resolve.py",
     "if version > max_version:",
     "if version >= max_version:",
     "spec 3: same highest version with different payloads must CONFLICT", ["test_normalize_resolve", "test_semantics", "test_public_fixture"]),
    ("conflicts-never-detected", "recon/resolve.py",
     "if len(variants) == 1:",
     "if len(variants) >= 1:",
     "spec 3: block conflicting keys", ["test_normalize_resolve", "test_public_fixture"]),
    ("closed-interval-end", "recon/snapshot.py",
     "hits = [iv for iv in merchants.get(merchant_id, ()) if iv[0] <= effective_at < iv[1]]",
     "hits = [iv for iv in merchants.get(merchant_id, ()) if iv[0] <= effective_at <= iv[1]]",
     "spec 2: merchant validity is half-open [from, to)", ["test_semantics", "test_public_fixture"]),
    ("ambiguity-ignored", "recon/snapshot.py",
     "if len(hits) > 1:",
     "if len(hits) > 2:",
     "spec 3.1: more than one containing interval is AMBIGUOUS_MERCHANT", ["test_semantics", "test_public_fixture"]),
    ("erasure-not-masked", "recon/snapshot.py",
     '"customer_token": None if token in erased else token,',
     '"customer_token": token,',
     "spec 2 erasures: mask erased tokens", ["test_privacy", "test_public_fixture"]),
    ("erasure-respects-cutoff", "recon/snapshot.py",
     'erased = {r[0] for r in c.execute("SELECT customer_token FROM erasures")}',
     'erased = {r[0] for r in c.execute("SELECT customer_token FROM erasures WHERE batch<=?", (cutoff,))}',
     "spec 3: masking uses all committed erasures independent of B", ["test_privacy", "test_public_fixture"]),
    ("quarantine-ignores-cutoff", "recon/snapshot.py",
     '"SELECT row_id, stream FROM quarantine WHERE first_batch<=?"',
     '"SELECT row_id, stream FROM quarantine WHERE first_batch<=? OR 1=1"',
     "spec 3: include unsupported rows with batch <= B only", ["test_public_fixture"]),
    ("inclusive-end-date", "recon/snapshot.py",
     "if status == SELECTED and op == \"POST\" and start <= eff_date < end:",
     "if status == SELECTED and op == \"POST\" and start <= eff_date <= end:",
     "spec 4: end_date is exclusive", ["test_public_fixture", "test_differential"]),
    ("gap-check-disabled", "recon/snapshot.py",
     'missing = store.missing_up_to(query["cutoff_batch"])',
     "missing = []",
     "spec 3: every batch 1..B must be committed or exit 65", ["test_recovery", "test_semantics"]),
    ("v2-amount-not-normalised", "recon/normalize.py",
     'amount = parse_int_text(_req(payload, "amount_minor_text", str))',
     'amount = _req(payload, "amount_minor_text", str)',
     "spec ledger v2: normalise before comparing revisions", ["test_normalize_resolve", "test_public_fixture"]),
    ("before-commit-commits-anyway", "recon/ingest.py",
     "            sys.stderr.flush()\n            os._exit(EXIT_FAILPOINT)\n\n        store.commit()",
     "            sys.stderr.flush()\n            store.commit()\n            os._exit(EXIT_FAILPOINT)\n\n        store.commit()",
     "spec 5: before-commit must leave the previous committed state visible", ["test_recovery"]),
    ("after-stage-publishes-first", "recon/ingest.py",
     '        if failpoint == "after-stage":\n            _log(',
     '        if failpoint == "after-stage":\n            publish(store, batch_no, None)\n            _log(',
     "spec 5: after-stage fires before visible publication", ["test_recovery"]),
    ("timeline-skips-current-batch", "recon/ingest.py",
     "            if as_of < batch_no:\n                continue  # points before this batch are already materialised and unchanged",
     "            if as_of <= batch_no:\n                continue  # points before this batch are already materialised and unchanged",
     "incremental state must materialise the point introduced by this batch", ["test_public_fixture", "test_differential"]),
    ("profile-pii-retained", "recon/normalize.py",
     'return batch, Rec(row_id, stream, sv, KIND_PROFILE, (), None, None, {})',
     'return batch, Rec(row_id, stream, sv, KIND_PROFILE, (), None, None, {"name": payload.get("name"), "email": payload.get("email")})',
     "spec profiles: name/email must not persist in managed storage", ["test_normalize_resolve", "test_privacy"]),
    ("trace-unsorted", "recon/snapshot.py",
     '    trace.sort(key=lambda t: t["entry_id"])',
     '    trace.sort(key=lambda t: t["entry_id"], reverse=True)',
     "spec 4: trace sorted by entry_id ascending", ["test_public_fixture", "test_semantics"]),
    ("fx-rounding-after-sum", "recon/snapshot.py",
     "        usd = to_usd_micro(amount, rate[0], rate[1])\n        g = groups[(eff_date, merchant_id, region, tier)]\n        g[0] += usd",
     "        usd = to_usd_micro(amount, rate[0], rate[1])\n        g = groups[(eff_date, merchant_id, region, tier)]\n        g[0] += amount * rate[0] // rate[1]",
     "spec 3.4: round each entry, never the aggregate", ["test_semantics", "test_public_fixture"]),
]


def apply(tree: str, path: str, old: str, new: str) -> None:
    p = os.path.join(tree, path)
    with open(p, encoding="utf-8") as f:
        src = f.read()
    n = src.count(old)
    if n != 1:
        raise SystemExit(f"mutant anchor must occur exactly once in {path} (found {n}): {old[:60]!r}")
    with open(p, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new))


def run_suite(tree: str, modules: list[str] | None) -> tuple[bool, str]:
    """Run the suite (or the named modules) against ``tree``; return (killed, first failing test)."""
    patterns = [f"{m}.py" for m in modules] if modules else ["test_*.py"]
    for pat in patterns:
        cmd = [sys.executable, "-I", "-m", "unittest", "discover", "-q", "-s", "tests", "-p", pat]
        r = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
        out = r.stderr + r.stdout
        if "_FailedTest" in out or "Ran 0 tests" in out:
            raise SystemExit(f"harness error: tests did not load for pattern {pat}:\n{out[-800:]}")
        if r.returncode != 0:
            m = re.search(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
            return True, m.group(1) if m else "?"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    results = []
    base = tempfile.mkdtemp(prefix="recon-mutants-")
    try:
        for name, path, old, new, rule, killers in MUTANTS:
            if args.only and name not in args.only:
                continue
            tree = os.path.join(base, name)
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "ai_logs", "bench_out", "*.sqlite3*"))
            apply(tree, path, old, new)
            t0 = time.monotonic()
            killed, first = run_suite(tree, killers if args.fast else None)
            dt = time.monotonic() - t0
            results.append((name, rule, killed, first, dt))
            status = "KILLED  " if killed else "SURVIVED"
            print(f"{status} {name:32s} {dt:5.1f}s  {rule}  {('by ' + first) if first else ''}", flush=True)
            shutil.rmtree(tree, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    killed = sum(1 for r in results if r[2])
    print(f"\n{killed}/{len(results)} mutants killed")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
