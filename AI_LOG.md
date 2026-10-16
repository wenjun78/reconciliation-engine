# AI_LOG

## Tools and models

| Item | Value |
|---|---|
| Tool | Claude (claude.ai, agentic coding session with shell, file and test execution) |
| Model | `claude-fable-5-1` as configured for the session (the serving model can differ; the raw log records what served each turn) |
| Session id | `session_011tQdng5wBc2mbUpDme1N8Q` — transcript at `ai_logs/claude-session_011tQdng5wBc2mbUpDme1N8Q.md` (assistant-generated from the live session context at the end of the session, not a platform export; user/assistant text verbatim, tool calls listed in order with condensed output — see the file header and `ai_logs/README.md`) |
| Active hours | ~2 hours (one session, 2026-10-16 17:54–19:40 MYT) |
| Approximate spend | Claude subscription; no per-token charge |
| Credentials in logs | none used; nothing to redact beyond unrelated content (see `ai_logs/README.md`) |

How the work was split: the candidate supplied the assessment package and the brief ("10/10, be creative"); the AI read the specification, proposed the architecture, wrote the code, tests, benchmarks and documents, and ran every verification in this repository. Every claim in EVIDENCE.md is the output of a command the AI executed in the session; nothing was typed in by hand.

## Five consequential interactions

1. **Architecture choice: two-phase ingest instead of one transaction.** The AI's first sketch was a single `BEGIN … COMMIT` per batch (both failpoints coinciding, which the specification permits). It was rejected during design because `after-stage` would then have nothing durable to point at and would read as the "fake early exit" the spec warns about. Outcome: durable staging tables committed in their own transaction, publish reads only from staging, two distinct failpoints. *Checked by:* `tests/test_recovery.py::Failpoints` and the two failpoint mutants in the mutation harness.

2. **Incremental state design: knowledge-time timelines, not aggregate caches.** The AI evaluated per-cutoff settlement caches with invalidation on date moves and dimension corrections, estimated the invalidation surface (merchant and FX corrections fan out to many entries, reordered batches rewrite history) and rejected it in favour of per-key resolution change points plus a full-recompute oracle. *Checked by:* `tests/test_differential.py` (random data, random delivery order, incremental == full at every cutoff and window) and `./run check --deep`.

3. **Bug found by the first run and corrected: schema creation silently committed early.** The initial `Store._init_schema` wrapped `executescript()` in an explicit transaction; Python's `sqlite3.executescript` issues an implicit COMMIT first, so the first ingest failed with "cannot rollback - no transaction is active". The AI replaced it with statement-by-statement execution inside one transaction. This was caught by running the public fixture, not by reading the code—evidence that execution, not review, is what catches this class of error.

4. **Test defect corrected: a recovery test asserted "resuming at publish" but generated new `row_id`s on replay**, so the replayed file had a different SHA-256 and was legitimately re-staged. The suite failed (83/84); the AI fixed the test to reuse the same rows, then also made the in-process drivers silence engine logs. The engine behaviour was right; the test was wrong—recorded here because a less careful fix would have been to weaken the engine's SHA check.

5. **Mutation harness false positive detected and corrected.** The first "fast" mode of `tests/mutation/run_mutants.py` reported 18/18 killed in 0.1 s each; the AI noticed the kill reasons were module names rather than test names, ran a module by hand, and found `python -I -m unittest test_money` could not import the module (cwd not on `sys.path` under `-I`), i.e. every "kill" was an import error. The harness now runs `discover` per module and aborts on `_FailedTest`. Final result 18/18 killed with real test names (EVIDENCE §6).

## Suggestions rejected or corrected (at least two)

* **Rejected: aggregate caches** (interaction 2). Complexity without a measurable win at the scored scale; full-range snapshots are output-bound.
* **Rejected: single-transaction ingest** (interaction 1). Permitted, but weaker evidence for the recovery rubric.
* **Corrected: `executescript` transaction bug** (interaction 3) and **mutation-harness import failures masquerading as kills** (interaction 5).
* **Rejected: reduced-fraction FX equality.** The AI raised that `2/4` vs `1/2` could be intended as duplicates; the literal reading ("compare only the declared fields") was kept and the interpretation is recorded in LIMITATIONS.md as challengeable.
* **Rejected: a digest-pinned base image written from memory.** Docker Hub was unreachable, so instead of inventing a `sha256` the Dockerfile pins the tag and ships `tools/pin_base_image.sh`; this is disclosed in LIMITATIONS.md.

## What was checked, and how

* Public fixture: all 10 expected files, byte-for-value, in sorted, reversed and three shuffled delivery orders, with both engines.
* Hand-calculable examples from WORKED_EXAMPLES.md are each a named test in `tests/test_semantics.py`.
* Recovery: both failpoints, exact retry, resume-from-staging, six random `SIGKILL`s of a 30k-row ingest with integrity and exactly-once replay checks, reader isolation with a held read transaction.
* Privacy: byte-level scan of every state file for names/emails after profile ingest, erasure and replay; stderr checked for tokens.
* Scale: generated 200k-key fixture measured through the real adapter (EVIDENCE §5).
* Tests of the tests: 18 single-rule mutants, all killed by named tests.
