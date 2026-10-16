# LIMITATIONS

Honest list of what is incomplete, unverified on the reviewers' setup, or interpreted.

## Not verified in this environment

* **`docker build` was not executed** where this submission was assembled (no Docker daemon available). The Dockerfile uses only `COPY`, `RUN python3 …`, `chmod`, `USER`; the equivalent steps—`compileall`, world-readable tree, the public-fixture self-test with `python3 -I`, and the full test suite running as uid 65534 from a read-only copy of the tree with `HOME=/nonexistent`—were executed natively and pass (EVIDENCE §8). **Please run `docker build -t recon:local . && ./tools/docker_fixture.sh && make docker-test` before submitting, or push and check the `docker` job in GitHub Actions (`.github/workflows/ci.yml`)**; if anything fails there it is a packaging fault, not an engine fault, and the native evidence still stands.
* **Base image is pinned by tag (`python:3.12-slim-bookworm`), not digest**, because Docker Hub was unreachable from the build environment. `tools/pin_base_image.sh` rewrites the `FROM` line to an immutable `@sha256` digest in one command on any host with Hub access. The interpreter minor version and SQLite minimum are asserted at build time regardless.
* **Measurements were taken on a 2-vCPU host** (smaller than the stated 4-core/8 GiB evaluation host) with Python 3.13.16 rather than the image's 3.12. Numbers are ~10× inside every target, so the margin covers the difference, but the review host's figures are the ones that count.

## Interpretations of the specification (documented, challengeable)

* **FX payload equality is exact on `(usd_micro_num, usd_micro_den)`.** `2/4` and `1/2` at the same version are treated as different payloads → `REVISION_CONFLICT`. The specification says "compare only the declared fields after normalising v2 amounts" and names no fraction normalisation. Switching to reduced-fraction equality is a one-line change in `recon/normalize.py` (`fingerprint([str(num), str(den)])`).
* **A retried batch number with different bytes** (explicitly out of scope) is treated as an already-committed batch: logged with `same_bytes=false` on stderr and in `txn_log`, exit 0, no state change. Rejecting with a non-zero code would be equally defensible.
* **Malformed input** (out of scope): a file with mixed batch numbers, an empty file, or a supported-schema payload missing a mandatory field is rejected whole with exit 65 *before* any state write, rather than partially ingested.
* **`UNSUPPORTED_SCHEMA` `first_batch`** is the lowest batch in which the `row_id` was seen, so a quarantined row redelivered in a later batch is reported from its first appearance.

## Known engineering limits

* Concurrent writers are out of scope and untested; SQLite's write lock would serialise them.
* Full-range snapshots are bound by JSON serialisation (~2 s per 40 MB of output); output is built in memory, so a dataset 10–20× the scored scale would need streaming output.
* Timeline rebuild materialises all revisions of the keys touched by one batch in memory; a single key with millions of revisions would be the first memory hot spot.
* `check`'s PII scan looks for email-shaped strings only; names are unbounded synthetic text and are guaranteed absent by construction (never written), not by scanning.
* The `txn_log` grows by two to three rows per ingest and is never pruned.

## Process

* Development was AI-assisted (see AI_LOG.md). Active-hours and spend figures in AI_LOG.md are to be completed by the candidate from the session export; placeholders are marked, not estimated.
