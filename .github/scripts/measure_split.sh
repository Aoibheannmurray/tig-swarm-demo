#!/usr/bin/env bash
# Decompose build_so's ~825s for a REAL algorithm into its phases.
# Runs INSIDE the TIG hypergraph dev image:  /work = tig-monorepo, /swarm = swarm repo.
#
# Mirrors runtime: (1) warm build with the template (populates the cargo cache so
# std is compiled once, like the bench image), then (2) swap in a REAL algorithm
# (cloudy's) and time the build — cargo now recompiles only tig-algorithms +
# tig-binary (std warm), and the object cache hits the 56 library crates. The
# [phase] lines then attribute the time to cargo (rustc) vs the opt/llc/clang
# instrumentation loop vs link.
set -euo pipefail

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=/work/objcache
rm -rf "$BUILD_SO_OBJ_CACHE_DIR"

install -m 0755 /swarm/scripts/build_so.measure /usr/local/bin/tig-scripts/build_so

# (1) warm build with the template -> warms cargo cache + fills the object cache
mkdir -p "$SLOT"
cp "tig-algorithms/src/${CH}/template.rs" "${SLOT}/mod.rs"
sed -i 's/-> anyhow::Result<Option<Solution>>/-> anyhow::Result<()>/' "${SLOT}/mod.rs"
[ -f "tig-algorithms/src/${CH}/template.cu" ] && cp "tig-algorithms/src/${CH}/template.cu" "${SLOT}/kernels.cu" || true
printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"
echo "::group::warm build (template)"
build_algorithm swarm_algo >/tmp/warm.log 2>&1 || { echo "WARM BUILD FAILED"; tail -40 /tmp/warm.log; exit 1; }
echo "::endgroup::"

# (2) swap in the REAL algorithm (cloudy's) -> forces tig-algorithms/tig-binary miss
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true

# (3) TIMED build with the real algo (warm cargo for std, algo changed) = runtime
echo "::group::measured build (real algo)"
t0=$(date +%s)
build_algorithm swarm_algo >/tmp/meas.log 2>&1 || { echo "MEASURED BUILD FAILED"; tail -60 /tmp/meas.log; exit 1; }
t1=$(date +%s)
echo "::endgroup::"

echo "==================== SPLIT ===================="
echo "build_algorithm (build_so + build_ptx) total: $((t1-t0))s"
echo "--- phases (from build_so) ---"
grep -E '^\[phase\]' /tmp/meas.log || echo "(no phase lines)"
echo "--- which crates cargo recompiled ---"
grep -E '^\s*Compiling ' /tmp/meas.log | grep -vE '\.so for' || true
echo "--- per-file loop time (PROC/SKIP = cache miss; excludes CACHED) ---"
grep -E '^\[file\] ' /tmp/meas.log | grep -vE ' CACHED ' | sort -rn -k2 || true
echo "--- cache hits (CACHED) count ---"
grep -cE '^\[file\] .* CACHED ' /tmp/meas.log || true
echo "=============================================="
