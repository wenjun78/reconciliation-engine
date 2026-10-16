# The Reconciliation Engine

A local, recoverable settlement data product: JSONL batches of ledger entries, merchant classifications, FX rates, profiles and erasures go in; exact USD-microdollar settlements at any **knowledge cutoff** come out, and stay correct after late data, retroactive corrections, conflicting revisions, retries, crashes and erasure requests.

* Python 3.12 + SQLite, **zero third-party runtime dependencies**.
* Two-phase ingest (durable staging → one atomic publish) with truthful `after-stage` / `before-commit` failpoints.
* Incremental **knowledge-time timelines** per business key; an independent full-recompute engine cross-checks them.
* Exact integer money (arbitrary precision), ties-to-even per entry.
* PII (names/emails) is discarded on ingest and never stored; erasure masks tokens globally.
* 85 tests (unit, scenario, SIGKILL recovery, privacy, randomised differential, adapter contract) plus an 18-mutant mutation harness, all killed.

Documents: [DESIGN.md](DESIGN.md) · [OPERATIONS.md](OPERATIONS.md) · [EVIDENCE.md](EVIDENCE.md) · [AI_LOG.md](AI_LOG.md) · [LIMITATIONS.md](LIMITATIONS.md)

## Build

```sh
docker build -t recon:local .          # pins python:3.12-slim-bookworm; asserts 3.12 + SQLite ≥ 3.35 at build time
                                       # and self-tests the public fixture inside the image (offline)
./tools/pin_base_image.sh              # optional, once, on a host with Docker Hub access: rewrites FROM to an @sha256 digest
```

No network is needed at runtime. Nothing is installed by pip for the engine; `requirements-dev.txt` (hash-pinned pytest) is only for the optional pytest runner.

## Run (adapter contract)

```sh
./run ingest   --input /abs/batch.jsonl --state /abs/state
./run ingest   --input /abs/batch.jsonl --state /abs/state --failpoint after-stage     # exits 86 after durable staging
./run ingest   --input /abs/batch.jsonl --state /abs/state --failpoint before-commit   # exits 86 right before COMMIT
./run snapshot --query /abs/query.json --state /abs/state --out /abs/result.json       # exit 65 if batches 1..cutoff are not all committed
```

Container form (read-only root, writable `/state` and `/tmp`, any numeric UID):

```sh
docker run --rm --read-only --tmpfs /tmp --user 12345 -v $PWD/fixtures:/fixtures:ro -v /abs/state:/state \
  recon:local ingest --input /fixtures/data/batch_01.jsonl --state /state
docker run --rm --read-only --tmpfs /tmp --user 12345 -v $PWD/fixtures:/fixtures:ro -v /abs/state:/state -v /abs/out:/out \
  recon:local snapshot --query /fixtures/queries/q07.json --state /state --out /out/q07.json
./tools/docker_fixture.sh      # does the above for the whole public fixture and diffs against fixtures/expected
```

Operator commands (not required by the contract): `./run status`, `./run check [--deep]`, `./run explain --entry E [--cutoff B]` — see OPERATIONS.md.

Exit codes: 0 ok · 64 usage/bad query · 65 data (snapshot gap, malformed batch) · 66 input missing · 70 internal · 73 cannot write output · 86 failpoint.

## Verify

```sh
make test              # python3 -I -m unittest discover -s tests   (no dependencies, ~10 s)
make public-check      # ingest fixtures/data, diff all 10 public queries against fixtures/expected, both engines
make check-orders      # same with reversed and shuffled batch delivery
make mutants           # 18 single-rule mutants; every one must be killed by a named test
make bench             # 200k-key scale fixture generated on the fly, measured through ./run (nothing committed)
make docker-test       # the suite inside the image, read-only root, non-root
```

`.github/workflows/ci.yml` runs the suite, the public fixture, the mutation harness and the full Docker build + container contract on every push (Actions tab), so the image is verified even without a local Docker installation.

## Repository layout

```
run                 POSIX sh adapter (exec python3 -I bin/recon-cli.py)
recon/              engine: normalize · store · ingest · resolve · snapshot · inspect · money · cli
tests/              unittest-compatible suite (also runs under pytest); tests/mutation/run_mutants.py
bench/              gen_scale.py (deterministic hidden-shape fixture), bench.py (measurements)
fixtures/           the published public fixture (data, queries, expected, structural checker)
tools/              public_check.py, docker_fixture.sh, pin_base_image.sh, assert_runtime.py
ai_logs/            raw coding-agent session logs (see below)
Dockerfile · Makefile · pyproject.toml · requirements*.txt
```

## AI usage and logs

`AI_LOG.md` records the model/tool, the consequential interactions, what was checked and what was rejected, time and cost. The **raw session logs** live in `ai_logs/` (see `ai_logs/README.md` for the export procedure and what was redacted: credentials and unrelated private content only, never the working process on this exercise).

## Secrets

None are needed and none are committed. The engine reads no environment variables except the optional test hook `RECON_SNAPSHOT_HOLD_SECONDS` and `RECON_PYTHON` (interpreter override for `./run`). A `.env` file, if you create one, is gitignored.
