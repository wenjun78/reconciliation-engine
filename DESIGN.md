# DESIGN

## 1. Shape of the solution

A single-process Python 3.12 + SQLite engine (standard library only). All durable state is one database, `engine.sqlite3`, under `--state`. Ingest is two-phase (durable staging, then one atomic publish transaction). Snapshots run inside one WAL read transaction and compute from *materialised knowledge-time timelines* rather than from raw revision rows; an independent full-recompute path over the raw history exists purely to cross-check the incremental tables.

Guiding idea: **"what did we know at batch B?" is a point lookup on a per-key timeline, not a scan of everything that ever arrived.**

## 2. Data model

| Table | Role | Key |
|---|---|---|
| `batches` | processed-batch checkpoint; a row means "visible" | `batch` |
| `stage_batches`, `stage_rows` | durable, PII-free staging for the batch being ingested | `(batch, seq)` |
| `ledger_rev`, `merchant_rev`, `fx_rev` | append-only revision history, exactly as normalised | `(batch, row_id)` |
| `ledger_state`, `merchant_state`, `fx_state` | knowledge-time timelines: one row per `(business key, as_of batch)` at which the key's *resolution changed* | `(key…, as_of)` |
| `quarantine` | `UNSUPPORTED_SCHEMA` rows, first batch seen | `row_id` |
| `erasures` | tombstones: opaque token, batch, time | `customer_token` |
| `txn_log` | inspectable journal of every stage/publish/no-op with counts and durations | `id` |

Money is stored as decimal text and handled as Python `int`; amounts above 2^63 round-trip untouched and nothing is ever a float.

Normalisation happens once, on ingest. Ledger v2 `amount_minor_text` is parsed with a strict `^[+-]?[0-9]+$` grammar into the same integer the v1 payload carries, and a 128-bit fingerprint of the *declared* business fields (never `row_id`, batch or schema version) is computed. v1/v2 copies of one revision therefore collapse to one fingerprint, and extra payload fields are invisible.

Profiles are reduced to a count at parse time. Name and email never reach the staging table, the revision tables, a log line or a temp file; there is nothing to erase later.

## 3. Invariants

1. **Visibility ⇔ checkpoint.** A batch's rows, its timeline updates, its erasures and its `batches` row are written in one `BEGIN IMMEDIATE … COMMIT`. Either all are visible or none.
2. **Timeline correctness.** For every key and every committed cutoff B with `1..B` contiguous, the last timeline point with `as_of ≤ B` equals the resolution computed from scratch over revisions with `batch ≤ B`. `check --deep` asserts this at every cutoff; the differential tests assert it under random data and random delivery orders.
3. **Prefix stability.** Ingesting batch N only deletes and rewrites timeline points with `as_of ≥ N`, and only for keys that batch N touches. Points with `as_of < N` are mathematically unaffected by N (the resolution at B depends only on revisions ≤ B), so earlier cutoffs cannot change.
4. **Masking is global.** Erasure checks use *all* committed tombstones regardless of cutoff. Only the token is masked; amounts, versions and lineage are untouched.
5. **No PII in managed storage.** Enforced structurally (never stored) and verified by `check`, which scans every byte of `--state` for email-shaped strings (expected 0).
6. **Snapshot is a pure function** of `(committed state at read start, query)`. It never reads inputs, the network or the cwd.

## 4. State transitions of a batch

```
unseen ──stage──▶ staged ──publish──▶ committed
   │   (phase 1,            (phase 2, one txn,
   │    own txn)             COMMIT = visibility)
   └──── exact retry of a committed batch: no-op, logged
```

* `--failpoint after-stage`: exits 86 after phase 1 committed. Staging is durable; nothing is visible; snapshots at that cutoff exit 65. A replay recognises the staged copy by file SHA-256 and resumes at publish.
* `--failpoint before-commit`: exits 86 after every write of phase 2 has been issued and immediately before `COMMIT`. The process dies with the transaction open; SQLite discards it on the next open. Previous committed state remains visible.

