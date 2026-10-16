"""Retention discipline: names/emails never land in managed storage; erasure masks across cutoffs and replay."""

import os
import unittest

from helpers import EngineCase, erasure, fx, ledger, merchant, profile, requires_posix, trace_map

NAME = "SYNTHETIC_PERSON_ZEBRA_7"
EMAIL = "zebra7@example.invalid"
DIM = [merchant("M1"), fx("USD", "2026-01-01", 1, 1)]


def _state_bytes(state_dir: str) -> bytes:
    blob = b""
    for root, _d, files in os.walk(state_dir):
        for fn in files:
            with open(os.path.join(root, fn), "rb") as f:
                blob += f.read()
    return blob


class Privacy(EngineCase):
    def test_profile_pii_is_never_stored(self):
        self.ingest(1, DIM + [profile("C1", NAME, EMAIL), ledger("A", 1, 1, token="C1")])
        blob = _state_bytes(self.state)
        self.assertNotIn(NAME.encode(), blob)
        self.assertNotIn(EMAIL.encode(), blob)
        self.assertIn(b"C1", blob)  # opaque token tombstone/lineage is allowed
        r = self.both(1)
        self.assertEqual(r["trace"][0]["customer_token"], "C1")

    def test_erasure_masks_every_cutoff_including_earlier_ones(self):
        self.ingest(1, DIM + [profile("C1", NAME, EMAIL), ledger("A", 1, 5, token="C1"), ledger("B", 1, 6, token="C2")])
        self.assertEqual(self.both(1)["trace"][0]["customer_token"], "C1")
        self.ingest(2, [erasure("C1")])
        for cutoff in (1, 2):
            tm = trace_map(self.both(cutoff))
            self.assertIsNone(tm["A"]["customer_token"])
            self.assertEqual(tm["B"]["customer_token"], "C2")
            self.assertEqual(tm["A"]["usd_micro"], 5)  # amounts unchanged by erasure

    def test_erasure_survives_old_data_replay_and_reordered_delivery(self):
        self.ingest(1, DIM)
        self.ingest(3, [erasure("C1")])
        # older batch arrives after the erasure: token must still be masked
        self.ingest(2, [profile("C1", NAME, EMAIL), ledger("A", 1, 5, token="C1")])
        self.assertIsNone(trace_map(self.both(2))["A"]["customer_token"])
        self.assertIsNone(trace_map(self.both(3))["A"]["customer_token"])
        # profile re-sent after erasure (batch 11 pattern): nothing resurrects
        self.ingest(4, [profile("C1", "REPLAY_NAME", "replay@example.invalid")])
        blob = _state_bytes(self.state)
        for s in (NAME, EMAIL, "REPLAY_NAME", "replay@example.invalid"):
            self.assertNotIn(s.encode(), blob)
        self.assertIsNone(trace_map(self.both(4))["A"]["customer_token"])
        # exact retry of the pre-erasure batch is a no-op
        self.ingest(2, [profile("C1", NAME, EMAIL), ledger("A", 1, 5, token="C1")])
        self.assertIsNone(trace_map(self.both(4))["A"]["customer_token"])

    def test_erasure_of_unknown_token_is_harmless(self):
        self.ingest(1, DIM + [erasure("ghost"), ledger("A", 1, 1, token="C1")])
        self.assertEqual(self.both(1)["trace"][0]["customer_token"], "C1")

    @requires_posix
    def test_stderr_never_carries_tokens_or_names(self):
        r = self.cli_ingest(1, DIM + [profile("C1", NAME, EMAIL), ledger("A", 1, 1, token="SECRET_TOKEN_X")])
        self.assertEqual(r.returncode, 0)
        for s in (NAME, EMAIL, "SECRET_TOKEN_X"):
            self.assertNotIn(s, r.stderr)
            self.assertNotIn(s, r.stdout)
        r2, _, _ = self.cli_snapshot(1)
        self.assertNotIn("SECRET_TOKEN_X", r2.stderr)

    @requires_posix
    def test_check_command_reports_zero_email_like_strings(self):
        import json

        self.ingest(1, DIM + [profile("C1", NAME, EMAIL), ledger("A", 1, 1, token="C1")])
        r = self.run_cli("check", "--state", self.state, "--deep")
        self.assertEqual(r.returncode, 0, r.stdout)
        rep = json.loads(r.stdout)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["pii_scan"]["email_like_strings"], 0)
        self.assertEqual(rep["differential"]["cutoffs_differing"], [])

    @requires_posix
    def test_explain_masks_erased_token(self):
        import json

        self.ingest(1, DIM + [ledger("A", 1, 1, token="C1")])
        self.ingest(2, [erasure("C1")])
        r = self.run_cli("explain", "--state", self.state, "--entry", "A", "--cutoff", "1")
        self.assertEqual(r.returncode, 0)
        doc = json.loads(r.stdout)
        self.assertIsNone(doc["resolution"]["customer_token"])
        self.assertIsNone(doc["revisions"][0]["customer_token"])
        self.assertNotIn('"C1"', r.stdout)


if __name__ == "__main__":
    unittest.main()
