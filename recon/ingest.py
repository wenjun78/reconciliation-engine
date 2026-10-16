"""Two-phase batch ingest.

Phase 1 ``stage``   : parse + normalise the file (PII dropped), write the
                      PII-free records into ``stage_rows`` and COMMIT.  This is
                      durable staging: a crash afterwards loses nothing and
                      publishes nothing.
Phase 2 ``publish`` : in ONE write transaction copy staged records into the
                      revision tables, apply erasures/quarantine, rebuild the
                      knowledge-time timelines of every touched key, record the
                      checkpoint row and delete the staging rows.  COMMIT is the
                      single atomic visibility boundary.

Failpoints:
  after-stage    exit 86 after phase 1 committed, before phase 2 starts.
  before-commit  exit 86 inside phase 2 with every write issued, immediately
                 before COMMIT (the process dies without committing; SQLite's
                 journal discards the open transaction on next open).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter

from . import normalize as nz
from .resolve import CONFLICT, SELECTED, timeline
from .store import Store, utcnow

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_DATAERR = 65
EXIT_NOINPUT = 66
EXIT_FAILPOINT = 86

FAILPOINTS = ("after-stage", "before-commit")


def _log(msg: str) -> None:
    print(f"recon ingest: {msg}", file=sys.stderr, flush=True)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_file(path: str) -> tuple[int, list[nz.Rec], Counter]:
    """Parse one JSONL batch file into PII-free records.

    Returns (batch_number, records, per-stream row counts).  Profiles are
    counted and discarded; they never reach the returned list.
    """
    recs: list[nz.Rec] = []
    counts: Counter = Counter()
    batch_no = None
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise nz.InputError(f"line {lineno}: invalid JSON ({e.msg})") from None
            try:
                b, rec = nz.normalise(obj)
            except nz.InputError as e:
                raise nz.InputError(f"line {lineno}: {e}") from None
            if batch_no is None:
                batch_no = b
            elif b != batch_no:
                raise nz.InputError(f"line {lineno}: batch {b} differs from batch {batch_no} in the same file")
            counts[rec.stream if rec.kind != nz.KIND_QUARANTINE else "quarantine"] += 1
            if rec.kind == nz.KIND_PROFILE:
                continue  # discarded on ingest: nothing durable carries name/email
            recs.append(rec)
    if batch_no is None:
        raise nz.InputError("batch file is empty")
    return batch_no, recs, counts


# ---------------------------------------------------------------------------
# Phase 1: durable staging
# ---------------------------------------------------------------------------

def stage(store: Store, batch_no: int, recs: list[nz.Rec], sha: str, source: str, counts: Counter) -> None:
    store.begin_write()
    try:
        store.conn.execute("DELETE FROM stage_rows WHERE batch=?", (batch_no,))
        store.conn.execute("DELETE FROM stage_batches WHERE batch=?", (batch_no,))
        store.conn.executemany(
            "INSERT INTO stage_rows(batch,seq,rec) VALUES(?,?,?)",
            ((batch_no, i, nz.rec_to_json(r)) for i, r in enumerate(recs)),
        )
        store.conn.execute(
            "INSERT INTO stage_batches(batch,file_sha256,source,row_count,staged_at,stats) VALUES(?,?,?,?,?,?)",
            (batch_no, sha, source, sum(counts.values()), utcnow(), json.dumps(counts, sort_keys=True)),
        )
        store.log(batch_no, "stage", "committed", {"sha256": sha, "rows": sum(counts.values()), "streams": dict(counts)})
        store.commit()
    except BaseException:
        store.rollback()
        raise


# ---------------------------------------------------------------------------
# Phase 2: atomic publication
# ---------------------------------------------------------------------------

def _rebuild_ledger_timelines(store: Store, batch_no: int, entry_ids: set[str]) -> int:
    if not entry_ids:
        return 0
    c = store.conn
    c.execute("CREATE TEMP TABLE IF NOT EXISTS touched_ledger(entry_id TEXT PRIMARY KEY)")
    c.execute("DELETE FROM touched_ledger")
    c.executemany("INSERT INTO touched_ledger(entry_id) VALUES(?)", ((e,) for e in entry_ids))
    c.execute(
        "DELETE FROM ledger_state WHERE as_of>=? AND entry_id IN (SELECT entry_id FROM touched_ledger)",
        (batch_no,),
    )
    rows = c.execute(
        "SELECT r.entry_id, r.batch, r.version, r.fp, r.op, r.merchant_id, r.customer_token, r.effective_at, r.currency, r.amount_minor "
        "FROM ledger_rev r JOIN touched_ledger t ON t.entry_id=r.entry_id ORDER BY r.entry_id, r.batch"
    )
    inserts = []
    cur_key = None
    revs: list[tuple] = []

    def flush():
        if cur_key is None:
            return
        for as_of, status, version, payload in timeline(revs):
            if as_of < batch_no:
                continue  # points before this batch are already materialised and unchanged
            if status == SELECTED:
                op, merchant_id, token, effective_at, currency, amount = payload
                inserts.append((cur_key, as_of, status, version, op, merchant_id, token, effective_at, effective_at[:10], currency, amount))
            else:
                inserts.append((cur_key, as_of, status, version, None, None, None, None, None, None, None))

    for entry_id, b, version, fp, op, merchant_id, token, effective_at, currency, amount in rows:
        if entry_id != cur_key:
            flush()
            cur_key = entry_id
            revs = []
        revs.append((b, version, fp, (op, merchant_id, token, effective_at, currency, amount)))
    flush()
    c.executemany(
        "INSERT INTO ledger_state(entry_id,as_of,status,version,op,merchant_id,customer_token,effective_at,eff_date,currency,amount_minor) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        inserts,
    )
    c.execute("DELETE FROM touched_ledger")
    return len(inserts)


def _rebuild_merchant_timelines(store: Store, batch_no: int, keys: set[tuple]) -> int:
    if not keys:
        return 0
    c = store.conn
    c.execute("CREATE TEMP TABLE IF NOT EXISTS touched_merchant(merchant_id TEXT, valid_from TEXT, PRIMARY KEY(merchant_id, valid_from))")
    c.execute("DELETE FROM touched_merchant")
    c.executemany("INSERT INTO touched_merchant VALUES(?,?)", keys)
    c.execute(
        "DELETE FROM merchant_state WHERE as_of>=? AND (merchant_id, valid_from) IN (SELECT merchant_id, valid_from FROM touched_merchant)",
        (batch_no,),
    )
    rows = c.execute(
        "SELECT r.merchant_id, r.valid_from, r.batch, r.version, r.fp, r.valid_to, r.region, r.tier FROM merchant_rev r "
        "JOIN touched_merchant t ON t.merchant_id=r.merchant_id AND t.valid_from=r.valid_from ORDER BY r.merchant_id, r.valid_from, r.batch"
    )
    inserts = []
    cur_key = None
    revs: list[tuple] = []

    def flush():
        if cur_key is None:
            return
        for as_of, status, version, payload in timeline(revs):
            if as_of < batch_no:
                continue
            if status == SELECTED:
                inserts.append((cur_key[0], cur_key[1], as_of, status, version, payload[0], payload[1], payload[2]))
            else:
                inserts.append((cur_key[0], cur_key[1], as_of, status, version, None, None, None))

    for merchant_id, valid_from, b, version, fp, valid_to, region, tier in rows:
        key = (merchant_id, valid_from)
        if key != cur_key:
            flush()
            cur_key = key
            revs = []
        revs.append((b, version, fp, (valid_to, region, tier)))
    flush()
    c.executemany("INSERT INTO merchant_state VALUES(?,?,?,?,?,?,?,?)", inserts)
    c.execute("DELETE FROM touched_merchant")
    return len(inserts)


def _rebuild_fx_timelines(store: Store, batch_no: int, keys: set[tuple]) -> int:
    if not keys:
        return 0
    c = store.conn
    c.execute("CREATE TEMP TABLE IF NOT EXISTS touched_fx(currency TEXT, date TEXT, PRIMARY KEY(currency, date))")
    c.execute("DELETE FROM touched_fx")
    c.executemany("INSERT INTO touched_fx VALUES(?,?)", keys)
    c.execute(
        "DELETE FROM fx_state WHERE as_of>=? AND (currency, date) IN (SELECT currency, date FROM touched_fx)",
        (batch_no,),
    )
    rows = c.execute(
        "SELECT r.currency, r.date, r.batch, r.version, r.fp, r.num, r.den FROM fx_rev r "
        "JOIN touched_fx t ON t.currency=r.currency AND t.date=r.date ORDER BY r.currency, r.date, r.batch"
    )
    inserts = []
    cur_key = None
    revs: list[tuple] = []

    def flush():
        if cur_key is None:
            return
        for as_of, status, version, payload in timeline(revs):
            if as_of < batch_no:
                continue
            if status == SELECTED:
                inserts.append((cur_key[0], cur_key[1], as_of, status, version, payload[0], payload[1]))
            else:
                inserts.append((cur_key[0], cur_key[1], as_of, status, version, None, None))

    for currency, date, b, version, fp, num, den in rows:
        key = (currency, date)
        if key != cur_key:
            flush()
            cur_key = key
            revs = []
        revs.append((b, version, fp, (num, den)))
    flush()
    c.executemany("INSERT INTO fx_state VALUES(?,?,?,?,?,?,?)", inserts)
    c.execute("DELETE FROM touched_fx")
    return len(inserts)


def publish(store: Store, batch_no: int, failpoint: str | None) -> dict:
    """Phase 2.  Reads ONLY from the staging tables, never from the input file."""
    c = store.conn
    t0 = time.monotonic()
    store.begin_write()
    try:
        if store.is_committed(batch_no):
            store.rollback()
            return {"skipped": True}
        meta = c.execute("SELECT file_sha256, source, row_count, stats FROM stage_batches WHERE batch=?", (batch_no,)).fetchone()
        if meta is None:
            raise RuntimeError(f"batch {batch_no} is not staged")
        sha, source, row_count, stats = meta

        ledger_rows, merchant_rows, fx_rows, quarantine_rows, erasure_rows = [], [], [], [], []
        entry_ids: set[str] = set()
        merchant_keys: set[tuple] = set()
        fx_keys: set[tuple] = set()
        for (rec_json,) in c.execute("SELECT rec FROM stage_rows WHERE batch=? ORDER BY seq", (batch_no,)):
            r = nz.rec_from_json(rec_json)
            if r.kind == nz.KIND_LEDGER:
                f = r.fields
                ledger_rows.append((batch_no, r.row_id, r.schema_version, r.key[0], r.version, r.fp, f["op"], f["merchant_id"], f["customer_token"], f["effective_at"], f["currency"], str(f["amount_minor"])))
                entry_ids.add(r.key[0])
            elif r.kind == nz.KIND_MERCHANT:
                f = r.fields
                merchant_rows.append((batch_no, r.row_id, r.key[0], r.key[1], r.version, r.fp, f["valid_to"], f["region"], f["tier"]))
                merchant_keys.add((r.key[0], r.key[1]))
            elif r.kind == nz.KIND_FX:
                f = r.fields
                fx_rows.append((batch_no, r.row_id, r.key[0], r.key[1], r.version, r.fp, str(f["num"]), str(f["den"])))
                fx_keys.add((r.key[0], r.key[1]))
            elif r.kind == nz.KIND_QUARANTINE:
                quarantine_rows.append((r.row_id, r.stream, None if r.schema_version is None else json.dumps(r.schema_version), batch_no))
            elif r.kind == nz.KIND_ERASURE:
                erasure_rows.append((r.key[0], batch_no, utcnow()))

        c.executemany("INSERT OR IGNORE INTO ledger_rev VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ledger_rows)
        c.executemany("INSERT OR IGNORE INTO merchant_rev VALUES(?,?,?,?,?,?,?,?,?)", merchant_rows)
        c.executemany("INSERT OR IGNORE INTO fx_rev VALUES(?,?,?,?,?,?,?,?)", fx_rows)
        c.executemany(
            "INSERT INTO quarantine(row_id,stream,schema_version,first_batch) VALUES(?,?,?,?) "
            "ON CONFLICT(row_id) DO UPDATE SET first_batch=MIN(first_batch, excluded.first_batch)",
            quarantine_rows,
        )
        c.executemany("INSERT OR IGNORE INTO erasures(customer_token,batch,committed_at) VALUES(?,?,?)", erasure_rows)

        n_ls = _rebuild_ledger_timelines(store, batch_no, entry_ids)
        n_ms = _rebuild_merchant_timelines(store, batch_no, merchant_keys)
        n_fs = _rebuild_fx_timelines(store, batch_no, fx_keys)

        c.execute(
            "INSERT INTO batches(batch,file_sha256,source,row_count,committed_at,stats) VALUES(?,?,?,?,?,?)",
            (batch_no, sha, source, row_count, utcnow(), stats),
        )
        c.execute("DELETE FROM stage_rows WHERE batch=?", (batch_no,))
        c.execute("DELETE FROM stage_batches WHERE batch=?", (batch_no,))
        detail = {
            "sha256": sha,
            "ledger_rev": len(ledger_rows),
            "merchant_rev": len(merchant_rows),
            "fx_rev": len(fx_rows),
            "quarantine": len(quarantine_rows),
            "erasures": len(erasure_rows),
            "timeline_points": {"ledger": n_ls, "merchant": n_ms, "fx": n_fs},
            "touched_keys": {"ledger": len(entry_ids), "merchant": len(merchant_keys), "fx": len(fx_keys)},
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
        store.log(batch_no, "publish", "committed", detail)

        if failpoint == "before-commit":
            # Every write of this transaction has been issued; the next
            # statement would be COMMIT.  Die here without it.
            _log(f"failpoint before-commit reached for batch {batch_no}; exiting 86 without COMMIT")
            sys.stderr.flush()
            os._exit(EXIT_FAILPOINT)

        store.commit()
        return detail
    except BaseException:
        store.rollback()
        raise


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_ingest(input_path: str, state_dir: str, failpoint: str | None) -> int:
    if failpoint is not None and failpoint not in FAILPOINTS:
        _log(f"unknown failpoint {failpoint!r}; expected one of {FAILPOINTS}")
        return EXIT_USAGE
    if not os.path.isfile(input_path):
        _log(f"input file not found: {input_path}")
        return EXIT_NOINPUT

    t0 = time.monotonic()
    try:
        batch_no, recs, counts = parse_file(input_path)
    except nz.InputError as e:
        _log(f"rejecting {os.path.basename(input_path)}: {e}")
        return EXIT_DATAERR
    sha = _sha256(input_path)
    source = os.path.basename(input_path)

    store = Store.open(state_dir, create=True)
    try:
        committed = store.conn.execute("SELECT file_sha256 FROM batches WHERE batch=?", (batch_no,)).fetchone()
        if committed is not None:
            if committed[0] != sha:
                _log(f"batch {batch_no} already committed from a file with a different sha256; retry ignored (out of scope: differing bytes for one batch)")
            else:
                _log(f"batch {batch_no} already committed; exact retry is a no-op")
            store.begin_write()
            store.log(batch_no, "ingest", "already-committed", {"sha256": sha, "same_bytes": committed[0] == sha})
            store.commit()
            return EXIT_OK

        staged = store.conn.execute("SELECT file_sha256 FROM stage_batches WHERE batch=?", (batch_no,)).fetchone()
        if staged is not None and staged[0] == sha:
            _log(f"batch {batch_no}: durable staging from a previous attempt matches; resuming at publish")
        else:
            stage(store, batch_no, recs, sha, source, counts)
            _log(f"batch {batch_no}: staged {len(recs)} records ({dict(counts)}) in {time.monotonic() - t0:.2f}s")

        if failpoint == "after-stage":
            _log(f"failpoint after-stage reached for batch {batch_no}; staging is durable, nothing published; exiting 86")
            sys.stderr.flush()
            os._exit(EXIT_FAILPOINT)

        # A separate, tiny committed marker so a later crash is explainable.
        store.begin_write()
        store.log(batch_no, "publish", "begin", {"sha256": sha, "failpoint": failpoint})
        store.commit()

        detail = publish(store, batch_no, failpoint)
        if detail.get("skipped"):
            _log(f"batch {batch_no} was committed concurrently; nothing to do")
        else:
            _log(f"batch {batch_no}: committed in {time.monotonic() - t0:.2f}s total; {detail['touched_keys']} keys touched")
        return EXIT_OK
    finally:
        store.close()
