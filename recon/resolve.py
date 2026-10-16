"""Revision selection: highest business version wins, ties on different
payloads block the key.

The same pure function serves two callers:

* ``ingest`` rebuilds the knowledge-time *timeline* of every key touched by a
  batch (incremental derived state);
* ``snapshot --engine full`` resolves straight from revision history as an
  independent cross-check of the incremental tables.

A revision tuple is ``(batch, version, fp, payload)``.  A timeline is a list of
``(as_of, status, version, payload)`` change points in ascending batch order;
``payload`` is ``None`` for a CONFLICT point.
"""

from __future__ import annotations

from itertools import groupby
from typing import Iterable, Optional

SELECTED = "SELECTED"
CONFLICT = "CONFLICT"


def timeline(revs: Iterable[tuple]) -> list[tuple]:
    """Knowledge-time resolution change points for one business key.

    At each distinct batch (in ascending order) all revisions with batch <=
    that point are considered: the highest version wins; if two or more
    distinct fingerprints share that highest version the key is CONFLICT.
    Only points where the resolution differs from the previous point are
    emitted, so "resolution at cutoff B" is "last point with as_of <= B".
    """
    ordered = sorted(revs, key=lambda r: r[0])
    max_version = 0
    variants: dict[str, object] = {}
    out: list[tuple] = []
    prev: Optional[tuple] = None
    for batch, group in groupby(ordered, key=lambda r: r[0]):
        for _b, version, fp, payload in group:
            if version > max_version:
                max_version = version
                variants = {fp: payload}
            elif version == max_version and fp not in variants:
                variants[fp] = payload
        if len(variants) == 1:
            cur = (SELECTED, max_version, next(iter(variants.values())))
        else:
            cur = (CONFLICT, max_version, None)
        if cur != prev:
            out.append((batch, cur[0], cur[1], cur[2]))
            prev = cur
    return out


def resolve_at(revs: Iterable[tuple], cutoff: int) -> Optional[tuple]:
    """Resolution ``(status, version, payload)`` considering revisions with batch <= cutoff."""
    pts = timeline(r for r in revs if r[0] <= cutoff)
    if not pts:
        return None
    _as_of, status, version, payload = pts[-1]
    return status, version, payload


def latest_at(points: Iterable[tuple], cutoff: int) -> Optional[tuple]:
    """Pick the last timeline point with as_of <= cutoff (points ascending)."""
    best = None
    for p in points:
        if p[0] <= cutoff:
            best = p
        else:
            break
    return best
