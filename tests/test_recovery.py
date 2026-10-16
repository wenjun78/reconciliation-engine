"""Durability: failpoints, hard kills, replay-exactly-once, gap rejection, output preservation.

These tests run the real ``./run`` adapter in subprocesses because the
failpoints terminate the process with os._exit.
"""

import os
import random
import signal
import subprocess
import time
import unittest

from helpers import RUN, EngineCase, fx, ledger, merchant, requires_posix, write_batch

DIM = [merchant("M1"), fx("USD", "2026-01-01", 1, 1)]


@requires_posix
class Failpoints(EngineCase):
    def _committed(self):
        s = self.db()
        try:
            return s.committed_batches(), [r[0] for r in s.conn.execute("SELECT batch FROM stage_batches")]
        finally:
            s.close()

    def test_after_stage_is_durable_but_invisible(self):
        self.assertEqual(self.cli_ingest(1, DIM + [ledger("A", 1, 1)]).returncode, 0)
        rows2 = [ledger("A", 2, 2)]
        r = self.cli_ingest(2, rows2, failpoint="after-stage")
        self.assertEqual(r.returncode, 86, r.stderr)
        self.assertIn("after-stage", r.stderr)
        self.assertEqual(self._committed(), ([1], [2]))          # staged, not committed
        self.assertEqual(self.cli_snapshot(2)[0].returncode, 65)  # not visible
        r1, data, _ = self.cli_snapshot(1)
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(data["trace"][0]["usd_micro"], 1)        # previous state intact
        # replay without failpoint commits exactly once, resuming from staging
        r = self.cli_ingest(2, rows2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("resuming at publish", r.stderr)
        self.assertEqual(self._committed(), ([1, 2], []))
        self.assertEqual(self.cli_snapshot(2)[1]["trace"][0]["usd_micro"], 2)

    def test_before_commit_leaves_previous_state_visible(self):
        self.assertEqual(self.cli_ingest(1, DIM + [ledger("A", 1, 1)]).returncode, 0)
        rows2 = [ledger("A", 2, 2)]
        r = self.cli_ingest(2, rows2, failpoint="before-commit")
        self.assertEqual(r.returncode, 86, r.stderr)
        self.assertIn("before-commit", r.stderr)
        self.assertEqual(self._committed(), ([1], [2]))
        self.assertEqual(self.cli_snapshot(2)[0].returncode, 65)
        self.assertEqual(self.cli_snapshot(1)[1]["trace"][0]["usd_micro"], 1)
        s = self.db()
        try:  # no partial publication leaked
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM ledger_rev WHERE batch=2").fetchone()[0], 0)
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM ledger_state WHERE as_of=2").fetchone()[0], 0)
        finally:
            s.close()
        r = self.cli_ingest(2, rows2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("resuming at publish", r.stderr)
        self.assertEqual(self._committed(), ([1, 2], []))
        self.assertEqual(self.cli_snapshot(2)[1]["trace"][0]["usd_micro"], 2)

    def test_failpoint_on_seen_batch_does_not_fire(self):
        self.assertEqual(self.cli_ingest(1, DIM + [ledger("A", 1, 1)]).returncode, 0)
        for fp in ("after-stage", "before-commit"):
            r = self.cli_ingest(1, DIM + [ledger("A", 1, 1)], failpoint=fp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("already committed", r.stderr)
        self.assertEqual(self._committed(), ([1], []))

    def test_failpoint_fires_on_every_unseen_attempt(self):
        self.assertEqual(self.cli_ingest(1, DIM).returncode, 0)
        rows2 = [ledger("A", 1, 1)]
        for _ in range(3):
            self.assertEqual(self.cli_ingest(2, rows2, failpoint="after-stage").returncode, 86)
            self.assertEqual(self.cli_ingest(2, rows2, failpoint="before-commit").returncode, 86)
        self.assertEqual(self._committed(), ([1], [2]))
        self.assertEqual(self.cli_ingest(2, rows2).returncode, 0)
        self.assertEqual(self._committed(), ([1, 2], []))

    def test_exact_retry_is_idempotent_and_logged(self):
        rows = DIM + [ledger("A", 1, 1)]
        self.assertEqual(self.cli_ingest(1, rows).returncode, 0)
        r = self.cli_ingest(1, rows)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no-op", r.stderr)
        s = self.db()
        try:
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM ledger_rev").fetchone()[0], 1)
            outcomes = [r[0] for r in s.conn.execute("SELECT outcome FROM txn_log WHERE batch=1 ORDER BY id")]
            self.assertEqual(outcomes, ["committed", "begin", "committed", "already-committed"])
        finally:
            s.close()

    def test_gap_rejection_preserves_existing_output_byte_for_byte(self):
        self.assertEqual(self.cli_ingest(1, DIM + [ledger("A", 1, 1)]).returncode, 0)
        r, _, out = self.cli_snapshot(1, qid="keep")
        self.assertEqual(r.returncode, 0)
        with open(out, "rb") as f:
            before = f.read()
        mtime = os.stat(out).st_mtime_ns
        r2 = self.run_cli("snapshot", "--query", self._q(5, "keep"), "--state", self.state, "--out", out)
        self.assertEqual(r2.returncode, 65)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(out).st_mtime_ns, mtime)
        self.assertEqual([n for n in os.listdir(self.outdir) if n.startswith(".recon-")], [])  # no temp litter

    def test_snapshot_without_state_or_cutoff_beyond_committed(self):
        r = self.run_cli("snapshot", "--query", self._q(1, "none"), "--state", os.path.join(self.tmp, "nope"), "--out", os.path.join(self.outdir, "x.json"))
        self.assertEqual(r.returncode, 65)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope")))  # snapshot never creates state
        self.assertEqual(self.cli_ingest(1, DIM).returncode, 0)
        self.assertEqual(self.cli_snapshot(2)[0].returncode, 65)

    def _q(self, cutoff, qid):
        from helpers import query, write_query

        return write_query(self.inbox, query(qid, cutoff), f"{qid}-{cutoff}.json")


@requires_posix
class HardKills(EngineCase):
    """SIGKILL the ingest process at random points; state must stay consistent and replay must commit once."""

    def _big_batch(self, n, batch):
        rows = DIM if batch == 1 else []
        rows = rows + [ledger(f"E{i}", batch, i, at="2026-01-01T00:00:00Z") for i in range(n)]
        return write_batch(self.inbox, batch, rows)

    def test_random_sigkill_then_replay(self):
        self._big_batch(1, 1)
        self.assertEqual(subprocess.run([RUN, "ingest", "--input", os.path.join(self.inbox, "batch_01.jsonl"), "--state", self.state], capture_output=True).returncode, 0)
        path = self._big_batch(30000, 2)
        rnd = random.Random(99)
        killed = 0
        for attempt in range(6):
            p = subprocess.Popen([RUN, "ingest", "--input", path, "--state", self.state], stderr=subprocess.DEVNULL)
            time.sleep(rnd.uniform(0.05, 0.9))
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
                p.wait()
                killed += 1
            s = self.db()
            try:
                self.assertEqual(s.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                committed = s.committed_batches()
                n_rev = s.conn.execute("SELECT COUNT(*) FROM ledger_rev WHERE batch=2").fetchone()[0]
                n_state = s.conn.execute("SELECT COUNT(*) FROM ledger_state WHERE as_of=2").fetchone()[0]
            finally:
                s.close()
            if 2 in committed:
                self.assertEqual((n_rev, n_state), (30000, 30000))
                break
            self.assertEqual((n_rev, n_state), (0, 0), "partial publication leaked after kill")
        # final replay must leave exactly one committed copy
        r = subprocess.run([RUN, "ingest", "--input", path, "--state", self.state], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.db()
        try:
            self.assertEqual(s.committed_batches(), [1, 2])
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM ledger_rev WHERE batch=2").fetchone()[0], 30000)
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM stage_rows").fetchone()[0], 0)
        finally:
            s.close()
        r, data, _ = self.cli_snapshot(2)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(data["trace"]), 30000)  # E0 v2 supersedes E0 v1 from batch 1
        self.assertGreaterEqual(killed, 1, "the kill loop never killed anything; machine too fast for this test")


if __name__ == "__main__":
    unittest.main()


@requires_posix
class ReaderIsolation(EngineCase):
    """A snapshot whose read transaction began before an erasure commit does not see it; the next one does."""

    def test_concurrent_commit_is_invisible_to_a_running_snapshot(self):
        import json
        import os
        import subprocess

        from helpers import erasure, query, write_query

        self.assertEqual(self.cli_ingest(1, DIM + [ledger("A", 1, 1, token="C1")]).returncode, 0)
        q = write_query(self.inbox, query("iso", 1))
        out_early = os.path.join(self.outdir, "early.json")
        env = dict(os.environ, RECON_SNAPSHOT_HOLD_SECONDS="2.5")
        p = subprocess.Popen([RUN, "snapshot", "--query", q, "--state", self.state, "--out", out_early], env=env, stderr=subprocess.PIPE, text=True)
        time.sleep(0.8)  # the reader has begun its transaction
        r = self.cli_ingest(2, [erasure("C1")])
        self.assertEqual(r.returncode, 0, r.stderr)  # writer is not blocked by the reader (WAL)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        with open(out_early) as f:
            early = json.load(f)
        self.assertEqual(early["trace"][0]["customer_token"], "C1")  # committed state as of read start
        r2, late, _ = self.cli_snapshot(1, qid="late")
        self.assertEqual(r2.returncode, 0)
        self.assertIsNone(late["trace"][0]["customer_token"])        # read began after the commit
