#!/usr/bin/env bash
# The codegen-units experiment. The instrumentation loop fans out one process per
# .ll file, but codegen-units=1 collapses tig_algorithms into ONE 295MB .ll that
# a single core must grind through -> it's the long pole in BOTH the cargo codegen
# phase and the instrumentation loop, on a many-core machine. This A/Bs the current
# codegen-units=1 (debug0) vs codegen-units=16, measuring cargo/loop/total time,
# the .ll file inventory, and surfacing per-file timings so we SEE tig_algorithms
# dominate at cu=1 and split at cu=16. amd64, compile-only, no GPU.
# NOTE: correctness (fuel identical) needs a separate GPU run; this measures speed
# and proves the mechanism first, same staged approach as the debuginfo fix.
set -eu   # NOT pipefail: `head` on a log pipe legitimately SIGPIPEs the producer.

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
OUT="tig-algorithms/lib/${CH}/amd64/swarm_algo.so"
TGT=x86_64-unknown-linux-gnu
LLDIR="target/${TGT}/release/deps"
LLFILE="${LLDIR}/tig_algorithms.ll"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=""      # original behavior + phase/file timers

grep -q "pub mod swarm_algo;" "tig-algorithms/src/${CH}/mod.rs" \
  || printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"
mkdir -p "$SLOT"
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true

run() {  # $1=build_so variant  $2=label  $3=saved-so path
  rm -rf target
  install -m 0755 "$1" /usr/local/bin/tig-scripts/build_so
  local t0 t1
  t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$2 BUILD FAILED"; tail -80 /tmp/m.log; exit 1; }
  t1=$(date +%s)
  echo "### $2 total=$((t1-t0))s"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $2 /"
  # .ll inventory: how many tig_algorithms units, and the biggest .ll files
  echo "### $2 tig_algorithms .ll units: $(ls -1 ${LLDIR}/tig_algorithms*.ll 2>/dev/null | wc -l)  total=$(cat ${LLDIR}/tig_algorithms*.ll 2>/dev/null | wc -c | awk '{printf "%.0fMB", $1/1048576}')"
  echo "### $2 biggest .ll files:"; ls -S ${LLDIR}/*.ll 2>/dev/null | head -6 | while read -r f; do echo "###   $(ls -lh "$f" | awk '{print $5}')  $(basename "$f")"; done
  # slowest per-file instrumentation timings (the long pole)
  echo "### $2 slowest [file] steps:"; grep -E '^\[file\]' /tmp/m.log | sort -k2 -rn | head -8 | sed "s/^/###   /"
  cp "$OUT" "$3" 2>/dev/null || true
}

echo "::group::A  codegen-units=1 (current)"
run /swarm/scripts/build_so.debug0 "CU1" /tmp/so_cu1.so
echo "::endgroup::"

echo "::group::B  codegen-units=16"
run /swarm/scripts/build_so.cu16 "CU16" /tmp/so_cu16.so
echo "::endgroup::"

echo "==================== CODEGEN-UNITS EXPERIMENT ===================="
echo "CU1  stripped .so=$( [ -f /tmp/so_cu1.so ] && { strip --strip-debug /tmp/so_cu1.so 2>/dev/null; ls -lh /tmp/so_cu1.so|awk '{print $5}'; } || echo n/a )"
echo "CU16 stripped .so=$( [ -f /tmp/so_cu16.so ] && { strip --strip-debug /tmp/so_cu16.so 2>/dev/null; ls -lh /tmp/so_cu16.so|awk '{print $5}'; } || echo n/a )"
echo "VERDICT: fix is real if CU16 loop/cargo/total << CU1 and it BUILDS. Fuel-identity"
echo "         must then be confirmed on GPU before shipping (codegen-units can reorder"
echo "         but should not change which instructions get fuel-counted)."
echo "================================================================="
