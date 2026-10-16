#!/usr/bin/env python3
"""Deterministic scale fixture generator (shape of the hidden scale suite).

Defaults: 200,000 ledger keys plus revisions, 12 batches, 100 merchants, four
currencies, January 2026, deliberately skewed (one merchant takes ~30% of
entries, one day ~20%).  Includes every disclosed test family: v2 duplicates,
conflicts, resolutions, voids, date moves, exact redeliveries, overlapping and
corrected intervals, late FX corrections, unsupported schemas, profiles and
erasures.  Output is streamed line by line so the generator itself stays small.

    python3 bench/gen_scale.py --out /tmp/scale --keys 200000 --batches 12 --seed 17
"""

from __future__ import annotations

import argparse
import json
import os
import random

CURS = ["USD", "EUR", "JPY", "SGD"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--keys", type=int, default=200_000)
    ap.add_argument("--batches", type=int, default=12)
    ap.add_argument("--merchants", type=int, default=100)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)
    files = {b: open(os.path.join(args.out, f"batch_{b:02d}.jsonl"), "w", encoding="utf-8") for b in range(1, args.batches + 1)}
    seq = [0]

    def emit(batch: int, stream: str, sv, payload: dict, row_id: str | None = None) -> str:
        seq[0] += 1
        rid = row_id or f"s{args.seed}_{seq[0]:09d}"
        files[batch].write(json.dumps({"batch": batch, "row_id": rid, "stream": stream, "schema_version": sv, "payload": payload}, separators=(",", ":")) + "\n")
        return rid

    dates = [f"2026-01-{d:02d}" for d in range(1, args.days + 1)]
    merchants = [f"M{i}" for i in range(args.merchants)]

    # ---- batch 1: dimensions ---------------------------------------------------
    for m in merchants:
        emit(1, "merchants", 1, {"merchant_id": m, "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
                                 "version": 1, "region": rnd.choice(["EU", "US", "SG", "JP"]), "tier": rnd.choice(["standard", "premium"])})
    # one merchant with a split interval (two keys) and one overlapping key that gets corrected later
    emit(1, "merchants", 1, {"merchant_id": "M1", "valid_from": "2026-01-15T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z", "version": 1, "region": "US", "tier": "premium"})
    for c in CURS:
        for d in dates:
            if (c, d) == ("SGD", dates[-1]):
                continue  # missing FX on the last day for SGD
            emit(1, "fx", 1, {"currency": c, "date": d, "version": 1, "usd_micro_num": rnd.randint(1, 2_000_000), "usd_micro_den": rnd.choice([1, 2, 3, 7, 11, 13])})
    for i in range(5000):
        emit(1, "profiles", 1, {"customer_token": f"C{i}", "name": f"SYNTHETIC_NAME_{i}", "email": f"synthetic_{i}@example.invalid"})

    # ---- ledger keys across batches 2..N ----------------------------------------
    body = list(range(2, args.batches + 1))
    skew_merchant = merchants[0]
    skew_date = dates[rnd.randrange(len(dates))]

    def pick_merchant():
        return skew_merchant if rnd.random() < 0.30 else rnd.choice(merchants)

    def pick_date():
        return skew_date if rnd.random() < 0.20 else rnd.choice(dates)

    def ledger_payload(eid, version, amount, op="POST", merchant=None, token=None, date=None, cur=None):
        return {"entry_id": eid, "version": version, "op": op, "merchant_id": merchant, "customer_token": token,
                "effective_at": f"{date}T{rnd.randrange(24):02d}:{rnd.randrange(60):02d}:{rnd.randrange(60):02d}Z",
                "currency": cur, "amount_minor": amount}

    n_rev = n_conf = n_dup = n_void = n_redeliver = 0
    for i in range(args.keys):
        eid = f"E{i}"
        b = rnd.choice(body)
        merchant, token, date, cur = pick_merchant(), f"C{rnd.randrange(6000)}", pick_date(), rnd.choice(CURS)
        amount = rnd.randint(-10_000_000, 10_000_000)
        if rnd.random() < 0.001:
            amount = rnd.choice([9007199254740993, -9007199254740993, 10**20 + 1])
        p = ledger_payload(eid, 1, amount, merchant=merchant, token=token, date=date, cur=cur)
        rid = emit(b, "ledger", 1, p)
        r = rnd.random()
        if r < 0.15:  # replacement in a later (or earlier-numbered!) batch, sometimes a date move
            n_rev += 1
            b2 = rnd.choice(body)
            d2 = pick_date() if rnd.random() < 0.3 else date
            p2 = ledger_payload(eid, 2, rnd.randint(-10_000_000, 10_000_000), merchant=merchant, token=token, date=d2, cur=cur)
            emit(b2, "ledger", 1, p2)
            if rnd.random() < 0.2:  # v3 in schema 2 as a duplicate of a v3 in schema 1
                n_dup += 1
                b3 = rnd.choice(body)
                p3 = ledger_payload(eid, 3, rnd.randint(-10_000_000, 10_000_000), merchant=merchant, token=token, date=d2, cur=cur)
                emit(b3, "ledger", 1, p3)
                p3b = dict(p3)
                p3b["amount_minor_text"] = str(p3b.pop("amount_minor"))
                emit(rnd.choice(body), "ledger", 2, p3b)
        elif r < 0.17:  # conflict at v2; a third of them resolved by v3
            n_conf += 1
            pa = ledger_payload(eid, 2, 11, merchant=merchant, token=token, date=date, cur=cur)
            pb = dict(pa, amount_minor=12)
            emit(rnd.choice(body), "ledger", 1, pa)
            emit(rnd.choice(body), "ledger", 1, pb)
            if rnd.random() < 0.33:
                emit(rnd.choice(body), "ledger", 1, ledger_payload(eid, 3, 13, merchant=merchant, token=token, date=date, cur=cur))
        elif r < 0.19:  # void
            n_void += 1
            emit(rnd.choice(body), "ledger", 1, dict(p, version=2, op="VOID"))
        elif r < 0.21:  # exact redelivery (same row_id) in another batch
            n_redeliver += 1
            emit(rnd.choice(body), "ledger", 1, p, row_id=rid)

    # ---- late corrections, erasures, unsupported ----------------------------------
    for _ in range(12):
        c, d = rnd.choice(CURS), rnd.choice(dates)
        emit(rnd.choice(body), "fx", 1, {"currency": c, "date": d, "version": 2, "usd_micro_num": rnd.randint(1, 2_000_000), "usd_micro_den": rnd.choice([1, 2, 3, 7])})
    emit(rnd.choice(body), "merchants", 1, {"merchant_id": "M1", "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-01-15T00:00:00Z", "version": 2, "region": "EU", "tier": "standard"})
    emit(rnd.choice(body), "merchants", 1, {"merchant_id": "M2", "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z", "version": 2, "region": "ZZ", "tier": "gold"})
    emit(rnd.choice(body), "merchants", 1, {"merchant_id": "MX", "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z", "version": 1, "region": "A", "tier": "x"})
    emit(rnd.choice(body), "merchants", 1, {"merchant_id": "MX", "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z", "version": 1, "region": "B", "tier": "x"})
    for i in range(500):
        emit(rnd.choice(body[len(body) // 2:]), "erasures", 1, {"customer_token": f"C{rnd.randrange(6000)}"})
    for i in range(100):
        emit(rnd.choice(body), "ledger", 99, {"entry_id": f"E{rnd.randrange(args.keys)}", "version": 9, "op": "POST", "merchant_id": "M0",
                                             "customer_token": "C0", "effective_at": "2026-01-01T00:00:00Z", "currency": "USD", "amount_minor": 1})
        emit(rnd.choice(body), "future_stream", 1, {"note": "ignore"})
    for i in range(200):
        emit(rnd.choice(body), "profiles", 1, {"customer_token": f"C{i}", "name": f"REPLAY_NAME_{i}", "email": f"replay_{i}@example.invalid"})

    for f in files.values():
        f.close()
    sizes = {os.path.basename(p): os.path.getsize(p) for p in sorted(os.path.join(args.out, n) for n in os.listdir(args.out))}
    print(json.dumps({"keys": args.keys, "batches": args.batches, "rows": seq[0], "revisions": n_rev, "conflicts": n_conf,
                      "schema2_dups": n_dup, "voids": n_void, "redeliveries": n_redeliver, "bytes": sum(sizes.values()), "files": sizes}, indent=1))


if __name__ == "__main__":
    main()
