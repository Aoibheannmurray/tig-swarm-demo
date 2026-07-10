#!/usr/bin/env bash
#
# Build the custom benchmark image (Option B) for one challenge, locally.
# See docs/tig_docker_plan.md.
#
#   build_bench_image.sh <challenge> [version] [monorepo_path]
#
#   version        defaults to tig_version in tig_pin.json
#   monorepo_path  defaults to ../tig-monorepo (the pinned TIG source)
#   PLATFORM env   defaults to the HOST's platform (the image runs locally,
#                  and a non-native base can't execute without emulation —
#                  its first RUN dies with `exec format error`). Set it
#                  explicitly for cross-builds where emulation is available.
#
# Produces image:  tig-custom-image-<challenge>:<version>
set -euo pipefail

CHALLENGE="${1:?usage: build_bench_image.sh <challenge> [version] [monorepo_path]}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"   # swarm repo root
VERSION="${2:-$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['tig_version'])" "$HERE/tig_pin.json")}"
MONOREPO="${3:-$(cd "$HERE/.." && pwd)/tig-monorepo}"
case "$(uname -m)" in
  arm64|aarch64) DEFAULT_PLATFORM="linux/arm64" ;;
  *)             DEFAULT_PLATFORM="linux/amd64" ;;
esac
PLATFORM="${PLATFORM:-$DEFAULT_PLATFORM}"

if [ ! -d "$MONOREPO/tig-algorithms" ]; then
  echo "error: monorepo source not found at $MONOREPO (pass it as arg 3)" >&2
  exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

echo "Staging build context in $stage ..."
mkdir -p "$stage/tig-source"
# Re-add only the source the dev image stripped; exclude build artifacts / vcs.
tar -C "$MONOREPO" --exclude=./target --exclude=./.git -cf - . | tar -C "$stage/tig-source" -xf -
cp "$HERE/scripts/modified_test_algorithm" "$stage/modified_test_algorithm"
cp "$HERE/scripts/bench_blind_nn.py" "$stage/bench_blind_nn.py"
cp "$HERE/Dockerfile.bench" "$stage/Dockerfile.bench"

IMAGE="tig-custom-image-${CHALLENGE}:${VERSION}"
echo "Building $IMAGE  (platform=$PLATFORM, version=$VERSION) ..."
docker build --platform "$PLATFORM" \
  -f "$stage/Dockerfile.bench" \
  --build-arg CHALLENGE="$CHALLENGE" \
  --build-arg VERSION="$VERSION" \
  -t "$IMAGE" \
  "$stage"

echo "Built $IMAGE"
