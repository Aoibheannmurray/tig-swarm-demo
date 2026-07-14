#!/usr/bin/env bash
# Measure the AMD64 tig_algorithms.ll size (+ phase split) for cloudy's algorithm,
# to compare against the arm64 build's 15 MB. If amd64 emits much more IR, that
# explains why the single-threaded instrumentation is slower on C3 despite a
# faster CPU. Runs INSIDE the hypergraph dev image: /work=monorepo, /swarm=swarm repo.
set -euo pipefail

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=""      # original build_so behavior + our phase timers
install -m 0755 /swarm/scripts/build_so.measure /usr/local/bin/tig-scripts/build_so
grep -q "pub mod swarm_algo;" "tig-algorithms/src/${CH}/mod.rs" \
  || printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"

# seed cloudy's real algorithm
mkdir -p "$SLOT"
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true

echo "::group::build cloudy (amd64)"
t0=$(date +%s)
build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "BUILD FAILED"; tail -60 /tmp/m.log; exit 1; }
t1=$(date +%s)
echo "::endgroup::"

TGT=x86_64-unknown-linux-gnu
echo "==================== AMD64 IR SIZE ===================="
echo "arch:              $(uname -m)"
echo "nproc:             $(nproc)"
echo "build total:       $((t1-t0))s"
grep -E '^\[phase\]' /tmp/m.log || true
echo "--- tig_algorithms.ll (the single bottleneck file) ---"
ls -lh "target/${TGT}/release/deps/tig_algorithms.ll" 2>/dev/null \
  || find target -name 'tig_algorithms*.ll' -exec ls -lh {} \; 2>/dev/null
echo "--- total .ll size (all deps) ---"
find target -path "*${TGT}*release/deps*" -name '*.ll' 2>/dev/null | xargs du -ch 2>/dev/null | tail -1
echo "--- ARM64 reference (your local build): tig_algorithms.ll = 15M, total .ll = 367M ---"
echo "======================================================"
