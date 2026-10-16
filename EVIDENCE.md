# EVIDENCE

Every section below is **measured**: the command was executed and the output pasted (paths shortened to `$W` for the scratch work directory and `$R` for the repository root). Items that are *proposed design* rather than measurement are marked **[design]**. Host for all measurements: Linux 6.18 (x86_64), Intel Xeon @ 2.80 GHz, **2 vCPU**, 7.8 GiB RAM, Python 3.13.16, SQLite 3.45.1. The container image pins Python 3.12; the engine has no version-specific code between 3.10 and 3.13.

Transcripts in §1–§4 are reproducible with `./tools/evidence_run.sh`; §5 with `bench/bench.py`; §6 with `tests/mutation/run_mutants.py`; §7 with `make test`.

## 1. Correctness: public fixture

```
$ ./tools/evidence_run.sh
### 1. Correctness: ingest all public batches, snapshot every query
ingested: committed:        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
  == day2.json matches expected
  == empty.json matches expected
  == q01.json matches expected
  == q02.json matches expected
  == q03.json matches expected
  == q04.json matches expected
  == q05.json matches expected
  == q07.json matches expected
  == q09.json matches expected
  == q12.json matches expected

$ python3 -I fixtures/tools/check_output.py $W/out/q07.json
Output structure and ordering OK; values not checked.
exit=0
```

Delivery order and engine independence (`tools/public_check.py`):

```
$ python3 -I tools/public_check.py                          → ALL PUBLIC QUERIES MATCH
$ python3 -I tools/public_check.py --engine full            → ALL PUBLIC QUERIES MATCH
$ python3 -I tools/public_check.py --order reverse          → ALL PUBLIC QUERIES MATCH
$ python3 -I tools/public_check.py --order shuffle:7        → ALL PUBLIC QUERIES MATCH
$ python3 -I tools/public_check.py --order shuffle:42 --engine full → ALL PUBLIC QUERIES MATCH
```

Hand-calculable cases (WORKED_EXAMPLES 1–7) are each a named test in `tests/test_semantics.py` / `tests/test_money.py` (§7).

## 2. Replay and exact retry

```
$ ./run ingest --input $R/fixtures/data/batch_07.jsonl --state $W/state
recon ingest: batch 7 already committed; exact retry is a no-op
exit=0
$ ./run ingest --input $R/fixtures/data/batch_07.jsonl --state $W/state --failpoint before-commit
recon ingest: batch 7 already committed; exact retry is a no-op
exit=0            # failpoints fire only on previously unseen batches
$ snapshot q07 again → == q07.json matches expected
```

## 3. Crash recovery: both failpoints, restart, gap rejection, output preservation

```
$ ./run ingest --input .../batch_01.jsonl --state $W/state2
recon ingest: batch 1: staged 116 records ({'profiles': 23, 'merchants': 100, 'fx': 16}) in 0.01s
recon ingest: batch 1: committed in 0.01s total; {'ledger': 0, 'merchant': 100, 'fx': 16} keys touched
exit=0

$ ./run ingest --input .../batch_02.jsonl --state $W/state2 --failpoint after-stage
recon ingest: batch 2: staged 16 records ({'ledger': 16}) in 0.00s
recon ingest: failpoint after-stage reached for batch 2; staging is durable, nothing published; exiting 86
exit=86

$ ./run status --state $W/state2
committed:        [1]
servable cutoff:  1  (gaps: none)
staged/uncommit:  [2]
rows:             {'ledger_rev': 0, ..., 'ledger_state': 0, ..., 'stage_rows': 16}

$ ./run snapshot --query .../q01.json --state $W/state2 --out $W/out2/q01.json
recon snapshot: query q01 @cutoff 1 [2026-01-01,2026-01-05): 0 groups, 0 entries, 0 exceptions (incremental)
exit=0

$ snapshot q02 must fail with 65 and leave an existing --out untouched
recon snapshot: query q02: batches [2] not committed; refusing cutoff_batch=2 (exit 65)
exit=65
  --out preserved byte for byte

$ ./run ingest --input .../batch_02.jsonl --state $W/state2 --failpoint before-commit
recon ingest: batch 2: durable staging from a previous attempt matches; resuming at publish
recon ingest: failpoint before-commit reached for batch 2; exiting 86 without COMMIT
exit=86

$ ./run status --state $W/state2
committed:        [1]
staged/uncommit:  [2]
rows:             {'ledger_rev': 0, ..., 'ledger_state': 0, ...}     # nothing of batch 2 leaked

$ ./run ingest --input .../batch_02.jsonl --state $W/state2
recon ingest: batch 2: durable staging from a previous attempt matches; resuming at publish
recon ingest: batch 2: committed in 0.00s total; {'ledger': 16, 'merchant': 0, 'fx': 0} keys touched
exit=0
$ ./run ingest --input .../batch_02.jsonl --state $W/state2
recon ingest: batch 2 already committed; exact retry is a no-op
exit=0

$ txn_log for batch 2
   (4, 'stage', 'committed')
   (5, 'publish', 'begin')        # attempt that died at before-commit
   (6, 'publish', 'begin')
   (7, 'publish', 'committed')    # exactly one commit
   (8, 'ingest', 'already-committed')

$ reordered delivery: batches 12, 5, 3 then 4 (gap) ...
recon snapshot: query q05: batches [4] not committed; refusing cutoff_batch=5 (exit 65)
exit=65 (65 expected: batch 4 missing)
exit=0 (0 expected)                 # after batch 4 arrives
  == q05.json matches expected      # after batches 6..11 (erasure batch 10 included)
  == q12.json matches expected
```