The two failpoints are distinct boundaries by design: durable staging (phase 1 commit) and visibility (phase 2 commit). Neither is a fake early exit: `after-stage` only fires after the staging transaction has been fsynced, and `before-commit` only after the full publish has been written into the open transaction. Both fire only on a batch not yet in `batches`.

Publication **reads only from staging**, never from the input file, so what was durably staged is exactly what becomes visible.

## 5. Transaction boundary and isolation

SQLite in WAL mode with `synchronous=FULL`. Writers take `BEGIN IMMEDIATE`; a snapshot takes a deferred `BEGIN` plus an initial read, which pins the WAL snapshot. From then on it sees exactly the committed state at that instant: a concurrent ingest can commit (readers never block writers in WAL) without the running snapshot observing any of it. A later snapshot sees it. This is precisely the erasure rule in the specification ("every snapshot whose read transaction begins after that commit"), and the `ReaderIsolation` test demonstrates it with a held read transaction.

Output publication is atomic too: the result is written to a temp file in the output directory, fsynced, then `os.replace`d. A snapshot that fails (gap, bad query, I/O) never touches an existing `--out` file.

## 6. Time semantics

* **Effective time** is the ledger entry's `effective_at`; its UTC date selects the FX row and the settlement day. Merchant validity is half-open `[valid_from, valid_to)` compared as ISO strings.
* **Knowledge time** is the batch number. A query with `cutoff_batch=B` resolves every key at B and requires `1..B` committed (else exit 65). Later batches cannot change answers at B (invariant 3).
* **Erasure** is the one deliberate exception: it is knowledge-time *independent* (invariant 4).

Revision selection: per key, highest `version` wins; two or more distinct fingerprints at that version block the key (`REVISION_CONFLICT`, reported at that version only); any higher unambiguous version resolves it; re-seeing a lower version never restores anything. Join order for selected in-range POSTs is merchant (0 → `MISSING_MERCHANT`, >1 → `AMBIGUOUS_MERCHANT`) then FX (`MISSING_FX`), first error only. Blocked merchant/FX keys are simply absent for joins and reported once each, whether or not any entry references them.

Rounding: `amount × num / den`, rounded per entry to nearest, ties to even, symmetric for negatives, by integer `divmod` on the exact product. Sums are of rounded integers; nothing is rounded after aggregation.

## 7. Lineage

Every revision row keeps `(batch, row_id, schema_version)`. `./run explain --entry E --cutoff B` prints the full lineage: each revision with its fingerprint and whether it was in scope at B, the recomputed and the materialised timeline (and whether they agree), the winning merchant interval and FX rows with their `row_id`s, the exact rational product and the rounded result. `txn_log` records what each ingest did, including the `begin` marker written before a publish attempt so a crash is explainable afterwards. Erased tokens are masked in `explain` output as well.

## 8. Incremental cost model

Publishing batch N costs O(rows in N + revisions of keys touched by N). Timelines are rebuilt only for touched keys and only for points `as_of ≥ N`, so an out-of-order batch 3 arriving after batch 7 rewrites just the keys batch 3 mentions.

A snapshot costs O(timeline rows of entries that were ever in the date range at some point ≤ B) + O(merchant/FX dimension) + O(conflicts). A one-day query over a 30-day, 200k-entry state touches ~1/30 of the entries; the measured single-day snapshot is ~10× faster than full recomputation (EVIDENCE §5). For a full-range query both engines are bounded by output size (~190k trace rows, 40 MB JSON); the incremental engine is still faster but the gain is small because serialisation dominates.

Full recomputation stays available as `--engine full`; `check --deep` compares the two at every cutoff.

## 9. Alternatives considered

* **Single-transaction ingest (one boundary).** Simpler, but then `after-stage` would have nothing durable to point at. Two phases make both failpoints truthful and give a resumable staging area.
* **Per-cutoff aggregate caches** (settlement sums per day/merchant/as_of). Rejected: with reorderable batches and merchant/FX corrections that fan out to many entries, invalidation logic would dominate the code for a gain measured in hundreds of milliseconds at the scored scale; the entry-level timeline already removes the expensive part (revision churn).
* **Resolution per `(key, batch)` for every batch**, not just change points: simpler query, up to 12× the rows; change points keep 200k keys at 71 MB.
* **PostgreSQL.** Nicer concurrency, but adds a root-requiring service to a read-only container for nothing the targets need.
* **Key-destruction erasure.** Unnecessary: the opaque token is a permitted tombstone and names/emails are never stored.

