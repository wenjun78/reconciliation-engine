"""Spec semantics on small hand-built datasets.  Every test names the rule it pins.

All results are computed with BOTH engines (helpers.both) so a divergence
between incremental state and full recomputation fails the test too.
"""

import unittest

from helpers import EngineCase, erasure, exc_set, fx, ledger, merchant, raw, trace_map

DIM = [merchant("M1"), fx("USD", "2026-01-01", 1, 2), fx("USD", "2026-01-02", 1, 2), fx("USD", "2026-01-03", 1, 2)]


class Money(EngineCase):
    def test_worked_example_1_group_net_zero_count_four(self):
        self.ingest(1, DIM + [ledger("A", 1, 1), ledger("B", 1, 3), ledger("C", 1, -1), ledger("D", 1, -3)])
        r = self.both(1)
        self.assertEqual(r["settlements"], [{"date": "2026-01-01", "merchant_id": "M1", "region": "EU", "tier": "standard",
                                             "net_usd_micro": 0, "entry_count": 4}])
        self.assertEqual([t["usd_micro"] for t in r["trace"]], [0, 2, 0, -2])

    def test_each_entry_rounded_independently_never_the_sum(self):
        # three entries of 1 at 1/2 each round to 0 -> net 0, not round(1.5)=2
        self.ingest(1, DIM + [ledger("A", 1, 1), ledger("B", 1, 1), ledger("C", 1, 1)])
        self.assertEqual(self.both(1)["settlements"][0]["net_usd_micro"], 0)

    def test_worked_example_7_exact_big_integer(self):
        big = 9007199254740993
        self.ingest(1, [merchant("M1"), fx("USD", "2026-01-01", 1, 1), ledger("A", 1, big)])
        r = self.both(1)
        self.assertEqual(r["trace"][0]["usd_micro"], big)
        self.assertEqual(r["settlements"][0]["net_usd_micro"], big)

    def test_amount_beyond_int64_round_trips(self):
        big = 10**25 + 7
        self.ingest(1, [merchant("M1"), fx("USD", "2026-01-01", 3, 1), ledger("A", 1, big), ledger("B", 1, -big, sv=2)])
        r = self.both(1)
        self.assertEqual({t["entry_id"]: t["usd_micro"] for t in r["trace"]}, {"A": 3 * big, "B": -3 * big})
        self.assertEqual(r["settlements"][0]["net_usd_micro"], 0)

    def test_usd_has_no_implicit_rate(self):
        self.ingest(1, [merchant("M1"), ledger("A", 1, 100)])
        r = self.both(1)
        self.assertEqual(r["settlements"], [])
        self.assertEqual(exc_set(r), {("ledger", "A", "MISSING_FX", 1)})

    def test_fx_is_microdollars_per_minor_unit_no_cents_conversion(self):
        self.ingest(1, [merchant("M1"), fx("USD", "2026-01-01", 10000, 1), ledger("A", 1, 7)])
        self.assertEqual(self.both(1)["trace"][0]["usd_micro"], 70000)


