"""The published fixture must match byte-for-value in every ingestion order and on both engines."""

import glob
import json
import os
import random
import unittest

from helpers import FIXTURES, EngineCase, quiet_ingest as run_ingest, quiet_snapshot as run_snapshot

DATA = sorted(glob.glob(os.path.join(FIXTURES, "data", "batch_*.jsonl")))
QUERIES = sorted(glob.glob(os.path.join(FIXTURES, "queries", "*.json")))


class PublicFixture(EngineCase):
    def _ingest_all(self, order):
        for f in order:
            self.assertEqual(run_ingest(f, self.state, None), 0, f)

    def _check_all(self, engine):
        for q in QUERIES:
            name = os.path.basename(q)
            out = os.path.join(self.outdir, name)
            self.assertEqual(run_snapshot(q, self.state, out, engine), 0, name)
            with open(out) as f, open(os.path.join(FIXTURES, "expected", name)) as g:
                self.assertEqual(json.load(f), json.load(g), f"{name} ({engine})")

    def test_sorted_order_both_engines(self):
        self._ingest_all(DATA)
        self._check_all("incremental")
        self._check_all("full")

    def test_reverse_order(self):
        self._ingest_all(list(reversed(DATA)))
        self._check_all("incremental")

    def test_shuffled_orders(self):
        for seed in (1, 7, 42):
            with self.subTest(seed=seed):
                self.tearDown()
                self.setUp()
                order = list(DATA)
                random.Random(seed).shuffle(order)
                self._ingest_all(order)
                self._check_all("incremental")

    def test_every_batch_retried_twice_is_harmless(self):
        for f in DATA:
            self.assertEqual(run_ingest(f, self.state, None), 0)
            self.assertEqual(run_ingest(f, self.state, None), 0)
        self._check_all("incremental")

    def test_structure_checker_accepts_output(self):
        import subprocess
        import sys

        self._ingest_all(DATA)
        q = os.path.join(FIXTURES, "queries", "q07.json")
        out = os.path.join(self.outdir, "q07.json")
        self.assertEqual(run_snapshot(q, self.state, out), 0)
        r = subprocess.run([sys.executable, "-I", os.path.join(FIXTURES, "tools", "check_output.py"), out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_snapshot_at_low_cutoff_only_differs_by_erasure(self):
        """q02 before batch 10 differs from the published answer only in masking (WORKED_EXAMPLES note)."""
        for f in DATA[:2]:
            self.assertEqual(run_ingest(f, self.state, None), 0)
        q = os.path.join(FIXTURES, "queries", "q02.json")
        out = os.path.join(self.outdir, "q02.json")
        self.assertEqual(run_snapshot(q, self.state, out), 0)
        with open(out) as f, open(os.path.join(FIXTURES, "expected", "q02.json")) as g:
            got, want = json.load(f), json.load(g)
        self.assertEqual(got["settlements"], want["settlements"])
        self.assertEqual(got["exceptions"], want["exceptions"])
        unmasked = {t["entry_id"]: t["customer_token"] for t in got["trace"]}
        for t in want["trace"]:
            if t["customer_token"] is None:
                self.assertIn(unmasked[t["entry_id"]], ("C0", "C1"))
            else:
                self.assertEqual(unmasked[t["entry_id"]], t["customer_token"])
        # once the erasure batch is in, everything matches even at cutoff 2
        for f in DATA[2:10]:
            self.assertEqual(run_ingest(f, self.state, None), 0)
        self.assertEqual(run_snapshot(q, self.state, out), 0)
        with open(out) as f:
            self.assertEqual(json.load(f), want)


if __name__ == "__main__":
    unittest.main()
