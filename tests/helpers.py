"""Shared fixtures: build batch files from dicts, drive the engine in-process or via ./run."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name == "posix"
requires_posix = unittest.skipUnless(POSIX, "drives the POSIX sh adapter ./run (or uses signals); run on Linux/macOS or in CI")
RUN = os.path.join(ROOT, "run")
FIXTURES = os.path.join(ROOT, "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from recon.ingest import run_ingest  # noqa: E402
from recon.snapshot import compute, run_snapshot  # noqa: E402
from recon.store import Store  # noqa: E402

_seq = [0]


def quiet_ingest(path, state, failpoint=None):
    with contextlib.redirect_stderr(io.StringIO()):
        return run_ingest(path, state, failpoint)


def quiet_snapshot(query_path, state, out, engine="incremental"):
    with contextlib.redirect_stderr(io.StringIO()):
        return run_snapshot(query_path, state, out, engine)



def _rid() -> str:
    _seq[0] += 1
    return f"t_{_seq[0]:08d}"


def ledger(entry_id, version, amount, *, op="POST", merchant="M1", token="C1", at="2026-01-01T12:00:00Z",
           cur="USD", sv=1, row_id=None, extra=None):
    p = {"entry_id": entry_id, "version": version, "op": op, "merchant_id": merchant, "customer_token": token,
         "effective_at": at, "currency": cur}
    if sv == 2:
        p["amount_minor_text"] = str(amount)
    else:
        p["amount_minor"] = amount
    if extra:
        p.update(extra)
    return {"row_id": row_id or _rid(), "stream": "ledger", "schema_version": sv, "payload": p}


def merchant(mid, vf="2026-01-01T00:00:00Z", vt="2026-02-01T00:00:00Z", version=1, region="EU", tier="standard", row_id=None):
    return {"row_id": row_id or _rid(), "stream": "merchants", "schema_version": 1,
            "payload": {"merchant_id": mid, "valid_from": vf, "valid_to": vt, "version": version, "region": region, "tier": tier}}


def fx(cur, date, num, den, version=1, row_id=None):
    return {"row_id": row_id or _rid(), "stream": "fx", "schema_version": 1,
            "payload": {"currency": cur, "date": date, "version": version, "usd_micro_num": num, "usd_micro_den": den}}


def profile(token, name, email, row_id=None):
    return {"row_id": row_id or _rid(), "stream": "profiles", "schema_version": 1,
            "payload": {"customer_token": token, "name": name, "email": email}}


def erasure(token, row_id=None):
    return {"row_id": row_id or _rid(), "stream": "erasures", "schema_version": 1, "payload": {"customer_token": token}}


def raw(stream, schema_version, payload, row_id=None):
    return {"row_id": row_id or _rid(), "stream": stream, "schema_version": schema_version, "payload": payload}


def write_batch(directory: str, batch: int, rows: list[dict], name: str | None = None) -> str:
    path = os.path.join(directory, name or f"batch_{batch:02d}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            obj = dict(r)
            obj["batch"] = batch
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    return path


def query(qid="q", cutoff=1, start="2026-01-01", end="2026-02-01") -> dict:
    return {"query_id": qid, "cutoff_batch": cutoff, "start_date": start, "end_date": end}


def write_query(directory: str, q: dict, name: str | None = None) -> str:
    path = os.path.join(directory, name or f"{q['query_id']}.json")
    with open(path, "w") as f:
        json.dump(q, f)
    return path


class EngineCase(unittest.TestCase):
    """Base class with a temp workspace and in-process/subprocess drivers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recon-test-")
        self.state = os.path.join(self.tmp, "state")
        self.inbox = os.path.join(self.tmp, "in")
        self.outdir = os.path.join(self.tmp, "out")
        os.makedirs(self.inbox)
        os.makedirs(self.outdir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # in-process (fast)
    def ingest(self, batch: int, rows: list[dict], failpoint=None) -> int:
        path = write_batch(self.inbox, batch, rows)
        with contextlib.redirect_stderr(io.StringIO()):
            return run_ingest(path, self.state, failpoint)

    def snapshot(self, cutoff: int, start="2026-01-01", end="2026-02-01", qid="q", engine="incremental"):
        q = query(qid, cutoff, start, end)
        out = os.path.join(self.outdir, f"{qid}-{cutoff}-{engine}.json")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run_snapshot(write_query(self.inbox, q, f"{qid}-{cutoff}.json"), self.state, out, engine)
        if rc != 0:
            return rc, None
        with open(out) as f:
            return rc, json.load(f)

    def both(self, cutoff: int, **kw):
        rc1, a = self.snapshot(cutoff, engine="incremental", **kw)
        rc2, b = self.snapshot(cutoff, engine="full", **kw)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(a, b, "incremental and full engines disagree")
        return a

    # subprocess (for failpoints / adapter semantics)
    def run_cli(self, *args, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([RUN, *args], capture_output=True, text=True, env=env)

    def cli_ingest(self, batch: int, rows: list[dict], failpoint=None) -> subprocess.CompletedProcess:
        path = write_batch(self.inbox, batch, rows)
        args = ["ingest", "--input", path, "--state", self.state]
        if failpoint:
            args += ["--failpoint", failpoint]
        return self.run_cli(*args)

    def cli_snapshot(self, cutoff: int, start="2026-01-01", end="2026-02-01", qid="q", out=None):
        q = query(qid, cutoff, start, end)
        out = out or os.path.join(self.outdir, f"{qid}-{cutoff}.json")
        r = self.run_cli("snapshot", "--query", write_query(self.inbox, q, f"{qid}-{cutoff}.json"), "--state", self.state, "--out", out)
        data = None
        if r.returncode == 0:
            with open(out) as f:
                data = json.load(f)
        return r, data, out

    def db(self):
        return Store.open(self.state, create=False)


def trace_map(result: dict) -> dict:
    return {t["entry_id"]: t for t in result["trace"]}


def exc_set(result: dict) -> set:
    return {(e["stream"], e["key"], e["code"], e["version"]) for e in result["exceptions"]}


__all__ = [
    "ROOT", "RUN", "FIXTURES", "ledger", "merchant", "fx", "profile", "erasure", "raw", "write_batch", "query",
    "write_query", "EngineCase", "trace_map", "exc_set", "compute", "run_ingest", "run_snapshot", "quiet_ingest", "quiet_snapshot",
    "POSIX", "requires_posix",
]