Hard kills (`tests/test_recovery.py::HardKills`): a 30,000-row batch is ingested in a subprocess and `SIGKILL`ed at a random moment (0.05–0.9 s) up to six times; after each kill `PRAGMA integrity_check` = ok and the batch is either fully visible (30,000 rev rows, 30,000 timeline points) or entirely absent; the final replay commits exactly once and the snapshot has 30,000 trace rows. Passes (§7).

Reader isolation (`tests/test_recovery.py::ReaderIsolation`): a snapshot holds its read transaction open (`RECON_SNAPSHOT_HOLD_SECONDS=2.5`); an erasure batch commits meanwhile (writer not blocked); the running snapshot still shows the token, the next snapshot masks it. Passes.

## 4. Privacy and provenance

```
$ grep -c SYNTHETIC_NAME / example.invalid in every state file
  engine.sqlite3: names=0 emails=0
$ ./run check --state $W/state --deep
  ok=True problems=[] integrity=['ok'] email_like=0 differential={'cutoffs_compared': 12, 'cutoffs_differing': []}
$ erased tokens C0/C1 masked in q12 trace
  masked: 6  C0/C1 present: False
$ ./run explain --entry L17_E0 --cutoff 3
   {'exception': {'code': 'REVISION_CONFLICT', 'key': 'L17_E0', 'stream': 'ledger', 'version': 2}, 'since_batch': 3, 'status': 'CONFLICT', 'version': 2}
$ ./run explain --entry L17_E0 --cutoff 9
   {'exact': '297/2', 'expression': '99 * 3 / 2', 'rounded_half_even_usd_micro': 148,
    'settlement_group': {'date': '2026-01-01', 'merchant_id': 'K17_M0', 'region': 'EU', 'tier': 'standard'}, 'trace_customer_token': None}
```

`tests/test_privacy.py` additionally verifies: profile PII absent from every byte of state after ingest; masking at earlier cutoffs after a later erasure; erasure surviving reordered delivery of an older batch, a replayed profile and an exact retry; stderr free of tokens and names; `explain` masking.

## 5. Scale (200,000 keys, 12 batches, 100 merchants, 4 currencies)

Fixture generated by `bench/gen_scale.py --seed 17` (not committed): 265,728 rows, 66.9 MB; 30,146 revisions, 4,068 conflicts, 6,028 schema-2 duplicates, 3,954 voids, 3,926 exact redeliveries, 500 erasures, 200 unsupported rows, skew: one merchant ≈30 % of entries, one day ≈20 %.

