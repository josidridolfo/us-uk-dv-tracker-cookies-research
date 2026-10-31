#!/usr/bin/env bash
# Build the crawl image, pinning DV_REPO_COMMIT to the exact tree it came from.
#
# A hash taken from a dirty working tree is false provenance rather than
# missing provenance, which is worse, so this refuses to build on one. The
# resulting image bakes the commit in as a build ARG (see docker/Dockerfile)
# rather than reading it with `-e`, so it cannot be skipped by a direct
# `docker compose build` call that forgets to set it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}" || exit 1

if [ -n "$(git status --porcelain)" ]; then
    echo "error: working tree is dirty; commit or stash before building the crawl image" >&2
    exit 1
fi

DV_REPO_COMMIT="$(git rev-parse HEAD)"
export DV_REPO_COMMIT
echo "building crawl image at commit ${DV_REPO_COMMIT}"
docker compose -f "${REPO_ROOT}/compose.yaml" build crawl
