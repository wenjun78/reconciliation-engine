"""Command-line adapter.  All operations go through ``./run``."""

from __future__ import annotations

import argparse
import sys

from . import __version__

EXIT_USAGE = 64
EXIT_SOFTWARE = 70


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run", description="Reconciliation engine")
    p.add_argument("--version", action="version", version=f"recon {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="stage and publish one batch file")
    s.add_argument("--input", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--failpoint", choices=["after-stage", "before-commit"], default=None)

    s = sub.add_parser("snapshot", help="answer a query at a knowledge cutoff")
    s.add_argument("--query", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--engine", choices=["incremental", "full"], default="incremental")

    s = sub.add_parser("status", help="show checkpoint, staging and storage facts")
    s.add_argument("--state", required=True)
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("explain", help="lineage of one ledger entry at a cutoff")
    s.add_argument("--state", required=True)
    s.add_argument("--entry", required=True)
    s.add_argument("--cutoff", type=int, default=None)

    s = sub.add_parser("check", help="integrity, PII scan and (optionally) differential verification")
    s.add_argument("--state", required=True)
    s.add_argument("--deep", action="store_true", help="compare incremental vs full engine at every committed cutoff")
    s.add_argument("--start", default="0000-01-01")
    s.add_argument("--end", default="9999-12-31")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "ingest":
            from .ingest import run_ingest

            return run_ingest(args.input, args.state, args.failpoint)
        if args.cmd == "snapshot":
            from .snapshot import run_snapshot

            return run_snapshot(args.query, args.state, args.out, args.engine)
        if args.cmd == "status":
            from .inspect import run_status

            return run_status(args.state, args.json)
        if args.cmd == "explain":
            from .inspect import run_explain

            return run_explain(args.state, args.entry, args.cutoff)
        if args.cmd == "check":
            from .inspect import run_check

            return run_check(args.state, args.deep, args.start, args.end)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - last-resort diagnostic without payload data
        print(f"recon: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_SOFTWARE


if __name__ == "__main__":
    sys.exit(main())
