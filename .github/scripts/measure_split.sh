#!/usr/bin/env bash
# The debuginfo experiment: build cloudy (amd64) with debuginfo=2 (current) vs
# debuginfo=0, and compare IR size, build time, and the resulting CODE.
# Prediction: debuginfo=0 collapses the 829MB tig_algorithms.ll and speeds up the
# build, with identical stripped code (debuginfo affects metadata, not codegen).
# Runs INSIDE the hypergraph dev image: /work=monorepo, /swarm=swarm repo.
set -euo pipefail

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
OUT="tig-algorithms/lib/${CH}/amd64/swarm_algo.so"
TGT=x86_64-unknown-linux-gnu
LLFILE="target/${TGT}/release/deps/tig_algorithms.ll"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=""      # original behavior + phase timers

grep -q "pub mod swarm_algo;" "tig-algorithms/src/${CH}/mod.rs" \
  || printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"
mkdir -p "$SLOT"
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true

run() {  # $1=build_so variant  $2=label  $3=saved-so path
  install -m 0755 "$1" /usr/local/bin/tig-scripts/build_so
  local t0 t1
  t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$2 BUILD FAILED"; tail -60 /tmp/m.log; exit 1; }
  t1=$(date +%s)
  echo "### $2 total=$((t1-t0))s  tig_algorithms.ll=$(ls -lh "$LLFILE" 2>/dev/null | awk '{print $5}')"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $2 /"
  cp "$OUT" "$3"
}

echo "::group::A  debuginfo=2 (current)"; run /swarm/scripts/build_so.measure "DBG2" /tmp/so_dbg2.so; echo "::endgroup::"
echo "::group::B  debuginfo=0 (fix)";     run /swarm/scripts/build_so.debug0  "DBG0" /tmp/so_dbg0.so; echo "::endgroup::"

echo "==================== DEBUGINFO EXPERIMENT ===================="
# Correctness: strip debug from both, then compare the CODE via the symbol+size
# table (link-order-independent — factors out the parallel-link non-determinism).
codehash() { strip --strip-debug "$1" 2>/dev/null || true
  nm --print-size --defined-only "$1" 2>/dev/null | awk '{print $2, $4}' | sort | sha256sum | cut -c1-16; }
H2=$(codehash /tmp/so_dbg2.so); H0=$(codehash /tmp/so_dbg0.so)
echo "DBG2 stripped .so=$(ls -lh /tmp/so_dbg2.so|awk '{print $5}')  code(sym+size) sha=$H2"
echo "DBG0 stripped .so=$(ls -lh /tmp/so_dbg0.so|awk '{print $5}')  code(sym+size) sha=$H0"
[ "$H2" = "$H0" ] && echo "CODE IDENTICAL: debuginfo=0 preserves the compiled code (safe)." \
                  || echo "CODE DIFFERS: investigate before trusting debuginfo=0."
echo "VERDICT: fix is real if DBG0 IR << DBG2 IR, DBG0 time < DBG2 time, and code identical."
echo "============================================================="
