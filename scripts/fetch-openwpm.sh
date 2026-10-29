#!/usr/bin/env bash
# Build the pinned OpenWPM base image from a verified upstream clone.
#
# Upstream's Dockerfile copies its own source tree as the build context, so the
# base image is built from a clone rather than reimplemented here. The clone is
# verified against a pinned commit, and upstream's floating `FROM ubuntu:22.04`
# is rewritten to a pinned digest so the base does not drift between builds.
#
# Produces: dv-openwpm-base:<OPENWPM_TAG>, plus a provenance record beside it.

set -euo pipefail

OPENWPM_REPOSITORY="https://github.com/openwpm/OpenWPM.git"
OPENWPM_TAG="v0.38.0"
# Tag v0.38.0, released 2026-10-04; the release that moves to Firefox 157.
OPENWPM_COMMIT="da628bab9bfa77117d28f44e625914ba8cab4e8d"
# ubuntu:22.04 multi-arch index digest, resolved 2026-10-29.
UBUNTU_DIGEST="sha256:e0f80c829509ef4df9e76bd8944eb7b8ec25cbb379b112a7f9448b2c77a77c9b"

IMAGE_NAME="dv-openwpm-base:${OPENWPM_TAG}"
WORK_DIR="${OPENWPM_CHECKOUT_DIR:-${TMPDIR:-/tmp}/openwpm-${OPENWPM_TAG}}"
PROVENANCE_PATH="${OPENWPM_PROVENANCE_PATH:-$(dirname "$0")/../data/openwpm-base-provenance.txt}"

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is required and was not found on PATH" >&2
    exit 1
fi

if [ ! -d "${WORK_DIR}/.git" ]; then
    echo "cloning ${OPENWPM_REPOSITORY} at ${OPENWPM_TAG}"
    rm -rf "${WORK_DIR}"
    git clone --quiet --branch "${OPENWPM_TAG}" --depth 1 "${OPENWPM_REPOSITORY}" "${WORK_DIR}"
fi

observed_commit="$(git -C "${WORK_DIR}" rev-parse HEAD)"
if [ "${observed_commit}" != "${OPENWPM_COMMIT}" ]; then
    echo "error: ${OPENWPM_TAG} resolved to ${observed_commit}, expected ${OPENWPM_COMMIT}" >&2
    echo "a moved tag is a supply-chain event; investigate before building" >&2
    exit 1
fi

# Pin upstream's base image. The rewrite is applied to the clone, never committed.
sed -i.bak "s|^FROM ubuntu:22.04$|FROM ubuntu@${UBUNTU_DIGEST}|" "${WORK_DIR}/Dockerfile"
if ! grep -q "^FROM ubuntu@${UBUNTU_DIGEST}$" "${WORK_DIR}/Dockerfile"; then
    echo "error: base-image pin did not apply; upstream Dockerfile FROM line has changed" >&2
    exit 1
fi

echo "building ${IMAGE_NAME} for linux/amd64"
docker build --platform linux/amd64 --tag "${IMAGE_NAME}" "${WORK_DIR}"

mkdir -p "$(dirname "${PROVENANCE_PATH}")"
{
    echo "openwpm_tag=${OPENWPM_TAG}"
    echo "openwpm_commit=${observed_commit}"
    echo "ubuntu_digest=${UBUNTU_DIGEST}"
    echo "image=${IMAGE_NAME}"
    echo "image_id=$(docker image inspect --format '{{.Id}}' "${IMAGE_NAME}")"
    echo "built_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${PROVENANCE_PATH}"

echo "wrote ${PROVENANCE_PATH}"
