"""Adapter contract: ./run from any cwd, absolute paths only, exit codes, output shape, no cwd/state side effects."""

import json
import os
import stat
import subprocess
import unittest

from helpers import ROOT, RUN, EngineCase, fx, ledger, merchant, query, requires_posix, write_batch, write_query

DIM = [merchant("M1"), fx("USD", "2026-01-01", 1, 1)]


@requires_posix
class Adapter(EngineCase):
    def test_run_is_executable_posix_sh(self):
        self.assertTrue(os.stat(RUN).st_mode & stat.S_IXUSR)
        with open(RUN) as f:
            self.assertTrue(f.readline().startswith("#!/bin/sh"))

    def test_works_from_unrelated_cwd_with_absolute_paths(self):
        path = write_batch(self.inbox, 1, DIM + [ledger("A", 1, 1)])
        r = subprocess.run([RUN, "ingest", "--input", path, "--state", self.state], cwd="/", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        q = write_query(self.inbox, query("x", 1))
        out = os.path.join(self.outdir, "x.json")
        r = subprocess.run([RUN, "snapshot", "--query", q, "--state", self.state, "--out", out], cwd="/", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")  # stdout stays clean; diagnostics on stderr
        with open(out) as f:
            doc = json.load(f)
        self.assertEqual(set(doc), {"query_id", "settlements", "trace", "exceptions"})
        self.assertIsInstance(doc["trace"][0]["usd_micro"], int)
        self.assertEqual(doc["query_id"], "x")

    def test_snapshot_does_not_touch_input_files_or_cwd(self):
        path = write_batch(self.inbox, 1, DIM + [ledger("A", 1, 1)])
        self.assertEqual(subprocess.run([RUN, "ingest", "--input", path, "--state", self.state], capture_output=True).returncode, 0)
        os.unlink(path)  # inputs gone: snapshot must still work from --state alone
        q = write_query(self.inbox, query("y", 1))
        out = os.path.join(self.outdir, "y.json")
        empty_cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(empty_cwd)
        r = subprocess.run([RUN, "snapshot", "--query", q, "--state", self.state, "--out", out], cwd=empty_cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(empty_cwd), [])

    def test_exit_codes(self):
        r = self.run_cli("ingest", "--input", os.path.join(self.tmp, "missing.jsonl"), "--state", self.state)
        self.assertEqual(r.returncode, 66)
        bad = os.path.join(self.inbox, "bad.json")
        with open(bad, "w") as f:
            json.dump({"query_id": "b", "cutoff_batch": "1", "start_date": "2026-01-01", "end_date": "2026-01-02"}, f)
        r = self.run_cli("snapshot", "--query", bad, "--state", self.state, "--out", os.path.join(self.outdir, "b.json"))
        self.assertEqual(r.returncode, 64)
        r = self.run_cli("ingest", "--input", bad, "--state", self.state, "--failpoint", "nope")
        self.assertEqual(r.returncode, 2)  # argparse usage error
        empty = os.path.join(self.inbox, "empty.jsonl")
        open(empty, "w").close()
        r = self.run_cli("ingest", "--input", empty, "--state", self.state)
        self.assertEqual(r.returncode, 65)
        mixed = os.path.join(self.inbox, "mixed.jsonl")
        with open(mixed, "w") as f:
            for b in (1, 2):
                f.write(json.dumps(dict(ledger("A", 1, 1), batch=b)) + "\n")
        r = self.run_cli("ingest", "--input", mixed, "--state", self.state)
        self.assertEqual(r.returncode, 65)
        self.assertFalse(os.path.exists(os.path.join(self.state, "engine.sqlite3")))  # rejected before any state write

    def test_runs_without_write_access_to_app_dir_as_non_root(self):
        """Simulates the container contract: read-only /app, writable state, arbitrary UID."""
        import shutil

        app = os.path.join(self.tmp, "app")
        shutil.copytree(ROOT, app, ignore=shutil.ignore_patterns("fixtures", "tests", "bench", ".git", "__pycache__", "*.pyc", "ai_logs"))
        for root, dirs, files in os.walk(app):
            for n in dirs + files:
                p = os.path.join(root, n)
                os.chmod(p, (os.stat(p).st_mode | 0o444) & ~0o222 | (0o111 if os.path.isdir(p) or n in ("run", "recon-cli.py") else 0))
        os.chmod(app, 0o555)
        path = write_batch(self.inbox, 1, DIM + [ledger("A", 1, 1)])
        os.chmod(self.tmp, 0o777)
        os.chmod(self.inbox, 0o777)
        os.chmod(self.outdir, 0o777)
        os.chmod(path, 0o644)
        os.makedirs(self.state, exist_ok=True)
        os.chmod(self.state, 0o777)
        prefix = [os.path.join(app, "run")]
        if os.geteuid() == 0 and shutil.which("setpriv"):
            prefix = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", *prefix]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}
        r = subprocess.run(prefix + ["ingest", "--input", path, "--state", self.state], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        q = write_query(self.inbox, query("ro", 1))
        os.chmod(q, 0o644)
        out = os.path.join(self.outdir, "ro.json")
        r = subprocess.run(prefix + ["snapshot", "--query", q, "--state", self.state, "--out", out], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        # no bytecode or anything else written into the app tree
        for root, _dirs, files in os.walk(app):
            self.assertNotIn("__pycache__", root)
            for n in files:
                self.assertFalse(n.endswith(".pyc"), n)


if __name__ == "__main__":
    unittest.main()
