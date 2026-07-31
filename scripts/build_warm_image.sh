#!/usr/bin/env bash
# Build (and optionally push) a warm benchmark image (see Dockerfile.warm).
#
# Usage:
#   scripts/build_warm_image.sh cpu [--push docker.io/<ns>]
#   scripts/build_warm_image.sh gpu [--push docker.io/<ns>]
#
# Local tag is tig-swarm-warm-<flavor>. With --push, the image is also tagged
# <ns>/tig-swarm-warm-<flavor>:{latest,<git short sha>} and pushed.
#
# NOTE: C3 runs linux/amd64. An image built on an arm64 host runs locally but
# NOT on C3 — publish from an amd64 builder (CI: build-warm-images.yml).
set -euo pipefail
cd "$(dirname "$0")/.."

FLAVOR="${1:?usage: build_warm_image.sh <cpu|gpu> [--push <registry-ns>]}"
case "$FLAVOR" in
  cpu) BASE="ubuntu:24.04" ;;
  gpu) BASE="nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04" ;;
  *) echo "flavor must be cpu or gpu" >&2; exit 2 ;;
esac
LOCAL_TAG="tig-swarm-warm-$FLAVOR"

# The bake compiles the crate, so every challenge needs SOME algorithm in its
# slot (src/*/algorithm is gitignored; seed_algorithms skips slots that are
# already populated). The baked algorithm is a placeholder — jobs overwrite it.
python3 scripts/seed_algorithms.py

docker build -f Dockerfile.warm \
  --build-arg "FLAVOR=$FLAVOR" --build-arg "BASE=$BASE" \
  -t "$LOCAL_TAG" .

if [ "${2:-}" = "--push" ]; then
  NS="${3:?--push needs a registry namespace, e.g. docker.io/mydockerhubuser}"
  SHA="$(git rev-parse --short HEAD)"
  for tag in latest "$SHA"; do
    docker tag "$LOCAL_TAG" "$NS/$LOCAL_TAG:$tag"
    docker push "$NS/$LOCAL_TAG:$tag"
  done
fi
echo "built $LOCAL_TAG"
