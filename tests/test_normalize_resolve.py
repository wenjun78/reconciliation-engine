import unittest

from helpers import ROOT  # noqa: F401

from recon import normalize as nz
from recon.resolve import CONFLICT, SELECTED, resolve_at, timeline


def env(stream, sv, payload, row_id="r1", batch=1, **extra):
    d = {"batch": batch, "row_id": row_id, "stream": stream, "schema_version": sv, "payload": payload}
    d.update(extra)
    return d


LEDGER = {"entry_id": "E", "version": 2, "op": "POST", "merchant_id": "M", "customer_token": "C",
          "effective_at": "2026-01-01T00:00:00Z", "currency": "USD", "amount_minor": 99}


class Normalisation(unittest.TestCase):
    def test_v1_and_v2_equal_amounts_share_fingerprint(self):
        _, a = nz.normalise(env("ledger", 1, LEDGER))
        v2 = dict(LEDGER)
        del v2["amount_minor"]
        v2["amount_minor_text"] = "0099"
        _, b = nz.normalise(env("ledger", 2, v2, row_id="r2"))
        self.assertEqual(a.fp, b.fp)
        self.assertEqual(a.fields["amount_minor"], 99)
        self.assertEqual(b.fields["amount_minor"], 99)

    def test_v2_amount_parsed_exactly(self):
        v2 = dict(LEDGER)
        del v2["amount_minor"]
        v2["amount_minor_text"] = "9007199254740993"
        _, b = nz.normalise(env("ledger", 2, v2))
        self.assertEqual(b.fields["amount_minor"], 9007199254740993)

    def test_different_amount_differs(self):
        _, a = nz.normalise(env("ledger", 1, LEDGER))
        _, b = nz.normalise(env("ledger", 1, dict(LEDGER, amount_minor=100)))
        self.assertNotEqual(a.fp, b.fp)

    def test_every_declared_field_is_business_relevant(self):
        _, base = nz.normalise(env("ledger", 1, LEDGER))
        for field, other in (("op", "VOID"), ("merchant_id", "M2"), ("customer_token", "C2"),
                             ("effective_at", "2026-01-02T00:00:00Z"), ("currency", "EUR")):
            _, r = nz.normalise(env("ledger", 1, dict(LEDGER, **{field: other})))
            self.assertNotEqual(base.fp, r.fp, field)

    def test_extra_fields_ignored_for_equality(self):
        _, a = nz.normalise(env("ledger", 1, LEDGER))
        _, b = nz.normalise(env("ledger", 1, dict(LEDGER, note="x"), row_id="zz", trace_id="abc"))
        self.assertEqual(a.fp, b.fp)

    def test_unknown_schema_or_stream_is_quarantined(self):
        for stream, sv in (("ledger", 99), ("ledger", "1"), ("merchants", 2), ("fx", 0), ("future_stream", 1), ("ledger", None)):
            _, r = nz.normalise(env(stream, sv, {"anything": 1}, row_id="q"))
            self.assertEqual(r.kind, nz.KIND_QUARANTINE, (stream, sv))
            self.assertEqual(r.key, ("q",))
            self.assertEqual(r.stream, stream)

    def test_profiles_drop_name_and_email(self):
        _, r = nz.normalise(env("profiles", 1, {"customer_token": "C", "name": "Secret Name", "email": "s@example.invalid"}))
        self.assertEqual(r.kind, nz.KIND_PROFILE)
        self.assertEqual(r.fields, {})
        self.assertNotIn("Secret", nz.rec_to_json(r))
        self.assertNotIn("@", nz.rec_to_json(r))

    def test_merchant_and_fx_keys(self):
        _, m = nz.normalise(env("merchants", 1, {"merchant_id": "M", "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
                                                  "version": 1, "region": "EU", "tier": "standard"}))
        self.assertEqual(m.key, ("M", "2026-01-01T00:00:00Z"))
        _, f = nz.normalise(env("fx", 1, {"currency": "EUR", "date": "2026-01-01", "version": 1, "usd_micro_num": 3, "usd_micro_den": 2}))
        self.assertEqual(f.key, ("EUR", "2026-01-01"))
        self.assertEqual(f.fields, {"num": 3, "den": 2})

    def test_stage_roundtrip_preserves_big_int(self):
        _, a = nz.normalise(env("ledger", 1, dict(LEDGER, amount_minor=10**30)))
        b = nz.rec_from_json(nz.rec_to_json(a))
        self.assertEqual(b, a)
        self.assertIsInstance(b.fields["amount_minor"], int)

    def test_bad_envelope_is_rejected(self):
        with self.assertRaises(nz.InputError):
            nz.normalise(env("ledger", 1, LEDGER, batch=0))
        with self.assertRaises(nz.InputError):
            nz.normalise({"batch": 1, "stream": "ledger", "schema_version": 1, "payload": LEDGER})


class Resolution(unittest.TestCase):
    """Worked example 3 and neighbours, on the pure timeline function."""

    def test_highest_version_wins_not_last_arrival(self):
        revs = [(5, 1, "a", "v1"), (2, 2, "b", "v2")]
        self.assertEqual(resolve_at(revs, 5), (SELECTED, 2, "v2"))

    def test_conflict_then_resolution_then_duplicate(self):
        revs = [(1, 1, "a", "v1"), (2, 2, "b", "x"), (2, 2, "c", "y")]
        self.assertEqual(resolve_at(revs, 2)[0], CONFLICT)
        self.assertEqual(resolve_at(revs, 2)[1], 2)
        revs.append((3, 1, "a", "v1"))  # seeing v1 again does not restore
        self.assertEqual(resolve_at(revs, 3)[0], CONFLICT)
        revs.append((4, 3, "d", "v3"))  # unambiguous higher version resolves
        self.assertEqual(resolve_at(revs, 4), (SELECTED, 3, "v3"))
        revs.append((5, 3, "d", "v3"))  # repeat in another schema with same parsed amount = duplicate
        self.assertEqual(resolve_at(revs, 5), (SELECTED, 3, "v3"))
        self.assertEqual(resolve_at(revs, 1), (SELECTED, 1, "v1"))

    def test_identical_copies_collapse(self):
        revs = [(1, 2, "b", "x"), (1, 2, "b", "x"), (3, 2, "b", "x")]
        self.assertEqual(resolve_at(revs, 3), (SELECTED, 2, "x"))

    def test_conflict_reported_only_at_highest_version(self):
        revs = [(1, 1, "a", "p"), (1, 1, "b", "q"), (2, 2, "c", "r")]
        self.assertEqual(resolve_at(revs, 1), (CONFLICT, 1, None))
        self.assertEqual(resolve_at(revs, 2), (SELECTED, 2, "r"))

    def test_timeline_emits_only_changes(self):
        revs = [(1, 1, "a", "p"), (2, 1, "a", "p"), (3, 2, "b", "q"), (4, 2, "b", "q")]
        self.assertEqual(timeline(revs), [(1, SELECTED, 1, "p"), (3, SELECTED, 2, "q")])

    def test_cutoff_filters_future_knowledge(self):
        revs = [(1, 1, "a", "p"), (9, 2, "b", "q")]
        self.assertEqual(resolve_at(revs, 5), (SELECTED, 1, "p"))
        self.assertIsNone(resolve_at(revs, 0))


if __name__ == "__main__":
    unittest.main()
