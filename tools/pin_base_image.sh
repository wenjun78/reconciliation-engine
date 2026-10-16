#!/bin/sh
# Resolve the current digest of the base image and rewrite the Dockerfile FROM
# line to an immutable reference.  Needs Docker with Hub access; run once.
set -eu
cd "$(dirname "$0")/.."
tag="python:3.12-slim-bookworm"
digest=$(docker buildx imagetools inspect "$tag" --format '{{json .Manifest.Digest}}' | tr -d '"')
[ -n "$digest" ] || { echo "could not resolve digest for $tag" >&2; exit 1; }
sed -i.bak "s|^FROM python:3.12-slim-bookworm.*|FROM ${tag}@${digest}|" Dockerfile && rm -f Dockerfile.bak
grep '^FROM' Dockerfile