class Revisions(EngineCase):
    def test_worked_example_2_replacement_and_void(self):
        self.ingest(1, [merchant("M1"), fx("USD", "2026-01-01", 2, 1), ledger("A", 1, 100)])
        self.ingest(2, [ledger("A", 2, 70)])
        self.ingest(3, [ledger("A", 3, 70, op="VOID")])
        self.assertEqual(self.both(1)["trace"][0]["usd_micro"], 200)
        self.assertEqual(self.both(2)["trace"][0]["usd_micro"], 140)
        r3 = self.both(3)
        self.assertEqual(r3["trace"], [])
        self.assertEqual(r3["settlements"], [])
        self.assertEqual(r3["exceptions"], [])

    def test_worked_example_3_conflict_block_restore_duplicate(self):
        self.ingest(1, DIM + [ledger("A", 1, 100)])
        self.ingest(2, [ledger("A", 2, 41), ledger("A", 2, 43)])
        self.ingest(3, [ledger("A", 1, 100)])                 # seeing v1 again does not restore
        self.ingest(4, [ledger("A", 3, 99)])                  # unambiguous v3 restores
        self.ingest(5, [ledger("A", 3, 99, sv=2)])            # schema-2 repeat is a duplicate
        self.assertEqual(self.both(1)["trace"][0]["usd_micro"], 50)
        for b in (2, 3):
            r = self.both(b)
            self.assertEqual(r["trace"], [])
            self.assertEqual(exc_set(r), {("ledger", "A", "REVISION_CONFLICT", 2)})
        for b in (4, 5):
            r = self.both(b)
            self.assertEqual(r["exceptions"], [])
            self.assertEqual(r["trace"][0]["version"], 3)
            self.assertEqual(r["trace"][0]["usd_micro"], 50)  # 49.5 -> 50 (even)

    def test_conflict_with_a_void_variant_is_still_a_conflict(self):
        self.ingest(1, DIM + [ledger("A", 1, 10), ledger("A", 2, 10), ledger("A", 2, 10, op="VOID")])
        r = self.both(1)
        self.assertEqual(exc_set(r), {("ledger", "A", "REVISION_CONFLICT", 2)})
        self.assertEqual(r["trace"], [])

    def test_conflict_outside_date_range_is_still_reported(self):
        self.ingest(1, DIM + [ledger("A", 1, 10, at="2026-03-01T00:00:00Z"), ledger("A", 1, 11, at="2026-03-01T00:00:00Z")])
        r = self.both(1, start="2026-01-01", end="2026-01-02")
        self.assertEqual(exc_set(r), {("ledger", "A", "REVISION_CONFLICT", 1)})

    def test_duplicate_within_one_batch_and_exact_redelivery_in_a_later_batch(self):
        row = ledger("A", 1, 8)
        self.ingest(1, DIM + [row, row])
        self.ingest(2, [row])
        for b in (1, 2):
            r = self.both(b)
            self.assertEqual(len(r["trace"]), 1)
            self.assertEqual(r["exceptions"], [])

    def test_revision_may_change_merchant_currency_and_customer(self):
        self.ingest(1, DIM + [merchant("M2", region="US", tier="premium"), fx("EUR", "2026-01-01", 3, 1), ledger("A", 1, 10)])
        self.ingest(2, [ledger("A", 2, 10, merchant="M2", cur="EUR", token="C9")])
        r = self.both(2)
        t = r["trace"][0]
        self.assertEqual((t["merchant_id"], t["region"], t["tier"], t["usd_micro"], t["customer_token"]), ("M2", "US", "premium", 30, "C9"))


class KnowledgeTime(EngineCase):
    def test_worked_example_5_date_move_invalidates_both_days(self):
        self.ingest(1, DIM + [ledger("A", 1, 100)])
        self.ingest(2, [ledger("A", 2, 100, at="2026-01-02T12:00:00Z")])
        day1 = ("2026-01-01", "2026-01-02")
        day2 = ("2026-01-02", "2026-01-03")
        self.assertEqual(len(self.both(1, start=day1[0], end=day1[1])["trace"]), 1)
        self.assertEqual(len(self.both(1, start=day2[0], end=day2[1])["trace"]), 0)
        self.assertEqual(len(self.both(2, start=day1[0], end=day1[1])["trace"]), 0)
        self.assertEqual(len(self.both(2, start=day2[0], end=day2[1])["trace"]), 1)

    def test_worked_example_6_late_fx_correction_does_not_rewrite_history(self):
        self.ingest(1, DIM + [ledger("A", 1, 100)])
        self.ingest(2, [fx("USD", "2026-01-01", 3, 2, version=2)])
        self.assertEqual(self.both(1)["trace"][0]["usd_micro"], 50)
        self.assertEqual(self.both(2)["trace"][0]["usd_micro"], 150)

    def test_late_merchant_correction_changes_only_newer_cutoffs(self):
        self.ingest(1, DIM + [ledger("A", 1, 100)])
        self.ingest(2, [merchant("M1", version=2, region="US", tier="premium")])
        self.assertEqual(self.both(1)["trace"][0]["region"], "EU")
        self.assertEqual(self.both(2)["trace"][0]["region"], "US")

    def test_ingesting_later_batches_does_not_change_earlier_cutoffs(self):
        self.ingest(1, DIM + [ledger("A", 1, 100), ledger("B", 1, 5)])
        before = self.both(1)
        self.ingest(2, [ledger("A", 2, 7), ledger("B", 2, 5, op="VOID"), fx("USD", "2026-01-01", 9, 1, version=2),
                        merchant("M1", version=2, region="XX"), erasure("nobody")])
        self.assertEqual(self.both(1), before)

    def test_reordered_delivery_gives_same_answers(self):
        b1 = DIM + [ledger("A", 1, 100)]
        b2 = [ledger("A", 2, 70)]
        b3 = [fx("USD", "2026-01-01", 1, 1, version=2)]
        self.ingest(3, b3)
        self.ingest(1, b1)
        self.ingest(2, b2)
        self.assertEqual(self.both(1)["trace"][0]["usd_micro"], 50)
        self.assertEqual(self.both(2)["trace"][0]["usd_micro"], 35)
        self.assertEqual(self.both(3)["trace"][0]["usd_micro"], 70)

    def test_snapshot_requires_contiguous_prefix(self):
        self.ingest(1, DIM + [ledger("A", 1, 100)])
        self.ingest(3, [ledger("A", 2, 70)])
        self.assertEqual(self.snapshot(1)[0], 0)
        self.assertEqual(self.snapshot(2)[0], 65)
        self.assertEqual(self.snapshot(3)[0], 65)
        self.assertEqual(self.snapshot(4)[0], 65)
        self.ingest(2, [ledger("B", 1, 1)])
        self.assertEqual(self.snapshot(3)[0], 0)


