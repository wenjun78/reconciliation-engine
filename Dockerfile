# syntax=docker/dockerfile:1
# Reconciliation engine image.  Runtime has no third-party dependencies, so the
# only thing to pin is the interpreter.  Minor version is pinned by tag; run
# `tools/pin_base_image.sh` once on a host with Docker Hub access to replace the
# tag with an immutable @sha256 digest (see README "Build").
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY run /app/run
COPY bin /app/bin
COPY recon /app/recon
COPY tests /app/tests
COPY fixtures /app/fixtures
COPY tools /app/tools
COPY pyproject.toml requirements.txt requirements-dev.txt /app/

# Assert the pinned interpreter, pre-compile bytecode so a read-only /app never
# needs writing, record build facts, and make everything world-readable and
# executable for an arbitrary numeric UID.
RUN python3 /app/tools/assert_runtime.py > /app/BUILD_INFO \
 && python3 -m compileall -q /app/recon /app/bin /app/tests /app/tools \
 && chmod -R a+rX /app \
 && chmod a+x /app/run /app/bin/recon-cli.py \
 && cat /app/BUILD_INFO

# Offline self-test of the public fixture inside the image (no network needed).
RUN python3 -I /app/tools/public_check.py

# Non-root by default; any --user works because /app is world-readable.
USER 65534:65534
VOLUME ["/state"]
ENTRYPOINT ["/app/run"]
