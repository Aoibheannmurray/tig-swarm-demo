#!/usr/bin/env bash
# llvm-split fuel-neutral prototype, on amd64 (the target: x86_64 has NO default
# MachineOutliner, unlike arm64). Both arms keep codegen-units=1 (identical rustc IR);
# LLSPLIT only partitions the already-optimized tig_algorithms.ll for parallel
# instrumentation. Measures speed AND compares the stripped .so — separating CODE
# (.text, type t/T) from DATA symbols, since only code changes matter for fuel.
#   A = build_so.debug0   (cu=1, single tig_algorithms.ll)
#   B = build_so.llsplit  (cu=1, split into 16)
set -eu

CH=hypergraph
SLOT="tig-algorithms/src/${CH}/swarm_algo"
OUT="tig-algorithms/lib/${CH}/amd64/swarm_algo.so"
TGT=x86_64-unknown-linux-gnu
LLDIR="target/${TGT}/release/deps"
export CHALLENGE="$CH"
export BUILD_SO_OBJ_CACHE_DIR=""

grep -q "pub mod swarm_algo;" "tig-algorithms/src/${CH}/mod.rs" \
  || printf '\npub mod swarm_algo;\n' >> "tig-algorithms/src/${CH}/mod.rs"
mkdir -p "$SLOT"
cp /swarm/fixtures/hypergraph/mod.rs "${SLOT}/mod.rs"
cp /swarm/fixtures/hypergraph/kernels.cu "${SLOT}/kernels.cu" 2>/dev/null || true

run() {  # $1=build_so  $2=label  $3=saved.so
  rm -rf target
  install -m 0755 "$1" /usr/local/bin/tig-scripts/build_so
  local t0 t1; t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$2 BUILD FAILED"; tail -60 /tmp/m.log; return 1; }
  t1=$(date +%s)
  echo "### $2 total=$((t1-t0))s"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $2 /"
  echo "### $2 tig_algorithms .ll units: $(ls -1 ${LLDIR}/tig_algorithms*.ll 2>/dev/null | wc -l)"
  cp "$OUT" "$3" 2>/dev/null || true
}

echo "::group::A  cu=1 (current)";      run /swarm/scripts/build_so.debug0  CU1     /tmp/so_cu1.so     || true; echo "::endgroup::"
echo "::group::B  llvm-split (cu=1)";    run /swarm/scripts/build_so.llsplit LLSPLIT /tmp/so_split.so   || true; echo "::endgroup::"

echo "==================== CODE-IDENTITY (amd64, .text only) ===================="
[ -f /tmp/so_cu1.so ] && [ -f /tmp/so_split.so ] || { echo "missing a .so, cannot compare"; exit 0; }
strip --strip-debug /tmp/so_cu1.so 2>/dev/null || true; strip --strip-debug /tmp/so_split.so 2>/dev/null || true
# CODE = text symbols (nm type t/T); DATA = the rest (r/R/d/D/b/B ...)
code() { nm --print-size --defined-only "$1" 2>/dev/null | awk '$3=="t"||$3=="T"{print $4, $2}' | sort; }
data() { nm --print-size --defined-only "$1" 2>/dev/null | awk '$3!="t"&&$3!="T"{print $4, $2}' | sort; }
code /tmp/so_cu1.so >/tmp/c1.code; code /tmp/so_split.so >/tmp/c2.code
echo "CODE symbols: CU1=$(wc -l </tmp/c1.code)  LLSPLIT=$(wc -l </tmp/c2.code)"
echo "DATA symbols: CU1=$(data /tmp/so_cu1.so|wc -l)  LLSPLIT=$(data /tmp/so_split.so|wc -l)"
CH1=$(cut -d' ' -f2 /tmp/c1.code|sort|sha256sum|cut -c1-16); CH2=$(cut -d' ' -f2 /tmp/c2.code|sort|sha256sum|cut -c1-16)
echo "CODE size-multiset hash: CU1=$CH1  LLSPLIT=$CH2  -> $([ "$CH1" = "$CH2" ] && echo SAME || echo DIFFERENT)"
echo "CODE symbols only-in-CU1=$(comm -23 <(cut -d' ' -f1 /tmp/c1.code) <(cut -d' ' -f1 /tmp/c2.code)|wc -l)  only-in-LLSPLIT=$(comm -13 <(cut -d' ' -f1 /tmp/c1.code) <(cut -d' ' -f1 /tmp/c2.code)|wc -l)"
echo "CODE shared-name DIFFERENT size: $(join /tmp/c1.code /tmp/c2.code|awk '$2!=$3'|wc -l)"
echo "  examples (name cu1 llsplit):"; join /tmp/c1.code /tmp/c2.code|awk '$2!=$3{print "    "$1,$2,$3}'|head -6
echo "  only-in-LLSPLIT code names:"; comm -13 <(cut -d' ' -f1 /tmp/c1.code) <(cut -d' ' -f1 /tmp/c2.code)|head -6|sed 's/^/    /'
echo "VERDICT: if CODE size-multiset SAME and no only-in/size-diff, the executed code is"
echo "         identical -> fuel identical (data-only differences are fuel-irrelevant)."
echo "=========================================================================="