class Joins(EngineCase):
    def test_worked_example_4_half_open_interval_boundary(self):
        self.ingest(1, [merchant("M1", vt="2026-01-02T00:00:00Z"), fx("USD", "2026-01-02", 1, 1),
                        ledger("A", 1, 5, at="2026-01-02T00:00:00Z")])
        r = self.both(1)
        self.assertEqual(exc_set(r), {("ledger", "A", "MISSING_MERCHANT", 1)})
        self.assertEqual(r["settlements"], [])

    def test_interval_start_is_inclusive(self):
        self.ingest(1, [merchant("M1", vf="2026-01-01T12:00:00Z"), fx("USD", "2026-01-01", 1, 1), ledger("A", 1, 5)])
        self.assertEqual(self.both(1)["trace"][0]["usd_micro"], 5)

    def test_worked_example_4_ambiguous_even_when_attributes_match(self):
        self.ingest(1, [merchant("M1"), merchant("M1", vf="2026-01-01T06:00:00Z"), fx("USD", "2026-01-01", 1, 1), ledger("A", 1, 5)])
        self.assertEqual(exc_set(self.both(1)), {("ledger", "A", "AMBIGUOUS_MERCHANT", 1)})

    def test_error_precedence_merchant_before_fx(self):
        self.ingest(1, [ledger("A", 1, 5, cur="ZZZ")])  # no merchant and no fx
        self.assertEqual(exc_set(self.both(1)), {("ledger", "A", "MISSING_MERCHANT", 1)})

    def test_join_errors_only_for_selected_posts_in_range(self):
        self.ingest(1, [ledger("A", 1, 5, at="2026-03-01T00:00:00Z"), ledger("B", 1, 5, op="VOID")])
        self.assertEqual(self.both(1, start="2026-01-01", end="2026-02-01")["exceptions"], [])
        self.assertEqual(exc_set(self.both(1, start="2026-03-01", end="2026-03-02")), {("ledger", "A", "MISSING_MERCHANT", 1)})

    def test_blocked_merchant_key_is_absent_for_joins(self):
        # two intervals, one conflicted: the entry matches only the clean one (no ambiguity), conflict reported
        self.ingest(1, [merchant("M1"), merchant("M1", vf="2026-01-01T06:00:00Z", region="A"),
                        merchant("M1", vf="2026-01-01T06:00:00Z", region="B"), fx("USD", "2026-01-01", 1, 1), ledger("A", 1, 5)])
        r = self.both(1)
        self.assertEqual(exc_set(r), {("merchants", "M1|2026-01-01T06:00:00Z", "REVISION_CONFLICT", 1)})
        self.assertEqual(r["trace"][0]["region"], "EU")

    def test_blocked_fx_key_is_missing_fx(self):
        self.ingest(1, [merchant("M1"), fx("USD", "2026-01-01", 1, 1), fx("USD", "2026-01-01", 2, 1), ledger("A", 1, 5)])
        self.assertEqual(exc_set(self.both(1)), {("fx", "USD|2026-01-01", "REVISION_CONFLICT", 1), ("ledger", "A", "MISSING_FX", 1)})

    def test_dimension_conflicts_reported_without_ledger_references(self):
        self.ingest(1, [merchant("MX", region="A"), merchant("MX", region="B"), fx("XTS", "2026-01-01", 1, 1), fx("XTS", "2026-01-01", 2, 1)])
        r = self.both(1, start="2027-01-01", end="2027-01-02")
        self.assertEqual(exc_set(r), {("merchants", "MX|2026-01-01T00:00:00Z", "REVISION_CONFLICT", 1), ("fx", "XTS|2026-01-01", "REVISION_CONFLICT", 1)})

    def test_fx_rate_uses_entry_utc_date(self):
        self.ingest(1, [merchant("M1"), fx("USD", "2026-01-01", 1, 1), fx("USD", "2026-01-02", 100, 1),
                        ledger("A", 1, 1, at="2026-01-01T23:59:59Z"), ledger("B", 1, 1, at="2026-01-02T00:00:00Z")])
        self.assertEqual({t["entry_id"]: t["usd_micro"] for t in self.both(1)["trace"]}, {"A": 1, "B": 100})

    def test_grouping_sorting_and_zero_net_groups(self):
        self.ingest(1, [merchant("M2", region="US", tier="premium"), merchant("M1")] + DIM[1:] +
                    [ledger("Z", 1, 2, merchant="M2"), ledger("A", 1, 2), ledger("B", 1, -2), ledger("C", 1, 4, at="2026-01-02T00:00:00Z")])
        r = self.both(1)
        self.assertEqual([(s["date"], s["merchant_id"], s["net_usd_micro"], s["entry_count"]) for s in r["settlements"]],
                         [("2026-01-01", "M1", 0, 2), ("2026-01-01", "M2", 1, 1), ("2026-01-02", "M1", 2, 1)])
        self.assertEqual([t["entry_id"] for t in r["trace"]], ["A", "B", "C", "Z"])

    def test_empty_range_and_reversed_range(self):
        self.ingest(1, DIM + [ledger("A", 1, 5)])
        r = self.both(1, start="2030-01-01", end="2030-01-02")
        self.assertEqual((r["settlements"], r["trace"], r["exceptions"]), ([], [], []))
        r = self.both(1, start="2026-01-02", end="2026-01-01")
        self.assertEqual(r["trace"], [])


