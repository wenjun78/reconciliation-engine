"""Operator commands: status, explain (lineage), check (integrity/PII/differential)."""

from __future__ import annotations

import json
import os
import re
import sys
from fractions import Fraction

from .money import to_usd_micro
from .resolve import SELECTED, latest_at, timeline
from .snapshot import compute
from .store import DB_NAME, Store

EXIT_OK = 0
EXIT_DATAERR = 65
EXIT_CHECK_FAILED = 1

EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def gather_status(store: Store, state_dir: str) -> dict:
    c = store.conn
    batches = store.committed_batches()
    hi = max(batches) if batches else 0
    gaps = [b for b in range(1, hi + 1) if b not in set(batches)]
    contiguous = 0
    for b in range(1, hi + 1):
        if b in set(batches):
            contiguous = b
        else:
            break
    staged = [r[0] for r in c.execute("SELECT batch FROM stage_batches ORDER BY batch")]
    counts = {}
    for t in ("ledger_rev", "merchant_rev", "fx_rev", "ledger_state", "merchant_state", "fx_state", "quarantine", "erasures", "txn_log", "stage_rows"):
        counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    files = {}
    for name in (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm"):
        p = os.path.join(state_dir, name)
        if os.path.exists(p):
            files[name] = os.path.getsize(p)
    meta = dict(c.execute("SELECT key, value FROM meta"))
    return {
        "state_dir": os.path.abspath(state_dir),
        "schema": meta,
        "committed_batches": batches,
        "highest_committed": hi,
        "highest_servable_cutoff": contiguous,
        "gaps": gaps,
        "staged_uncommitted": staged,
        "row_counts": counts,
        "files_bytes": files,
        "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0],
    }


def run_status(state_dir: str, as_json: bool) -> int:
    try:
        store = Store.open(state_dir, create=False)
    except FileNotFoundError:
        print(f"recon status: no state at {state_dir}", file=sys.stderr)
        return EXIT_DATAERR
    try:
        store.begin_read()
        st = gather_status(store, state_dir)
        store.rollback()
    finally:
        store.close()
    if as_json:
        _print(st)
    else:
        print(f"state:            {st['state_dir']}")
        print(f"committed:        {st['committed_batches']}")
        print(f"servable cutoff:  {st['highest_servable_cutoff']}  (gaps: {st['gaps'] or 'none'})")
        print(f"staged/uncommit:  {st['staged_uncommitted'] or 'none'}")
        print(f"rows:             {st['row_counts']}")
        print(f"files:            {st['files_bytes']}  journal={st['journal_mode']}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# explain: lineage of one ledger entry
# ---------------------------------------------------------------------------

def explain_entry(store: Store, entry_id: str, cutoff: int | None) -> dict:
    c = store.conn
    committed = store.committed_batches()
    if cutoff is None:
        cutoff = max(committed) if committed else 0
    erased = {r[0] for r in c.execute("SELECT customer_token FROM erasures")}

    def mask(tok):
        return None if tok in erased else tok

    revisions = []
    revs = []
    for b, row_id, sv, version, fp, op, mid, tok, eff, cur, amt in c.execute(
        "SELECT batch,row_id,schema_version,version,fp,op,merchant_id,customer_token,effective_at,currency,amount_minor FROM ledger_rev WHERE entry_id=? ORDER BY batch,row_id",
        (entry_id,),
    ):
        revisions.append(
            {"batch": b, "row_id": row_id, "schema_version": sv, "version": version, "fingerprint": fp, "op": op,
             "merchant_id": mid, "customer_token": mask(tok), "effective_at": eff, "currency": cur, "amount_minor": int(amt),
             "considered_at_cutoff": b <= cutoff}
        )
        revs.append((b, version, fp, (op, mid, tok, eff, cur, amt)))
    tl = [{"as_of": a, "status": s, "version": v} for a, s, v, _ in timeline(revs)]
    materialised = [
        {"as_of": a, "status": s, "version": v}
        for a, s, v in c.execute("SELECT as_of,status,version FROM ledger_state WHERE entry_id=? ORDER BY as_of", (entry_id,))
    ]
    pt = latest_at(timeline(revs), cutoff)
    out = {
        "entry_id": entry_id,
        "cutoff_batch": cutoff,
        "committed_batches": committed,
        "revisions": revisions,
        "timeline_recomputed": tl,
        "timeline_materialised": materialised,
        "timelines_agree": tl == materialised,
        "resolution": None,
        "join": None,
        "arithmetic": None,
    }
    if pt is None:
        out["resolution"] = {"status": "UNKNOWN_KEY", "note": "no revision with batch <= cutoff"}
        return out
    as_of, status, version, payload = pt
    if status != SELECTED:
        out["resolution"] = {"status": status, "version": version, "since_batch": as_of,
                             "exception": {"stream": "ledger", "key": entry_id, "code": "REVISION_CONFLICT", "version": version}}
        return out
    op, mid, tok, eff, cur, amt = payload
    out["resolution"] = {"status": status, "version": version, "since_batch": as_of, "op": op, "merchant_id": mid,
                         "customer_token": mask(tok), "effective_at": eff, "currency": cur, "amount_minor": int(amt)}
    if op == "VOID":
        out["join"] = {"note": "VOID selected: no join, no contribution, no trace"}
        return out

    # Merchant intervals (resolved from revision history at cutoff)
    hits, blocked = [], []
    groups: dict[str, list] = {}
    for vf, b, version_m, fp, vt, region, tier, row_id in c.execute(
        "SELECT valid_from,batch,version,fp,valid_to,region,tier,row_id FROM merchant_rev WHERE merchant_id=? AND batch<=? ORDER BY valid_from,batch",
        (mid, cutoff),
    ):
        groups.setdefault(vf, []).append((b, version_m, fp, (vt, region, tier, row_id)))
    for vf, g in groups.items():
        p = latest_at(timeline(g), cutoff)
        if p is None:
            continue
        _a, s, v, pl = p
        if s != SELECTED:
            blocked.append({"key": f"{mid}|{vf}", "version": v})
            continue
        vt, region, tier, row_id = pl
        contains = vf <= eff < vt
        hits.append({"valid_from": vf, "valid_to": vt, "region": region, "tier": tier, "version": v, "winning_row_id": row_id, "contains_effective_at": contains})
    containing = [h for h in hits if h["contains_effective_at"]]
    join = {"merchant_intervals_selected": hits, "merchant_keys_blocked": blocked}
    if len(containing) == 0:
        join["exception"] = {"stream": "ledger", "key": entry_id, "code": "MISSING_MERCHANT", "version": version}
        out["join"] = join
        return out
    if len(containing) > 1:
        join["exception"] = {"stream": "ledger", "key": entry_id, "code": "AMBIGUOUS_MERCHANT", "version": version}
        out["join"] = join
        return out
    date = eff[:10]
    fxrevs = [
        (b, v, fp, (num, den, row_id))
        for b, v, fp, num, den, row_id in c.execute(
            "SELECT batch,version,fp,num,den,row_id FROM fx_rev WHERE currency=? AND date=? AND batch<=?", (cur, date, cutoff)
        )
    ]
    p = latest_at(timeline(fxrevs), cutoff) if fxrevs else None
    if p is None or p[1] != SELECTED:
        join["fx"] = None if p is None else {"key": f"{cur}|{date}", "status": p[1], "version": p[2]}
        join["exception"] = {"stream": "ledger", "key": entry_id, "code": "MISSING_FX", "version": version}
        out["join"] = join
        return out
    num, den, row_id = p[3]
    num, den = int(num), int(den)
    join["fx"] = {"key": f"{cur}|{date}", "version": p[2], "usd_micro_num": num, "usd_micro_den": den, "winning_row_id": row_id}
    out["join"] = join
    exact = Fraction(int(amt) * num, den)
    usd = to_usd_micro(int(amt), num, den)
    out["arithmetic"] = {
        "expression": f"{int(amt)} * {num} / {den}",
        "exact": f"{exact.numerator}/{exact.denominator}" if exact.denominator != 1 else str(exact.numerator),
        "rounded_half_even_usd_micro": usd,
        "settlement_group": {"date": date, "merchant_id": mid, "region": containing[0]["region"], "tier": containing[0]["tier"]},
        "trace_customer_token": mask(tok),
    }
    return out


def run_explain(state_dir: str, entry_id: str, cutoff: int | None) -> int:
    try:
        store = Store.open(state_dir, create=False)
    except FileNotFoundError:
        print(f"recon explain: no state at {state_dir}", file=sys.stderr)
        return EXIT_DATAERR
    try:
        store.begin_read()
        res = explain_entry(store, entry_id, cutoff)
        store.rollback()
    finally:
        store.close()
    _print(res)
    return EXIT_OK


# ---------------------------------------------------------------------------
# check: integrity, PII scan, differential verification
# ---------------------------------------------------------------------------

def pii_scan(state_dir: str) -> dict:
    """Scan every byte of managed storage for e-mail-shaped strings.

    Names are synthetic and unbounded, so the scan targets the one PII shape
    with a reliable signature.  The engine never stores names or emails, so
    the expected count is always zero, independent of erasure.
    """
    hits = 0
    scanned = []
    for root, _dirs, files in os.walk(state_dir):
        for fn in files:
            p = os.path.join(root, fn)
            scanned.append(os.path.relpath(p, state_dir))
            with open(p, "rb") as f:
                prev = b""
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    hits += len(EMAIL_RE.findall(prev[-256:] + chunk))
                    prev = chunk
    return {"files_scanned": scanned, "email_like_strings": hits}


def run_check(state_dir: str, deep: bool, start: str, end: str) -> int:
    try:
        store = Store.open(state_dir, create=False)
    except FileNotFoundError:
        print(f"recon check: no state at {state_dir}", file=sys.stderr)
        return EXIT_DATAERR
    report: dict = {"ok": True, "problems": []}
    try:
        store.begin_read()
        ic = store.conn.execute("PRAGMA integrity_check").fetchall()
        report["integrity_check"] = [r[0] for r in ic]
        if ic != [("ok",)]:
            report["problems"].append("sqlite integrity_check failed")
        st = gather_status(store, state_dir)
        report["status"] = st
        if st["staged_uncommitted"]:
            report["problems"].append(f"staged but uncommitted batches present: {st['staged_uncommitted']} (replay them)")
        # Timeline invariants: materialised points never exceed revision batches
        bad = store.conn.execute(
            "SELECT COUNT(*) FROM ledger_state s WHERE NOT EXISTS (SELECT 1 FROM ledger_rev r WHERE r.entry_id=s.entry_id AND r.batch=s.as_of)"
        ).fetchone()[0]
        if bad:
            report["problems"].append(f"{bad} ledger_state points have no revision at their as_of batch")
        store.rollback()
        report["pii_scan"] = pii_scan(state_dir)
        if report["pii_scan"]["email_like_strings"]:
            report["problems"].append("email-like strings found in managed storage")

        if deep:
            diffs = []
            # Compare at every servable cutoff (1..contiguous) over the requested range
            for b in range(1, st["highest_servable_cutoff"] + 1):
                q = {"query_id": f"check-{b}", "cutoff_batch": b, "start_date": start, "end_date": end}
                store.begin_read()
                inc = compute(store, q, "incremental")
                full = compute(store, q, "full")
                store.rollback()
                if inc != full:
                    diffs.append(b)
            report["differential"] = {"cutoffs_compared": st["highest_servable_cutoff"], "cutoffs_differing": diffs}
            if diffs:
                report["problems"].append(f"incremental and full engines disagree at cutoffs {diffs}")
    finally:
        store.close()
    report["ok"] = not report["problems"]
    _print(report)
    return EXIT_OK if report["ok"] else EXIT_CHECK_FAILED
