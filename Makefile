# Convenience targets; every one is a plain command you can also run by hand.
PY ?= python3

.PHONY: test test-pytest public-check check-orders mutants bench docker-build docker-test docker-fixture

test:            ## full suite, standard library runner (no dependencies)
	$(PY) -I -m unittest discover -s tests -p 'test_*.py'

test-pytest:     ## same suite under pytest (pip install --require-hashes -r requirements-dev.txt)
	$(PY) -m pytest -q tests

public-check:    ## ingest fixtures/data and diff every public query against fixtures/expected
	$(PY) -I tools/public_check.py
	$(PY) -I tools/public_check.py --engine full

check-orders:    ## same, with reversed and shuffled batch delivery
	$(PY) -I tools/public_check.py --order reverse
	$(PY) -I tools/public_check.py --order shuffle:7
	$(PY) -I tools/public_check.py --order shuffle:42 --engine full

mutants:         ## mutation harness: every mutant must be killed
	$(PY) -I tests/mutation/run_mutants.py

bench:           ## generate the 200k-key scale fixture and measure (nothing committed)
	$(PY) -I bench/bench.py --work ./bench_out

docker-build:
	docker build -t recon:local .

docker-test:     ## run the test suite inside the image (read-only root, non-root uid)
	docker run --rm --read-only --tmpfs /tmp --entrypoint python3 -w /app recon:local -I -m unittest discover -s tests -p 'test_*.py'

docker-fixture:  ## public fixture through the container contract with a numeric uid
	./tools/docker_fixture.sh
