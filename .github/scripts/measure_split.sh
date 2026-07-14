#!/usr/bin/env bash
# Settle it directly: build a TRIVIAL algorithm vs cloudy's COMPLEX one through
# the identical path (warm std/deps cargo cache, each a fresh tig_algorithms
# compile) and compare. If TRIVIAL << COMPLEX -> the ~14min is algorithm
# complexity. If both are large -> there's fixed bloat.
#
# Runs INSIDE the hypergraph dev image: /work = tig-monorepo, /swarm = swarm repo.
set -euo pipefail

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=/work/objcache
rm -rf "$BUILD_SO_OBJ_CACHE_DIR"
install -m 0755 /swarm/scripts/build_so.measure /usr/local/bin/tig-scripts/build_so

grep -q "pub mod swarm_algo;" "tig-algorithms/src/${CH}/mod.rs" \
  || printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"

seed_template() {
  mkdir -p "$SLOT"
  cp "tig-algorithms/src/${CH}/template.rs" "${SLOT}/mod.rs"
  sed -i 's/-> anyhow::Result<Option<Solution>>/-> anyhow::Result<()>/' "${SLOT}/mod.rs"
  [ -f "tig-algorithms/src/${CH}/template.cu" ] && cp "tig-algorithms/src/${CH}/template.cu" "${SLOT}/kernels.cu" || true
}

run_measure() {  # $1 = label ; slot already seeded
  local label="$1" t0 t1
  t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$label BUILD FAILED"; tail -60 /tmp/m.log; exit 1; }
  t1=$(date +%s)
  echo "### $label total build_algorithm = $((t1-t0))s"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $label /"
  echo "### $label top uncached files:"
  grep -E '^\[file\] ' /tmp/m.log | grep -vE ' CACHED ' | sort -rn -k2 | head -6 | sed "s/^/### $label /"
}

# (0) warm-up with the template -> warms std/deps cargo + object cache
seed_template
echo "::group::warm-up (template)"
build_algorithm swarm_algo >/tmp/warm.log 2>&1 || { echo "WARM FAILED"; tail -40 /tmp/warm.log; exit 1; }
echo "::endgroup::"

# (A) TRIVIAL: template + a distinct trivial tweak -> fresh compile, tiny .ll
seed_template
printf '\npub fn _trivial_variant_marker() -> u64 { 7 }\n' >> "${SLOT}/mod.rs"
echo "::group::measure TRIVIAL"
run_measure "TRIVIAL"
echo "::endgroup::"

# (B) COMPLEX: cloudy's real algorithm -> fresh compile, big .ll
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true
echo "::group::measure COMPLEX (cloudy)"
run_measure "COMPLEX"
echo "::endgroup::"

echo "==================== VERDICT ===================="
echo "TRIVIAL << COMPLEX  -> ~14min is algorithm complexity (heavy cudarc monomorphization)."
echo "TRIVIAL ~= COMPLEX  -> fixed bloat, independent of the algorithm."
echo "================================================="
