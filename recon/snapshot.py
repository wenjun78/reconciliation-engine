"""Snapshot: answer a settlement query at a knowledge cutoff.

Two engines produce the same result object:

* ``incremental`` (default) reads the materialised knowledge-time timelines;
  cost is proportional to the entries in the requested date range plus the
  (small) merchant/FX dimension tables.
* ``full`` re-resolves every key from revision history for batch <= cutoff;
  it is the independent oracle used by ``check --deep`` and the test suite.

Both are pure functions of the committed state inside one WAL read
transaction, so a concurrent ingest cannot leak into a running snapshot.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from typing import Optional

from .money import to_usd_micro
from .resolve import CONFLICT, SELECTED, resolve_at
from .store import Store

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_DATAERR = 65
EXIT_CANTCREAT = 73


class QueryError(ValueError):
    pass


def _log(msg: str) -> None:
    print(f"recon snapshot: {msg}", file=sys.stderr, flush=True)


def load_query(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        q = json.load(f)
    if not isinstance(q, dict):
        raise QueryError("query must be a JSON object")
    for k in ("query_id", "cutoff_batch", "start_date", "end_date"):
        if k not in q:
            raise QueryError(f"query missing {k!r}")
    if not isinstance(q["query_id"], str):
        raise QueryError("query_id must be a string")
    cb = q["cutoff_batch"]
    if not isinstance(cb, int) or isinstance(cb, bool) or cb <= 0:
        raise QueryError("cutoff_batch must be a positive integer")
    for k in ("start_date", "end_date"):
        if not isinstance(q[k], str) or len(q[k]) != 10:
            raise QueryError(f"{k} must be YYYY-MM-DD")
    return q


# ---------------------------------------------------------------------------
# Dimension state at a cutoff
# ---------------------------------------------------------------------------

def _dims_incremental(store: Store, cutoff: int):
    c = store.conn
    merchants: dict[str, list] = defaultdict(list)
    exceptions: set[tuple] = set()
    last = None
    for mid, vf, as_of, status, version, vt, region, tier in c.execute(
        "SELECT merchant_id,valid_from,as_of,status,version,valid_to,region,tier FROM merchant_state WHERE as_of<=? ORDER BY merchant_id,valid_from,as_of",
        (cutoff,),
    ):
        key = (mid, vf)
        if last is not None and last[0] != key:
            _emit_merchant(last, merchants, exceptions)
        last = (key, status, version, vt, region, tier)
    if last is not None:
        _emit_merchant(last, merchants, exceptions)

    fx: dict[tuple, tuple] = {}
    last = None
    for cur, date, as_of, status, version, num, den in c.execute(
        "SELECT currency,date,as_of,status,version,num,den FROM fx_state WHERE as_of<=? ORDER BY currency,date,as_of",
        (cutoff,),
    ):
        key = (cur, date)
        if last is not None and last[0] != key:
            _emit_fx(last, fx, exceptions)
        last = (key, status, version, num, den)
    if last is not None:
        _emit_fx(last, fx, exceptions)
    return merchants, fx, exceptions


def _emit_merchant(last, merchants, exceptions):
    (mid, vf), status, version, vt, region, tier = last
    if status == SELECTED:
        merchants[mid].append((vf, vt, region, tier))
    else:
        exceptions.add(("merchants", f"{mid}|{vf}", "REVISION_CONFLICT", version))


def _emit_fx(last, fx, exceptions):
    (cur, date), status, version, num, den = last
    if status == SELECTED:
        fx[(cur, date)] = (int(num), int(den))
    else:
        exceptions.add(("fx", f"{cur}|{date}", "REVISION_CONFLICT", version))


def _dims_full(store: Store, cutoff: int):
    c = store.conn
    merchants: dict[str, list] = defaultdict(list)
    exceptions: set[tuple] = set()
    groups: dict[tuple, list] = defaultdict(list)
    for mid, vf, b, version, fp, vt, region, tier in c.execute(
        "SELECT merchant_id,valid_from,batch,version,fp,valid_to,region,tier FROM merchant_rev WHERE batch<=?", (cutoff,)
    ):
        groups[(mid, vf)].append((b, version, fp, (vt, region, tier)))
    for (mid, vf), revs in groups.items():
        status, version, payload = resolve_at(revs, cutoff)
        if status == SELECTED:
            merchants[mid].append((vf, payload[0], payload[1], payload[2]))
        else:
            exceptions.add(("merchants", f"{mid}|{vf}", "REVISION_CONFLICT", version))
    fx: dict[tuple, tuple] = {}
    groups = defaultdict(list)
    for cur, date, b, version, fp, num, den in c.execute(
        "SELECT currency,date,batch,version,fp,num,den FROM fx_rev WHERE batch<=?", (cutoff,)
    ):
        groups[(cur, date)].append((b, version, fp, (num, den)))
    for (cur, date), revs in groups.items():
        status, version, payload = resolve_at(revs, cutoff)
        if status == SELECTED:
            fx[(cur, date)] = (int(payload[0]), int(payload[1]))
        else:
            exceptions.add(("fx", f"{cur}|{date}", "REVISION_CONFLICT", version))
    return merchants, fx, exceptions


# ---------------------------------------------------------------------------
# Ledger selection at a cutoff
# ---------------------------------------------------------------------------

def _ledger_incremental(store: Store, cutoff: int, start: str, end: str):
    """Yield selected POST entries in range and conflict exceptions."""
    c = store.conn
    posts: list[tuple] = []
    conflicts: set[tuple] = set()
    # Candidates: any entry that had an in-range effective date at some point
    # <= cutoff.  The *latest* point decides; entries that moved out of range
    # are dropped below.
    rows = c.execute(
        "SELECT entry_id,as_of,status,version,op,merchant_id,customer_token,effective_at,eff_date,currency,amount_minor "
        "FROM ledger_state WHERE as_of<=? AND entry_id IN "
        "(SELECT DISTINCT entry_id FROM ledger_state WHERE as_of<=? AND eff_date>=? AND eff_date<?) "
        "ORDER BY entry_id, as_of",
        (cutoff, cutoff, start, end),
    )
    last = None
    for row in rows:
        if last is not None and last[0] != row[0]:
            _emit_entry(last, start, end, posts)
        last = row
    if last is not None:
        _emit_entry(last, start, end, posts)
    for entry_id, version in c.execute(
        "SELECT s.entry_id, s.version FROM ledger_state s WHERE s.status='CONFLICT' AND s.as_of<=? "
        "AND s.as_of=(SELECT MAX(t.as_of) FROM ledger_state t WHERE t.entry_id=s.entry_id AND t.as_of<=?)",
        (cutoff, cutoff),
    ):
        conflicts.add(("ledger", entry_id, "REVISION_CONFLICT", version))
    return posts, conflicts


def _emit_entry(row, start, end, posts):
    entry_id, _as_of, status, version, op, merchant_id, token, effective_at, eff_date, currency, amount = row
    if status == SELECTED and op == "POST" and start <= eff_date < end:
        posts.append((entry_id, version, merchant_id, token, effective_at, eff_date, currency, int(amount)))


def _ledger_full(store: Store, cutoff: int, start: str, end: str):
    c = store.conn
    posts: list[tuple] = []
    conflicts: set[tuple] = set()
    cur_key = None
    revs: list[tuple] = []

    def flush():
        if cur_key is None:
            return
        status, version, payload = resolve_at(revs, cutoff)
        if status == CONFLICT:
            conflicts.add(("ledger", cur_key, "REVISION_CONFLICT", version))
        else:
            op, merchant_id, token, effective_at, currency, amount = payload
            eff_date = effective_at[:10]
            if op == "POST" and start <= eff_date < end:
                posts.append((cur_key, version, merchant_id, token, effective_at, eff_date, currency, int(amount)))

    for entry_id, b, version, fp, op, merchant_id, token, effective_at, currency, amount in c.execute(
        "SELECT entry_id,batch,version,fp,op,merchant_id,customer_token,effective_at,currency,amount_minor FROM ledger_rev WHERE batch<=? ORDER BY entry_id,batch",
        (cutoff,),
    ):
        if entry_id != cur_key:
            flush()
            cur_key = entry_id
            revs = []
        revs.append((b, version, fp, (op, merchant_id, token, effective_at, currency, amount)))
    flush()
    return posts, conflicts


# ---------------------------------------------------------------------------
# Join, convert, aggregate
# ---------------------------------------------------------------------------

def compute(store: Store, query: dict, engine: str = "incremental") -> dict:
    cutoff = query["cutoff_batch"]
    start, end = query["start_date"], query["end_date"]
    c = store.conn

    erased = {r[0] for r in c.execute("SELECT customer_token FROM erasures")}
    if engine == "full":
        merchants, fx, exceptions = _dims_full(store, cutoff)
        posts, conflicts = _ledger_full(store, cutoff, start, end)
    else:
        merchants, fx, exceptions = _dims_incremental(store, cutoff)
        posts, conflicts = _ledger_incremental(store, cutoff, start, end)
    exceptions |= conflicts
    for row_id, stream in c.execute("SELECT row_id, stream FROM quarantine WHERE first_batch<=?", (cutoff,)):
        exceptions.add((stream, row_id, "UNSUPPORTED_SCHEMA", None))

    groups: dict[tuple, list] = defaultdict(lambda: [0, 0])
    trace: list[dict] = []
    for entry_id, version, merchant_id, token, effective_at, eff_date, currency, amount in posts:
        hits = [iv for iv in merchants.get(merchant_id, ()) if iv[0] <= effective_at < iv[1]]
        if not hits:
            exceptions.add(("ledger", entry_id, "MISSING_MERCHANT", version))
            continue
        if len(hits) > 1:
            exceptions.add(("ledger", entry_id, "AMBIGUOUS_MERCHANT", version))
            continue
        rate = fx.get((currency, eff_date))
        if rate is None:
            exceptions.add(("ledger", entry_id, "MISSING_FX", version))
            continue
        _vf, _vt, region, tier = hits[0]
        usd = to_usd_micro(amount, rate[0], rate[1])
        g = groups[(eff_date, merchant_id, region, tier)]
        g[0] += usd
        g[1] += 1
        trace.append(
            {
                "entry_id": entry_id,
                "version": version,
                "date": eff_date,
                "merchant_id": merchant_id,
                "region": region,
                "tier": tier,
                "usd_micro": usd,
                "customer_token": None if token in erased else token,
            }
        )

    settlements = [
        {"date": k[0], "merchant_id": k[1], "region": k[2], "tier": k[3], "net_usd_micro": v[0], "entry_count": v[1]}
        for k, v in sorted(groups.items())
    ]
    trace.sort(key=lambda t: t["entry_id"])
    exc_sorted = sorted(exceptions, key=lambda e: (e[0], e[1], e[2], -1 if e[3] is None else e[3]))
    return {
        "query_id": query["query_id"],
        "settlements": settlements,
        "trace": trace,
        "exceptions": [{"stream": s, "key": k, "code": code, "version": v} for s, k, code, v in exc_sorted],
    }


def write_atomic(out_path: str, result: dict) -> None:
    """Publish the result file atomically; an existing file is untouched on failure."""
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    data = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".recon-", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
        try:  # make the rename durable; directories cannot be opened on Windows
            dfd = os.open(out_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_snapshot(query_path: str, state_dir: str, out_path: str, engine: str = "incremental") -> int:
    try:
        query = load_query(query_path)
    except (OSError, ValueError) as e:
        _log(f"bad query: {e}")
        return EXIT_USAGE
    try:
        store = Store.open(state_dir, create=False)
    except FileNotFoundError:
        _log(f"no committed state under {state_dir}; cutoff_batch={query['cutoff_batch']} cannot be served (exit 65)")
        return EXIT_DATAERR
    try:
        store.begin_read()
        # Test hook: keep the read transaction open so a concurrent ingest can
        # commit meanwhile.  Demonstrates that a snapshot sees exactly the
        # committed state at the moment its read transaction began.
        hold = float(os.environ.get("RECON_SNAPSHOT_HOLD_SECONDS", "0") or 0)
        if hold > 0:
            _log(f"holding read transaction open for {hold}s (RECON_SNAPSHOT_HOLD_SECONDS)")
            time.sleep(hold)
        missing = store.missing_up_to(query["cutoff_batch"])
        if missing:
            shown = missing[:10]
            more = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
            _log(f"query {query['query_id']}: batches {shown}{more} not committed; refusing cutoff_batch={query['cutoff_batch']} (exit 65)")
            return EXIT_DATAERR
        result = compute(store, query, engine)
        store.rollback()  # end read transaction
    finally:
        store.close()
    try:
        write_atomic(out_path, result)
    except OSError as e:
        _log(f"cannot write output {out_path}: {e}")
        return EXIT_CANTCREAT
    _log(
        f"query {query['query_id']} @cutoff {query['cutoff_batch']} [{query['start_date']},{query['end_date']}): "
        f"{len(result['settlements'])} groups, {len(result['trace'])} entries, {len(result['exceptions'])} exceptions ({engine})"
    )
    return EXIT_OK