class UnsupportedSchema(EngineCase):
    def test_quarantined_rows_never_take_part_in_selection(self):
        self.ingest(1, DIM + [ledger("A", 1, 5), raw("ledger", 99, {"entry_id": "A", "version": 9, "op": "POST", "merchant_id": "M1",
                                                             "customer_token": "C1", "effective_at": "2026-01-01T12:00:00Z",
                                                             "currency": "USD", "amount_minor": 1}, row_id="bad1"),
                              raw("future_stream", 1, {"note": "ignore"}, row_id="bad2")])
        r = self.both(1)
        self.assertEqual(r["trace"][0]["version"], 1)
        self.assertEqual(exc_set(r), {("ledger", "bad1", "UNSUPPORTED_SCHEMA", None), ("future_stream", "bad2", "UNSUPPORTED_SCHEMA", None)})

    def test_unsupported_rows_reported_regardless_of_date_and_deduplicated(self):
        bad = raw("ledger", 7, {"effective_at": "1999-01-01T00:00:00Z"}, row_id="dup")
        self.ingest(1, [bad])
        self.ingest(2, [bad])
        r = self.both(2, start="2026-01-01", end="2026-01-02")
        self.assertEqual(r["exceptions"], [{"stream": "ledger", "key": "dup", "code": "UNSUPPORTED_SCHEMA", "version": None}])

    def test_exceptions_sorted_by_stream_key_code_version(self):
        self.ingest(1, [raw("ledger", 7, {}, row_id="A"), ledger("A", 1, 1, merchant="nope"), ledger("A", 1, 2, merchant="nope")])
        r = self.both(1)
        self.assertEqual([(e["code"], e["version"]) for e in r["exceptions"]], [("REVISION_CONFLICT", 1), ("UNSUPPORTED_SCHEMA", None)])


if __name__ == "__main__":
    unittest.main()