## 10. Limits

* Concurrent writers are out of scope; two ingests would serialise on SQLite's write lock and the second would see the first's checkpoint, but this is untested.
* Full-range snapshots at the scored scale are output-bound (~2 s for 40 MB); 10× more data would need streaming JSON output.
* Timeline rebuild holds all revisions of the touched keys in memory per batch; a key with millions of revisions would be the first hot spot.
* FX payload equality is exact on `(num, den)`; `2/4` and `1/2` are treated as different payloads, as the specification's "compare only the declared fields" reads literally.

## 11. One billion events per day (design note, not implemented)

**Assumptions.** 1e9 ledger events/day ≈ 11.6k/s average, 50k/s peak; 30-day late-arrival window; corrections must be visible within 15 minutes; 99.9 % query availability; daily close per merchant/day; knowledge time becomes a monotonically increasing commit epoch issued by the ingestion coordinator rather than a file batch number.

**Architecture.** Ingest via Kafka partitioned by `hash(entry_id)` so all revisions of one key are ordered on one partition. A stream processor (Flink) keeps per-key state = the timeline change points, exactly as here; it emits a *resolution delta* `(entry_id, epoch, status, payload)` whenever the resolution changes. Deltas land in an Iceberg table partitioned by `effective_date` and clustered by `merchant_id`, with the epoch as a column; snapshot-at-epoch is a time-travel read plus "latest delta ≤ epoch per key". Merchant/FX are small dimensions kept as versioned tables broadcast to all workers.

**Skew.** Hot merchants (30 % of volume here) are handled by partitioning on `entry_id`, not merchant, during resolution; aggregation partitions on `(merchant_id, effective_date, salt)` with a two-stage sum so a hot merchant/day spreads over N tasks before the final merge.

**Compaction.** Deltas accumulate ~2× the event rate (events plus revisions). Hourly compaction rewrites each `effective_date` partition to one row per key for epochs older than the 15-minute correction SLA, keeping the full delta history only for the 30-day window; after close, a partition is sealed and its `settlements` materialisation becomes immutable except through a new epoch that supersedes it (never in place).

**Consistency.** Query serving reads a published epoch pointer; an epoch becomes readable only after every partition's delta for it is committed (two-phase: write deltas, then publish the pointer). That is the same "checkpoint row = visibility" rule as this repository, scaled out. Erasure is a global tombstone table joined at read time, independent of epoch, so historical reproduction of identity is overridden exactly as specified.

**Costs (order of magnitude).** Kafka: 3 brokers, 1e9 × ~300 B × 3 replicas × 7-day retention ≈ 6 TB. Flink: ~24 task slots (≈50k/s peak at 2k events/s/slot), state ≈ 200k keys × 30 days × 2 points × 200 B ≈ 2.4 GB per day of keys—at 1e9/day that is ~2 TB of RocksDB state, so ~16 workers × 128 GB disk. Lakehouse: ~300 GB/day compressed deltas, ~9 TB rolling window plus compacted history. Query tier: 3 replicas of a vectorised engine (Trino/DuckDB) for 99.9 %.

**Cutover and backfill.** Replay the 30-day window through the new pipeline into a shadow epoch space, run this repository's differential comparison (incremental vs full recompute) per merchant/day against the legacy answers, then flip the epoch pointer. Backfill of older history is a batch job that emits deltas with their original epochs so knowledge-time queries stay honest.

**What changes from the local solution.** Batch number → epoch; SQLite tables → Kafka state + Iceberg deltas; `publish` transaction → epoch pointer publication; `explain` → a lineage service over delta history; the rounding, selection and join code is unchanged and should be shared as a library with the same mutation-tested suite.
