"""Property-style differential tests on randomly generated datasets.

For each seed: build N batches that exercise every published test family
(revisions, conflicts, v2 duplicates, date moves, overlapping intervals,
late FX/merchant corrections, redeliveries, voids, erasures, quarantine).
Then check three invariants:

1. incremental engine == full recomputation at every cutoff and date window;
2. the answer at cutoff B does not depend on the order batches were ingested,
   nor on batches ingested after B;
3. replaying every batch again changes nothing.
"""

import json
import os
import random
import unittest

from helpers import EngineCase, erasure, fx, ledger, merchant, profile, raw, quiet_ingest as run_ingest, write_batch

DATES = ["2026-01-01", "2026-01-02", "2026-01-03"]
CURS = ["USD", "EUR", "JPY"]


def gen_batches(seed: int, n_batches: int = 6, n_entries: int = 40):
    rnd = random.Random(seed)
    merchants = ["M1", "M2", "M3"]
    batches = {b: [] for b in range(1, n_batches + 1)}
    # dimensions in batch 1 (with one overlapping interval and one fx hole)
    for m in merchants:
        batches[1].append(merchant(m, region=rnd.choice(["EU", "US"]), tier=rnd.choice(["standard", "premium"])))
    batches[1].append(merchant("M1", vf="2026-01-02T06:00:00Z", vt="2026-01-03T00:00:00Z", region="SG"))  # overlap on Jan 2
    for c in CURS:
        for d in DATES:
            if (c, d) == ("JPY", "2026-01-03"):
                continue  # missing fx
            batches[1].append(fx(c, d, rnd.randint(1, 200000), rnd.randint(1, 9)))
    batches[1].append(profile("C1", "NAME_ONE", "one@example.invalid"))

    versions = {}
    for i in range(n_entries):
        eid = f"E{i}"
        b = rnd.randint(1, n_batches)
        row = ledger(eid, 1, rnd.randint(-10**7, 10**7), merchant=rnd.choice(merchants + ["M_missing"]), token=f"C{rnd.randint(1, 6)}",
                     at=f"{rnd.choice(DATES)}T{rnd.choice(['00:00:00', '06:00:00', '12:00:00', '23:59:59'])}Z", cur=rnd.choice(CURS))
        batches[b].append(row)
        versions[eid] = (1, row)
        # later revisions
        for _ in range(rnd.choice([0, 0, 1, 1, 2, 3])):
            v, prev = versions[eid]
            nb = rnd.randint(1, n_batches)
            kind = rnd.random()
            p = prev["payload"]
            if kind < 0.35:   # ordinary replacement, maybe date move / currency change
                nv = v + 1
                new = ledger(eid, nv, rnd.randint(-10**7, 10**7), merchant=p["merchant_id"], token=p["customer_token"],
                             at=f"{rnd.choice(DATES)}T12:00:00Z", cur=rnd.choice(CURS))
                batches[nb].append(new)
                versions[eid] = (nv, new)
            elif kind < 0.5:  # conflicting pair at the same higher version
                nv = v + 1
                a = ledger(eid, nv, 11, merchant=p["merchant_id"], token=p["customer_token"], at=p["effective_at"], cur=p["currency"])
                b2 = ledger(eid, nv, 12, merchant=p["merchant_id"], token=p["customer_token"], at=p["effective_at"], cur=p["currency"])
                batches[nb].append(a)
                batches[rnd.randint(1, n_batches)].append(b2)
                versions[eid] = (nv, a)
            elif kind < 0.62:  # duplicate in schema 2 (same business fields)
                amt = p.get("amount_minor", p.get("amount_minor_text"))
                dup = ledger(eid, v, int(amt), merchant=p["merchant_id"], token=p["customer_token"], at=p["effective_at"], cur=p["currency"], sv=2)
                batches[nb].append(dup)
            elif kind < 0.72:  # exact redelivery (same row_id) in another batch
                batches[nb].append(dict(prev))
            elif kind < 0.85:  # void
                nv = v + 1
                vd = ledger(eid, nv, 0, op="VOID", merchant=p["merchant_id"], token=p["customer_token"], at=p["effective_at"], cur=p["currency"])
                batches[nb].append(vd)
                versions[eid] = (nv, vd)
            else:             # re-send an old version (must not restore / must not matter)
                batches[nb].append(dict(prev))
    # late corrections, erasures, quarantine
    batches[rnd.randint(2, n_batches)].append(fx("USD", "2026-01-01", 7, 3, version=2))
    batches[rnd.randint(2, n_batches)].append(merchant("M2", version=2, region="XX", tier="gold"))
    batches[rnd.randint(2, n_batches)].append(merchant("M3", version=2, vt="2026-01-02T00:00:00Z"))  # shrink interval
    batches[rnd.randint(1, n_batches)].append(erasure("C1"))
    batches[rnd.randint(1, n_batches)].append(erasure("C3"))
    batches[rnd.randint(1, n_batches)].append(raw("ledger", 99, {"entry_id": "E0", "version": 50}, row_id=f"bad{seed}"))
    batches[rnd.randint(1, n_batches)].append(raw("mystery", 1, {"x": 1}, row_id=f"mys{seed}"))
    batches[rnd.randint(2, n_batches)].append(profile("C1", "NAME_AGAIN", "again@example.invalid"))
    for b in batches:  # every batch must be nonempty
        if not batches[b]:
            batches[b].append(ledger(f"filler{b}", 1, 1, merchant="M1"))
    return batches


