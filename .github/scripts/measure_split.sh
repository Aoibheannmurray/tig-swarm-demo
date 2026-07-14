#!/usr/bin/env bash
# Does a WARM REPEAT / incremental build_algorithm get fast (like the user's <1min
# local build), or is every build ~14min in this path? Builds cloudy's algorithm
# 3× in ONE persistent container:
#   BUILD1 = fresh (std warm, algo new)      -- what a fresh C3 job does
#   BUILD2 = identical repeat (all warm)      -- does a warm repeat cache?
#   BUILD3 = after a tiny edit (incremental)  -- mimics iterating on an algorithm
# If BUILD2/3 << BUILD1 -> local <1min = warm reuse, and C3 is slow only because
# each job is a fresh container. If BUILD2/3 ~= BUILD1 -> this path never caches
# the repeat, so the local speed comes from a different image/build_so.
#
# Uses build_so.measure with the object cache DISABLED = original build_so
# behavior + our phase timers. Runs INSIDE the dev image: /work=monorepo, /swarm=swarm repo.
set -euo pipefail

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=""      # disable object cache -> faithful original behavior
install -m 0755 /swarm/scripts/build_so.measure /usr/local/bin/tig-scripts/build_so

grep -q "pub mod swarm_algo;" "tig-algorithms/src/${CH}/mod.rs" \
  || printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"

run_build() {  # $1 = label
  local label="$1" t0 t1
  t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$label FAILED"; tail -50 /tmp/m.log; exit 1; }
  t1=$(date +%s)
  echo "### $label total = $((t1-t0))s"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $label /"
}

# (0) warm-up with the template -> warm std/deps cargo cache in a persistent target/
mkdir -p "$SLOT"
cp "tig-algorithms/src/${CH}/template.rs" "${SLOT}/mod.rs"
sed -i 's/-> anyhow::Result<Option<Solution>>/-> anyhow::Result<()>/' "${SLOT}/mod.rs"
[ -f "tig-algorithms/src/${CH}/template.cu" ] && cp "tig-algorithms/src/${CH}/template.cu" "${SLOT}/kernels.cu" || true
echo "::group::warm-up (template)"
build_algorithm swarm_algo >/tmp/w.log 2>&1 || { echo "WARM FAILED"; tail -40 /tmp/w.log; exit 1; }
echo "::endgroup::"

# BUILD 1: cloudy's real algorithm, fresh
mkdir -p "$SLOT"
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true
echo "::group::BUILD1 (cloudy fresh)"; run_build "BUILD1"; echo "::endgroup::"

# BUILD 2: identical repeat, nothing changed
echo "::group::BUILD2 (cloudy identical repeat)"; run_build "BUILD2"; echo "::endgroup::"

# BUILD 3: tiny edit to the algorithm (incremental)
printf '\npub fn _edit_marker() -> u64 { 123 }\n' >> "${SLOT}/mod.rs"
echo "::group::BUILD3 (cloudy + tiny edit)"; run_build "BUILD3"; echo "::endgroup::"

echo "==================== REPEAT TEST ===================="
echo "BUILD2/BUILD3 << BUILD1  -> warm repeat/incremental is fast (explains local <1min;"
echo "                            C3 slow only because each job is a FRESH container)."
echo "BUILD2/BUILD3 ~= BUILD1  -> this path never caches the repeat; local <1min must"
echo "                            come from a different local image/build_so."
echo "===================================================="
