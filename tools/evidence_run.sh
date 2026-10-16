#!/bin/sh
# Reproduces the transcripts in EVIDENCE.md (correctness, replay, crash
# recovery, privacy) against the public fixture.  Prints commands and results.
set -u
cd "$(dirname "$0")/.."
ROOT=$PWD
W=${WORK:-$(mktemp -d)}
S=$W/state; O=$W/out; mkdir -p "$O"
D=$ROOT/fixtures/data; Q=$ROOT/fixtures/queries; E=$ROOT/fixtures/expected
say() { printf '\n$ %s\n' "$*"; }
run() { say "$*"; "$@"; echo "exit=$?"; }
same() { python3 -c "import json,sys; sys.exit(0 if json.load(open('$1'))==json.load(open('$2')) else 1)" && echo "  == $(basename "$2") matches expected" || echo "  != $(basename "$2") DIFFERS"; }

echo "### 1. Correctness: ingest all public batches, snapshot every query"
for f in "$D"/batch_*.jsonl; do ./run ingest --input "$f" --state "$S" 2>/dev/null || echo "ingest failed: $f"; done
echo "ingested: $(./run status --state "$S" | sed -n 2p)"
for q in "$Q"/*.json; do n=$(basename "$q"); ./run snapshot --query "$q" --state "$S" --out "$O/$n" 2>/dev/null; same "$O/$n" "$E/$n"; done
run python3 -I fixtures/tools/check_output.py "$O/q07.json"

echo
echo "### 2. Replay / exact retry (already committed batch, with and without failpoints)"
run ./run ingest --input "$D/batch_07.jsonl" --state "$S"
run ./run ingest --input "$D/batch_07.jsonl" --state "$S" --failpoint before-commit
say "snapshot q07 again, compare"; ./run snapshot --query "$Q/q07.json" --state "$S" --out "$O/q07.json" 2>/dev/null; same "$O/q07.json" "$E/q07.json"

echo
echo "### 3. Crash recovery on a fresh state: both failpoints, gap rejection, output preservation"
S2=$W/state2; O2=$W/out2; mkdir -p "$O2"
run ./run ingest --input "$D/batch_01.jsonl" --state "$S2"
run ./run ingest --input "$D/batch_02.jsonl" --state "$S2" --failpoint after-stage
run ./run status --state "$S2"
run ./run snapshot --query "$Q/q01.json" --state "$S2" --out "$O2/q01.json"
cp "$O2/q01.json" "$O2/q01.before"
say "snapshot q02 must fail with 65 and leave an existing --out untouched"
cp "$O2/q01.json" "$O2/q02.json"
./run snapshot --query "$Q/q02.json" --state "$S2" --out "$O2/q02.json"; echo "exit=$?"
cmp "$O2/q02.json" "$O2/q01.before" && echo "  --out preserved byte for byte"
run ./run ingest --input "$D/batch_02.jsonl" --state "$S2" --failpoint before-commit
run ./run status --state "$S2"
run ./run ingest --input "$D/batch_02.jsonl" --state "$S2"
run ./run ingest --input "$D/batch_02.jsonl" --state "$S2"
say "txn_log for batch 2"; python3 -c "
import sqlite3; c=sqlite3.connect('$S2/engine.sqlite3')
for r in c.execute('select id,phase,outcome from txn_log where batch=2 order by id'): print('  ', r)"
say "reordered delivery: batches 12, 5, 3 then 4 (gap) ..."
for b in 12 05 03; do ./run ingest --input "$D/batch_$b.jsonl" --state "$S2" 2>/dev/null; done
./run snapshot --query "$Q/q05.json" --state "$S2" --out "$O2/q05.json"; echo "exit=$? (65 expected: batch 4 missing)"
./run ingest --input "$D/batch_04.jsonl" --state "$S2" 2>/dev/null
./run snapshot --query "$Q/q05.json" --state "$S2" --out "$O2/q05.json" 2>/dev/null; echo "exit=$? (0 expected)"
echo "  (differs from expected only by erasure masking: batch 10 not yet ingested)"
for b in 06 07 08 09 10 11; do ./run ingest --input "$D/batch_$b.jsonl" --state "$S2" 2>/dev/null; done
./run snapshot --query "$Q/q05.json" --state "$S2" --out "$O2/q05.json" 2>/dev/null; same "$O2/q05.json" "$E/q05.json"
./run snapshot --query "$Q/q12.json" --state "$S2" --out "$O2/q12.json" 2>/dev/null; same "$O2/q12.json" "$E/q12.json"

echo
echo "### 4. Privacy: no names/emails in managed storage; tokens masked; replay after erasure"
say "grep -c SYNTHETIC_NAME / example.invalid in every state file"
for f in "$S"/*; do printf '  %s: names=%s emails=%s\n' "$(basename "$f")" "$(grep -c -a SYNTHETIC_NAME "$f")" "$(grep -c -a example.invalid "$f")"; done
say "./run check --state (pii scan + integrity + deep differential)"
./run check --state "$S" --deep | python3 -c "import json,sys; d=json.load(sys.stdin); print('  ok=%s problems=%s integrity=%s email_like=%s differential=%s' % (d['ok'], d['problems'], d['integrity_check'], d['pii_scan']['email_like_strings'], d['differential']))"
say "erased tokens C0/C1 masked in q12 trace (count of null tokens) and the token values never appear"
python3 -c "
import json; r=json.load(open('$O/q12.json')); print('  masked:', sum(1 for t in r['trace'] if t['customer_token'] is None), ' C0/C1 present:', any(t['customer_token'] in ('C0','C1') for t in r['trace']))"
say "explain L17_E0 at cutoff 3 (conflict) and 9 (selected v3, FX v2)"
./run explain --state "$S" --entry L17_E0 --cutoff 3 | python3 -c "import json,sys; d=json.load(sys.stdin); print('  ', d['resolution'])"
./run explain --state "$S" --entry L17_E0 --cutoff 9 | python3 -c "import json,sys; d=json.load(sys.stdin); print('  ', d['arithmetic'])"
echo
echo "work dir: $W"
