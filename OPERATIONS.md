# OPERATIONS

All commands below are run from the project root (or `/app` in the container). `S` is the absolute state directory.

## 1. Health at a glance

```sh
./run status --state $S            # committed batches, gaps, staged leftovers, row counts, file sizes
./run check  --state $S            # sqlite integrity_check, timeline invariants, PII byte scan
./run check  --state $S --deep     # + incremental vs full-recompute comparison at every servable cutoff
./run explain --state $S --entry L17_E0 --cutoff 9   # lineage of one entry
```

`check` exits 0 only when everything passes; its JSON lists each problem. `status --json` is the same data for scripts.

## 2. Replay and rebuild

**Replay** (re-sending any batch file, in any order) is always safe:

* a batch already in `batches` with the same SHA-256 is a logged no-op (`txn_log.outcome = already-committed`);
* a batch that was staged but not published resumes at publish;
* an unseen batch is staged and published.

**Full rebuild** from the immutable inputs:

```sh
mv $S $S.old && for f in /inputs/batch_*.jsonl; do ./run ingest --input $f --state $S; done
./run check --state $S --deep
```

Order does not matter. Rebuild time equals the ingest total in §6 (~15 s at 200k keys). Keep `$S.old` until `check --deep` passes and sample snapshots diff clean against it.

**Derived-state rebuild only**: no separate command by design. `--engine full` answers any query straight from revision history and `check --deep` says whether the derived tables disagree; if they do, rebuild from inputs and file the bug.

## 3. Schema change

* **Input schema** (new `schema_version` or stream): unknown versions are quarantined and surface as `UNSUPPORTED_SCHEMA`, so a new producer version is visible before it is supported. Supporting it means a normaliser branch in `recon/normalize.py` mapping the payload onto the *same* business fields and fingerprint. Quarantined rows are not re-evaluated automatically: bump `STATE_SCHEMA_VERSION` and rebuild if already-ingested batches must be reinterpreted.
* **State schema** (`recon/store.py`): `meta.state_schema_version` is checked on open and a mismatch refuses to run. Upgrade path is rebuild-from-inputs (cheap, deterministic, verifiable), not in-place migration.

## 4. Failed-run recovery

| Symptom | Meaning | Action |
|---|---|---|
| ingest exit 86 | a `--failpoint` fired (test hook) | re-run without the flag |
| ingest killed / host crash | transaction open at death | nothing to clean: SQLite rolls back on next open; re-run the same ingest |
| `status` shows `staged_uncommitted: [N]` | stage committed, publish not | re-run `ingest` for batch N (resumes at publish) |
| snapshot exit 65 | `1..B` not all committed (message lists the missing batches) | ingest the missing batches; the existing `--out` file was not touched |
| snapshot exit 64 / 73 | bad query file / cannot write output dir | fix the input; state untouched |
| ingest exit 65 / 66 | malformed batch (mixed batch numbers, empty) / missing file | nothing was written; fix the file |
| exit 70 | internal error (message on stderr, no payload data) | `./run check`, then file a bug with the stderr line |

Every attempt is in `txn_log` (`sqlite3 $S/engine.sqlite3 'select * from txn_log order by id'`); a `publish/begin` marker without a following `committed` identifies a crash window.

## 5. Deletion (erasure)

Erasures are ordinary batches. Once committed, every snapshot whose read transaction starts afterwards masks that token at every cutoff. Names and emails are never stored, so nothing else needs purging; `./run check` confirms zero email-shaped strings in state. Erasure survives replay of older batches. Verify:

```sh
./run snapshot --query q.json --state $S --out r.json && grep -c '"customer_token":"C0"' r.json   # expect 0
```

Input files are *outside* managed state—do not create unredacted archives of them.

## 6. Performance measurements

Measured on the build host (Linux, 2× Xeon 2.8 GHz vCPU, 7.8 GiB, Python 3.13.16, SQLite 3.45.1—smaller than the review host) at the hidden-fixture shape: 200,000 keys, 265,728 rows, 12 batches, 100 merchants, 4 currencies, 30 days, skewed.

| Measure | Observed | Target |
|---|---|---|
| ingest, 12 batches total | 14.7 s (max batch 1.5 s) | < 180 s |
| ingest peak RSS | 73 MiB | < 4 GiB |
| state on disk | 71 MiB | < 5 GiB |
| snapshot, full month, cutoff 12 (192k trace rows, 40 MB JSON) | 1.9 s incremental / 2.1 s full | < 30 s |
| snapshot, single day | 0.11 s incremental / 1.10 s full | < 30 s |
| snapshot, empty range | 0.07 s / 1.11 s | < 30 s |
| snapshot peak RSS (full month) | 226 MiB | < 4 GiB |
| exact retry of a committed batch | 0.4 s | – |

Reproduce: `python3 bench/bench.py --work ./bench_out` (`--order shuffle` for reordered delivery); raw output in EVIDENCE.md. Ingest is linear in rows; snapshot is linear in in-range entries with a JSON serialisation floor (~2 s per 40 MB); memory is dominated by the trace list (~1 KB per entry).

## 7. Observability

* **stderr** is the only log; one line per phase with counts, durations and exit reasons, never payload values or tokens.
* **`txn_log`** table: durable, queryable history of what each run did.
* **Exit codes**: 0 ok · 64 usage · 65 data (gap or malformed input) · 66 missing input · 70 internal · 73 cannot write output · 86 failpoint.
* **Metrics to watch**: ingest wall time and `touched_keys` per batch (a jump means a wide correction), `CONFLICT` points per batch, erasure count, state growth, snapshot wall time vs trace rows.

## 8. Incident runbook: "settlement for merchant M on day D looks wrong"

1. **Freeze the question.** Record the query and output file. `./run status --state $S`: no gaps, no staged leftovers.
2. **Reproduce both ways.** Run the same query with `--engine incremental` and `--engine full`; `diff` the files. If they differ, the derived state is corrupt: switch consumers to `--engine full`, rebuild from inputs (§2), file the bug with the two outputs.
3. **Explain the entries.** For each trace entry for M/D (and any exception naming M): `./run explain --state $S --entry E --cutoff B` shows every revision with its batch, which version won and since when, the merchant interval and FX row chosen (with `row_id`s), the exact product and the rounded value.
4. **Classify.** Typical findings: a revision moved the entry to another day (check that day); overlapping intervals gave `AMBIGUOUS_MERCHANT` (upstream must correct the interval); an FX correction landed after the cutoff (rerun at the newer cutoff); a conflicting pair at the highest version (upstream must publish a higher unambiguous version—never pick a winner locally).
5. **Communicate** the finding with the `explain` JSON attached; it is PII-free (erased tokens are masked there too).
6. **If data must change**, it changes upstream as a new batch, never by editing state. After it is ingested, rerun the query at the new cutoff; the old cutoff still reproduces the old answer—that is the audit trail.
7. **Close** by adding the case to `tests/test_semantics.py` if it revealed a rule the suite did not pin.
