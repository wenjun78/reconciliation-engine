#!/bin/sh
# Drive the container exactly as the evaluation harness would: read-only root,
# writable /state and /tmp, explicitly mounted output dir, arbitrary numeric UID.
set -eu
cd "$(dirname "$0")/.."
work=${WORK:-$(mktemp -d)}
mkdir -p "$work/state" "$work/out"
chmod 777 "$work/state" "$work/out"
img=${IMG:-recon:local}
uid=${UID_TEST:-12345}
for f in fixtures/data/batch_*.jsonl; do
  docker run --rm --read-only --tmpfs /tmp --user "$uid" \
    -v "$PWD/fixtures:/fixtures:ro" -v "$work/state:/state" "$img" \
    ingest --input "/fixtures/data/$(basename "$f")" --state /state
done
for q in fixtures/queries/*.json; do
  n=$(basename "$q")
  docker run --rm --read-only --tmpfs /tmp --user "$uid" \
    -v "$PWD/fixtures:/fixtures:ro" -v "$work/state:/state" -v "$work/out:/out" "$img" \
    snapshot --query "/fixtures/queries/$n" --state /state --out "/out/$n"
  docker run --rm --read-only --tmpfs /tmp --user "$uid" \
    -v "$PWD/fixtures:/fixtures:ro" -v "$work/out:/out:ro" \
    --entrypoint python3 "$img" \
    -c 'import json,sys; a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2])); sys.exit(0 if a==b else 1)' \
    "/out/$n" "/fixtures/expected/$n" \
    && echo "ok $n" || { echo "MISMATCH $n"; exit 1; }
done
echo "container contract: all public queries match (uid=$uid, read-only root)"