```
$ python3 -I bench/bench.py --work $W/bench-200k --keys 200000 --batches 12
ingest batch_01.jsonl: 0.11s  rss 19.7 MiB  state 0.2 MiB
ingest batch_02.jsonl: 1.10s  rss 73.2 MiB  state 13.8 MiB
ingest batch_03.jsonl: 1.18s  rss 73.3 MiB  state 19.8 MiB
ingest batch_04.jsonl: 1.27s  rss 72.8 MiB  state 25.8 MiB
ingest batch_05.jsonl: 1.31s  rss 72.8 MiB  state 31.6 MiB
ingest batch_06.jsonl: 1.29s  rss 72.8 MiB  state 37.5 MiB
ingest batch_07.jsonl: 1.31s  rss 72.1 MiB  state 43.2 MiB
ingest batch_08.jsonl: 1.33s  rss 71.5 MiB  state 48.9 MiB
ingest batch_09.jsonl: 1.38s  rss 72.3 MiB  state 54.8 MiB
ingest batch_10.jsonl: 1.47s  rss 71.5 MiB  state 60.3 MiB
ingest batch_11.jsonl: 1.43s  rss 70.8 MiB  state 65.7 MiB
ingest batch_12.jsonl: 1.53s  rss 70.5 MiB  state 71.1 MiB
snapshot month_full_cutoff  incremental : 1.92s  rss 225.6 MiB  192063 trace rows   (40 MB JSON, 3000 groups, 4184 exceptions)
snapshot month_full_cutoff  full        : 2.05s  rss 224.3 MiB  192063 trace rows
snapshot month_mid_cutoff   incremental : 0.94s  rss 125.6 MiB  99824 trace rows
snapshot month_mid_cutoff   full        : 1.09s  rss 126.2 MiB  99824 trace rows
snapshot single_day         incremental : 0.11s  rss 23.1 MiB  5085 trace rows
snapshot single_day         full        : 1.10s  rss 23.1 MiB  5085 trace rows
snapshot empty_range        incremental : 0.07s  rss 17.9 MiB  0 trace rows
snapshot empty_range        full        : 1.11s  rss 18.2 MiB  0 trace rows
targets: ingest_total_lt_180s=true  snapshot_each_lt_30s=true  peak_rss_lt_4gib=true  state_lt_5gib=true
ingest total 14.72s, peak rss 73.3 MiB, state 71.1 MiB      exact retry of a committed batch: 0.40s
```

Same fixture, shuffled delivery order (`--order shuffle`): ingest total 15.43 s, peak RSS 74.8 MiB, state 71.1 MiB; all four snapshot outputs **byte-identical** to the sorted-order run (`cmp`), and incremental == full on every query.

| Target (4-core host) | Measured here (2-core) | Margin |
|---|---|---|
| ingest total < 180 s | 14.7 s | 12× |
| each snapshot < 30 s | ≤ 2.05 s | 15× |
| peak memory < 4 GiB | 226 MiB | 18× |
| state < 5 GiB | 71 MiB | 72× |

Limits of full recomputation **[measured]**: `--engine full` costs ~1.1 s fixed per snapshot at this scale (a scan of all 265k revision rows) regardless of the date range; the incremental engine is proportional to in-range entries (0.07 s empty, 0.11 s one day). For full-range queries both are dominated by building and serialising ~190k trace rows.

## 6. Tests of the tests: mutation harness

