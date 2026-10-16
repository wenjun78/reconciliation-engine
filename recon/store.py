"""SQLite state store: schema, connection policy and transaction helpers.

One file (``engine.sqlite3`` under ``--state``) holds everything durable.
WAL journaling gives every reader a stable snapshot as of the moment its read
transaction starts, which is exactly the isolation the specification asks for
("a concurrently running snapshot uses the committed state visible when its
read transaction began").
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_SCHEMA_VERSION = 1
DB_NAME = "engine.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Processed-batch checkpoint.  Presence of a row == the batch is visible.
CREATE TABLE IF NOT EXISTS batches(
  batch INTEGER PRIMARY KEY,
  file_sha256 TEXT NOT NULL,
  source TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  committed_at TEXT NOT NULL,
  stats TEXT NOT NULL
);

-- Durable staging area (phase 1 of ingest).  PII-free normalised records.
CREATE TABLE IF NOT EXISTS stage_batches(
  batch INTEGER PRIMARY KEY,
  file_sha256 TEXT NOT NULL,
  source TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  staged_at TEXT NOT NULL,
  stats TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_rows(
  batch INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  rec TEXT NOT NULL,
  PRIMARY KEY(batch, seq)
) WITHOUT ROWID;

-- Revision history (append-only, keyed by batch+row_id so exact redeliveries
-- in later batches are retained as lineage but collapse in resolution).
CREATE TABLE IF NOT EXISTS ledger_rev(
  batch INTEGER NOT NULL,
  row_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  entry_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  fp TEXT NOT NULL,
  op TEXT NOT NULL,
  merchant_id TEXT NOT NULL,
  customer_token TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount_minor TEXT NOT NULL,
  PRIMARY KEY(batch, row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ledger_rev_key ON ledger_rev(entry_id, batch);

CREATE TABLE IF NOT EXISTS merchant_rev(
  batch INTEGER NOT NULL,
  row_id TEXT NOT NULL,
  merchant_id TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  version INTEGER NOT NULL,
  fp TEXT NOT NULL,
  valid_to TEXT NOT NULL,
  region TEXT NOT NULL,
  tier TEXT NOT NULL,
  PRIMARY KEY(batch, row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS merchant_rev_key ON merchant_rev(merchant_id, valid_from, batch);

CREATE TABLE IF NOT EXISTS fx_rev(
  batch INTEGER NOT NULL,
  row_id TEXT NOT NULL,
  currency TEXT NOT NULL,
  date TEXT NOT NULL,
  version INTEGER NOT NULL,
  fp TEXT NOT NULL,
  num TEXT NOT NULL,
  den TEXT NOT NULL,
  PRIMARY KEY(batch, row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS fx_rev_key ON fx_rev(currency, date, batch);

CREATE TABLE IF NOT EXISTS quarantine(
  row_id TEXT PRIMARY KEY,
  stream TEXT NOT NULL,
  schema_version TEXT,
  first_batch INTEGER NOT NULL
) WITHOUT ROWID;

-- Tombstones: opaque token only.  Masking ignores batch numbers by design.
CREATE TABLE IF NOT EXISTS erasures(
  customer_token TEXT PRIMARY KEY,
  batch INTEGER NOT NULL,
  committed_at TEXT NOT NULL
) WITHOUT ROWID;

-- Knowledge-time resolution timelines (incremental derived state).
-- One row per (key, as_of batch) at which the key's resolution CHANGED.
-- The resolution at cutoff B is the row with the greatest as_of <= B.
CREATE TABLE IF NOT EXISTS ledger_state(
  entry_id TEXT NOT NULL,
  as_of INTEGER NOT NULL,
  status TEXT NOT NULL,            -- SELECTED | CONFLICT
  version INTEGER NOT NULL,
  op TEXT,
  merchant_id TEXT,
  customer_token TEXT,
  effective_at TEXT,
  eff_date TEXT,
  currency TEXT,
  amount_minor TEXT,
  PRIMARY KEY(entry_id, as_of)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ledger_state_date ON ledger_state(eff_date, as_of);
CREATE INDEX IF NOT EXISTS ledger_state_conflict ON ledger_state(as_of) WHERE status='CONFLICT';

CREATE TABLE IF NOT EXISTS merchant_state(
  merchant_id TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  as_of INTEGER NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  valid_to TEXT,
  region TEXT,
  tier TEXT,
  PRIMARY KEY(merchant_id, valid_from, as_of)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS fx_state(
  currency TEXT NOT NULL,
  date TEXT NOT NULL,
  as_of INTEGER NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  num TEXT,
  den TEXT,
  PRIMARY KEY(currency, date, as_of)
) WITHOUT ROWID;

-- Inspectable transaction journal (what happened, when, with which counts).
CREATE TABLE IF NOT EXISTS txn_log(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  batch INTEGER,
  phase TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def db_path(state_dir: str) -> Path:
    return Path(state_dir) / DB_NAME


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path

    @classmethod
    def open(cls, state_dir: str, create: bool) -> "Store":
        p = db_path(state_dir)
        if not p.exists():
            if not create:
                raise FileNotFoundError(str(p))
            os.makedirs(state_dir, exist_ok=True)
        conn = sqlite3.connect(str(p), isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            print(f"warn: journal_mode is {mode}, expected WAL", file=sys.stderr)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA foreign_keys=ON")
        st = cls(conn, p)
        if create:
            st._init_schema()
        return st

    def _init_schema(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript() would issue an implicit COMMIT first; run the
            # statements one by one so schema creation is itself atomic.
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            cur = self.conn.execute("SELECT value FROM meta WHERE key='state_schema_version'")
            row = cur.fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('state_schema_version',?),('created_at',?)",
                    (str(STATE_SCHEMA_VERSION), utcnow()),
                )
            elif int(row[0]) != STATE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"state schema version {row[0]} is not supported by this engine ({STATE_SCHEMA_VERSION}); rebuild from inputs"
                )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # -- transactions ----------------------------------------------------
    def begin_write(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def begin_read(self) -> None:
        # Deferred BEGIN + a trivial read pins the WAL snapshot for the whole
        # read transaction.
        self.conn.execute("BEGIN")
        self.conn.execute("SELECT 1 FROM meta LIMIT 1").fetchall()

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def close(self) -> None:
        self.conn.close()

    # -- checkpoint helpers ---------------------------------------------
    def committed_batches(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT batch FROM batches ORDER BY batch")]

    def is_committed(self, batch: int) -> bool:
        return self.conn.execute("SELECT 1 FROM batches WHERE batch=?", (batch,)).fetchone() is not None

    def missing_up_to(self, cutoff: int) -> list[int]:
        have = {r[0] for r in self.conn.execute("SELECT batch FROM batches WHERE batch<=?", (cutoff,))}
        return [b for b in range(1, cutoff + 1) if b not in have]

    def log(self, batch, phase: str, outcome: str, detail: dict) -> None:
        import json

        self.conn.execute(
            "INSERT INTO txn_log(at,batch,phase,outcome,detail) VALUES(?,?,?,?,?)",
            (utcnow(), batch, phase, outcome, json.dumps(detail, sort_keys=True)),
        )