WINDOWS = [("2026-01-01", "2026-01-04"), ("2026-01-02", "2026-01-03"), ("2026-01-01", "2026-01-02"), ("2026-01-03", "2026-01-04")]


class Differential(EngineCase):
    SEEDS = range(8)

    def _ingest_order(self, state, batches, order):
        d = os.path.join(self.tmp, f"in-{abs(hash(tuple(order)))}")
        os.makedirs(d, exist_ok=True)
        for b in order:
            self.assertEqual(run_ingest(write_batch(d, b, batches[b]), state, None), 0)

    def _answers(self, state, n_batches, engine):
        from recon.snapshot import compute
        from recon.store import Store

        s = Store.open(state, create=False)
        try:
            out = {}
            for b in range(1, n_batches + 1):
                for w in WINDOWS:
                    s.begin_read()
                    out[(b, w)] = compute(s, {"query_id": "d", "cutoff_batch": b, "start_date": w[0], "end_date": w[1]}, engine)
                    s.rollback()
            return out
        finally:
            s.close()

    def test_incremental_equals_full_and_is_order_invariant(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                batches = gen_batches(seed)
                n = len(batches)
                rnd = random.Random(seed * 31)
                orders = [list(range(1, n + 1)), list(range(n, 0, -1)), rnd.sample(range(1, n + 1), n)]
                baseline = None
                for i, order in enumerate(orders):
                    state = os.path.join(self.tmp, f"s{seed}-{i}")
                    self._ingest_order(state, batches, order)
                    inc = self._answers(state, n, "incremental")
                    full = self._answers(state, n, "full")
                    self.assertEqual(inc, full, f"seed {seed} order {order}: engines disagree")
                    if baseline is None:
                        baseline = inc
                    else:
                        self.assertEqual(inc, baseline, f"seed {seed}: order {order} changed answers")
                # sanity: the generator actually produced the interesting cases
                allex = {e["code"] for v in baseline.values() for e in v["exceptions"]}
                self.assertTrue({"REVISION_CONFLICT", "UNSUPPORTED_SCHEMA"} <= allex, allex)
                # replay everything again: nothing changes
                state = os.path.join(self.tmp, f"s{seed}-0")
                self._ingest_order(state, batches, list(range(1, n + 1)))
                self.assertEqual(self._answers(state, n, "incremental"), baseline)

    def test_prefix_answers_are_stable_as_later_batches_arrive(self):
        for seed in (3, 11):
            with self.subTest(seed=seed):
                batches = gen_batches(seed)
                n = len(batches)
                state = os.path.join(self.tmp, f"p{seed}")
                d = os.path.join(self.tmp, f"pin{seed}")
                os.makedirs(d)
                seen = {}
                erasures_so_far = set()
                for b in range(1, n + 1):
                    self.assertEqual(run_ingest(write_batch(d, b, batches[b]), state, None), 0)
                    erasures_so_far |= {r["payload"]["customer_token"] for r in batches[b] if r["stream"] == "erasures"}
                    now = self._answers(state, b, "incremental")
                    for (cb, w), res in seen.items():
                        cur = now[(cb, w)]
                        # business values identical; only masking may newly apply
                        self.assertEqual(cur["settlements"], res["settlements"], (seed, b, cb, w))
                        self.assertEqual(cur["exceptions"], res["exceptions"], (seed, b, cb, w))
                        for t_old, t_new in zip(res["trace"], cur["trace"]):
                            a, c = dict(t_old), dict(t_new)
                            tok_old, tok_new = a.pop("customer_token"), c.pop("customer_token")
                            self.assertEqual(a, c)
                            if tok_old is None:
                                self.assertIsNone(tok_new)
                            elif tok_new is None:
                                self.assertIn(tok_old, erasures_so_far)
                            else:
                                self.assertEqual(tok_old, tok_new)
                    seen = now


if __name__ == "__main__":
    unittest.main()
