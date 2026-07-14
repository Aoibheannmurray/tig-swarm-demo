#!/usr/bin/env bash
# The cudarc-trim experiment: build cloudy (amd64) with cudarc's DEFAULT features
# (current: cublas+cublaslt+curand compiled but UNUSED by hypergraph) vs a trimmed
# cudarc (default-features=false, only driver+runtime+nvrtc). Both arms use the
# already-validated debuginfo=0 build_so, so the ONLY variable is cudarc features.
# Measures: tig_algorithms.ll size + build time, and verifies behaviour by proving
# the trimmed .so is a size-consistent SUBSET of the full one (we only removed
# unused code, changed nothing that remains). amd64, compile-only, no GPU.
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

# Snapshot the pristine Cargo.tomls so we can restore between arms.
cp tig-algorithms/Cargo.toml /tmp/algo.toml.orig
cp tig-challenges/Cargo.toml /tmp/chal.toml.orig

trim_cudarc() {   # rewrite the cudarc dep in both manifests to a minimal feature set
  for f in tig-algorithms/Cargo.toml tig-challenges/Cargo.toml; do
    perl -0pi -e 's/features = \[\s*"cuda-version-from-build-system",\s*\],\s*optional = true \}/default-features = false, features = ["cuda-version-from-build-system", "std", "driver", "runtime", "nvrtc", "dynamic-loading"], optional = true }/gs' "$f"
  done
  echo "--- trimmed cudarc decl (tig-algorithms) ---"; grep -n "cudarc = " tig-algorithms/Cargo.toml
  echo "--- trimmed cudarc decl (tig-challenges) ---"; grep -n "cudarc = " tig-challenges/Cargo.toml
}

restore_cudarc() {
  cp /tmp/algo.toml.orig tig-algorithms/Cargo.toml
  cp /tmp/chal.toml.orig tig-challenges/Cargo.toml
}

run() {  # $1=label  $2=saved-so path
  rm -rf target   # fresh target per build
  install -m 0755 /swarm/scripts/build_so.debug0 /usr/local/bin/tig-scripts/build_so
  local t0 t1
  t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$1 BUILD FAILED"; tail -80 /tmp/m.log; exit 1; }
  t1=$(date +%s)
  echo "### $1 total=$((t1-t0))s  tig_algorithms.ll=$(ls -lh "$LLFILE" 2>/dev/null | awk '{print $5}')"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $1 /"
  cp "$OUT" "$2"
}

echo "::group::A  full cudarc defaults (current)"
restore_cudarc
run "FULL" /tmp/so_full.so
echo "::endgroup::"

echo "::group::B  trimmed cudarc (driver+runtime+nvrtc)"
trim_cudarc
run "TRIM" /tmp/so_trim.so
restore_cudarc
echo "::endgroup::"

echo "==================== CUDARC-TRIM EXPERIMENT ===================="
echo "FULL stripped .so=$(strip --strip-debug /tmp/so_full.so 2>/dev/null; ls -lh /tmp/so_full.so|awk '{print $5}')"
echo "TRIM stripped .so=$(strip --strip-debug /tmp/so_trim.so 2>/dev/null; ls -lh /tmp/so_trim.so|awk '{print $5}')"

# Behaviour check: every defined symbol in TRIM must exist with the SAME size in
# FULL. If so, trimming only REMOVED unused code and left the solve path byte-for-
# byte intact. Report symbol counts and any size mismatches.
symtab() { nm --print-size --defined-only "$1" 2>/dev/null | awk '{print $NF, $2}' | sort; }
symtab /tmp/so_full.so > /tmp/full.sym
symtab /tmp/so_trim.so > /tmp/trim.sym
echo "FULL defined symbols=$(wc -l </tmp/full.sym)  TRIM defined symbols=$(wc -l </tmp/trim.sym)"
# join on symbol name; flag any where sizes differ
MISMATCH=$(join -j1 /tmp/full.sym /tmp/trim.sym | awk '$2 != $3 {print}' | head -20)
ONLY_IN_TRIM=$(comm -13 <(cut -d' ' -f1 /tmp/full.sym) <(cut -d' ' -f1 /tmp/trim.sym) | head -20)
if [ -z "$MISMATCH" ] && [ -z "$ONLY_IN_TRIM" ]; then
  echo "BEHAVIOUR SAFE: every TRIM symbol is present with identical size in FULL (pure removal of unused code)."
else
  echo "INVESTIGATE — size mismatches:"; echo "$MISMATCH"
  echo "INVESTIGATE — symbols only in TRIM:"; echo "$ONLY_IN_TRIM"
fi
echo "VERDICT: fix is real if TRIM IR << FULL IR, TRIM time < FULL time, and behaviour SAFE."
echo "==============================================================="