```
$ python3 -I tests/mutation/run_mutants.py            # full suite per mutant
KILLED   ties-away-from-zero               12.3s  spec 3.4: round ties to even  by test_matches_fraction_round_on_random_grid
KILLED   float-division                    12.2s  spec 2: integers are exact above 2**53  by test_exact_above_double_precision
KILLED   last-arrival-wins                 12.1s  spec 3: same highest version with different payloads must CONFLICT  by test_incremental_equals_full_and_is_order_invariant
KILLED   conflicts-never-detected          11.8s  spec 3: block conflicting keys  by test_incremental_equals_full_and_is_order_invariant
KILLED   closed-interval-end               12.0s  spec 2: merchant validity is half-open [from, to)  by test_every_batch_retried_twice_is_harmless
KILLED   ambiguity-ignored                 12.0s  spec 3.1: more than one containing interval is AMBIGUOUS_MERCHANT  by test_every_batch_retried_twice_is_harmless
KILLED   erasure-not-masked                11.8s  spec 2 erasures: mask erased tokens  by test_erasure_masks_every_cutoff_including_earlier_ones
KILLED   erasure-respects-cutoff           12.5s  spec 3: masking uses all committed erasures independent of B  by test_erasure_masks_every_cutoff_including_earlier_ones
KILLED   quarantine-ignores-cutoff         12.1s  spec 3: include unsupported rows with batch <= B only  by test_prefix_answers_are_stable_as_later_batches_arrive
KILLED   inclusive-end-date                11.7s  spec 4: end_date is exclusive  by test_incremental_equals_full_and_is_order_invariant
KILLED   gap-check-disabled                12.1s  spec 3: every batch 1..B must be committed or exit 65  by test_after_stage_is_durable_but_invisible
KILLED   v2-amount-not-normalised          12.1s  spec ledger v2: normalise before comparing revisions  by test_v1_and_v2_equal_amounts_share_fingerprint
KILLED   before-commit-commits-anyway      11.7s  spec 5: before-commit must leave the previous committed state visible  by test_before_commit_leaves_previous_state_visible
KILLED   after-stage-publishes-first       12.0s  spec 5: after-stage fires before visible publication  by test_after_stage_is_durable_but_invisible
KILLED   timeline-skips-current-batch      12.3s  incremental state must materialise the point introduced by this batch  by test_works_from_unrelated_cwd_with_absolute_paths
KILLED   profile-pii-retained              12.5s  spec profiles: name/email must not persist in managed storage  by test_profiles_drop_name_and_email
KILLED   trace-unsorted                    12.3s  spec 4: trace sorted by entry_id ascending  by test_erasure_masks_every_cutoff_including_earlier_ones
KILLED   fx-rounding-after-sum             12.4s  spec 3.4: round each entry, never the aggregate  by test_worked_example_1_group_net_zero_count_four

18/18 mutants killed
```

(`--fast` runs only each mutant's expected killer modules: also 18/18, 24 s total.)

## 7. Test suite

```
$ python3 -I -m unittest discover -s tests -p 'test_*.py'
Ran 85 tests in 12.2s
OK
$ python3 -m pytest -q tests        (pytest 8.3.5 from requirements-dev.txt)
85 passed, 13 subtests passed
```

Modules: `test_money` (rounding, big ints, strict text parsing, 20k-point check against `fractions.Fraction`), `test_normalize_resolve` (fingerprints, v1/v2 equality, quarantine, pure resolution timeline), `test_semantics` (worked examples 1–7, joins, knowledge time, unsupported schema), `test_public_fixture` (all orders, both engines, structural checker), `test_recovery` (failpoints, SIGKILL, isolation, output preservation, exit codes), `test_privacy`, `test_differential` (8 random datasets × 3 delivery orders × 6 cutoffs × 4 windows, incremental == full == order-invariant; prefix stability as batches arrive), `test_adapter` (cwd independence, exit codes, non-root read-only tree).

## 8. Container contract (executed natively; `docker build` not run here — see LIMITATIONS)

The Dockerfile's steps were replayed on a copy of the tree: `tools/assert_runtime.py`, `compileall`, `chmod -R a+rX`, public-fixture self-test with `python3 -I`; then the whole tree was made read-only and the suite run as **uid 65534** with `HOME=/nonexistent`, `PYTHONDONTWRITEBYTECODE=1`, from cwd `/`:

```
$ setpriv --reuid=65534 --regid=65534 --clear-groups python3 -I -m unittest discover -s /tmp/imgsim/app/tests -p 'test_*.py'
Ran 85 tests in 15.1s
OK
$ find /tmp/imgsim/app -newer /tmp/imgsim/app/BUILD_INFO -type f -not -name '*.pyc'
(none)                      # nothing written into the app tree
```

**[design]** In the container the same holds with `--read-only --tmpfs /tmp --user N -v state:/state -v out:/out`; `tools/docker_fixture.sh` runs the public fixture that way and diffs against `fixtures/expected`.

## 9. Not measured

* Docker image build and run on the reviewers' 4-core host (LIMITATIONS §1).
* Behaviour under two concurrent writers (out of scope).
