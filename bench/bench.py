#!/usr/bin/env python3
"""Repeatable host measurement of ingest and snapshot against a generated scale fixture.

Measures, per ``./run`` invocation: wall time, peak RSS (ru_maxrss of the child),
and after ingest the on-disk state size.  Everything is a real subprocess of
the real adapter, so the numbers are what a reviewer's host would see.

    python3 bench/bench.py --work /tmp/recon-bench [--keys 200000] [--batches 12] [--order shuffle]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run")


def measured(cmd: list[str]) -> dict:
    """Run cmd in a child of a *fresh* helper process so ru_maxrss is that child's alone."""
    helper = (
        "import json,resource,subprocess,sys,time;"
        "t=time.monotonic();r=subprocess.run(sys.argv[1:],stderr=subprocess.PIPE,text=True);"
        "w=time.monotonic()-t;m=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss;"
        "print(json.dumps({'rc':r.returncode,'wall_s':round(w,3),'max_rss_mib':round(m/1024 if sys.platform!='darwin' else m/1048576,1),'stderr':r.stderr[-400:]}))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", helper, *cmd], capture_output=True, text=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def dir_size(path: str) -> int:
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def host_info() -> dict:
    info = {"platform": platform.platform(), "python": sys.version.split()[0], "cpu_count": os.cpu_count()}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    info["mem_total_gib"] = round(int(line.split()[1]) / 1048576, 1)
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    import sqlite3

    info["sqlite"] = sqlite3.sqlite_version
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--keys", type=int, default=200_000)
    ap.add_argument("--batches", type=int, default=12)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--order", choices=["sorted", "shuffle"], default="sorted")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    work = os.path.abspath(args.work)
    if os.path.exists(work):
        shutil.rmtree(work)
    data, state, out = (os.path.join(work, n) for n in ("data", "state", "out"))
    os.makedirs(out)
    report: dict = {"host": host_info(), "config": vars(args)}

    t = time.monotonic()
    gen = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "bench", "gen_scale.py"), "--out", data, "--keys", str(args.keys),
                          "--batches", str(args.batches), "--seed", str(args.seed)], capture_output=True, text=True, check=True)
    report["fixture"] = json.loads(gen.stdout)
    report["fixture"]["generate_s"] = round(time.monotonic() - t, 2)

    files = sorted(os.path.join(data, f) for f in os.listdir(data))
    if args.order == "shuffle":
        random.Random(args.seed).shuffle(files)
    ingest = []
    for f in files:
        m = measured([RUN, "ingest", "--input", f, "--state", state])
        if m["rc"] != 0:
            print("ingest failed:", m, file=sys.stderr)
            return 1
        m["file"] = os.path.basename(f)
        m["state_mib"] = round(dir_size(state) / 1048576, 1)
        ingest.append(m)
        print(f"ingest {m['file']}: {m['wall_s']:.2f}s  rss {m['max_rss_mib']} MiB  state {m['state_mib']} MiB", flush=True)
    report["ingest"] = {"per_batch": ingest, "total_wall_s": round(sum(m["wall_s"] for m in ingest), 2),
                        "peak_rss_mib": max(m["max_rss_mib"] for m in ingest), "final_state_mib": ingest[-1]["state_mib"]}
    # exact retry cost
    m = measured([RUN, "ingest", "--input", files[-1], "--state", state])
    report["ingest"]["exact_retry_wall_s"] = m["wall_s"]

    queries = {
        "month_full_cutoff": {"query_id": "month", "cutoff_batch": args.batches, "start_date": "2026-01-01", "end_date": "2026-02-01"},
        "month_mid_cutoff": {"query_id": "month-mid", "cutoff_batch": max(1, args.batches // 2), "start_date": "2026-01-01", "end_date": "2026-02-01"},
        "single_day": {"query_id": "day", "cutoff_batch": args.batches, "start_date": "2026-01-05", "end_date": "2026-01-06"},
        "empty_range": {"query_id": "empty", "cutoff_batch": args.batches, "start_date": "2027-01-01", "end_date": "2027-01-02"},
    }
    snaps = {}
    for name, q in queries.items():
        qp = os.path.join(work, f"{name}.json")
        with open(qp, "w") as f:
            json.dump(q, f)
        for engine in ("incremental", "full"):
            op = os.path.join(out, f"{name}-{engine}.json")
            m = measured([RUN, "snapshot", "--query", qp, "--state", state, "--out", op, "--engine", engine])
            if m["rc"] != 0:
                print("snapshot failed:", m, file=sys.stderr)
                return 1
            with open(op) as f:
                res = json.load(f)
            m.update({"groups": len(res["settlements"]), "trace": len(res["trace"]), "exceptions": len(res["exceptions"]),
                      "output_mib": round(os.path.getsize(op) / 1048576, 2)})
            snaps[f"{name}/{engine}"] = m
            print(f"snapshot {name:18s} {engine:12s}: {m['wall_s']:.2f}s  rss {m['max_rss_mib']} MiB  {m['trace']} trace rows", flush=True)
        with open(os.path.join(out, f"{name}-incremental.json")) as a, open(os.path.join(out, f"{name}-full.json")) as b:
            if json.load(a) != json.load(b):
                print(f"ENGINES DISAGREE on {name}", file=sys.stderr)
                return 1
    report["snapshot"] = snaps
    report["engines_agree"] = True
    report["targets"] = {
        "ingest_total_lt_180s": report["ingest"]["total_wall_s"] < 180,
        "snapshot_each_lt_30s": all(m["wall_s"] < 30 for m in snaps.values()),
        "peak_rss_lt_4gib": max(max(m["max_rss_mib"] for m in snaps.values()), report["ingest"]["peak_rss_mib"]) < 4096,
        "state_lt_5gib": report["ingest"]["final_state_mib"] < 5120,
    }
    with open(os.path.join(work, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(json.dumps({k: report[k] for k in ("host", "targets")}, indent=1))
    print(f"ingest total {report['ingest']['total_wall_s']}s, peak rss {report['ingest']['peak_rss_mib']} MiB, state {report['ingest']['final_state_mib']} MiB")
    print(f"report: {os.path.join(work, 'report.json')}")
    if not args.keep:
        shutil.rmtree(data, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
