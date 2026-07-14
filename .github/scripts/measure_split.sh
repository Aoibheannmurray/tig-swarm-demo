#!/usr/bin/env bash
# Per-package codegen-units. We PROVED the whole loop is one 302s file: tig_algorithms
# at codegen-units=1. A blanket codegen-units=16 fails to link because the fuel pass
# defines crate-level singletons (__fuel_remaining, __check_fuel, ...) in the "first"
# crate (tig_challenges), which then collide when tig_challenges is split. This arm
# splits ONLY tig_algorithms (the hog) via a Cargo per-package profile override,
# keeping tig_challenges (and everything else) at one unit so the fuel singletons stay
# unique. Expectation: tig_algorithms fans out into ~16 .ll units processed in parallel,
# collapsing the loop, and it LINKS. amd64, compile-only, no GPU (fuel-identity is a
# later GPU gate). A = current (cu=1 everywhere), B = tig_algorithms cu=16.
set -eu   # NOT pipefail: head on a log pipe legitimately SIGPIPEs the producer.

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

cp Cargo.toml /tmp/root.toml.orig

add_pkg_override() {   # split ONLY tig-algorithms; global stays cu=1 from [profile.release]
  cat >> Cargo.toml <<'EOF'

[profile.release.package.tig-algorithms]
codegen-units = 16
EOF
  echo "--- appended per-package override ---"; tail -4 Cargo.toml
}
restore_root() { cp /tmp/root.toml.orig Cargo.toml; }

run() {  # $1=build_so variant  $2=label  $3=saved-so path
  rm -rf target
  install -m 0755 "$1" /usr/local/bin/tig-scripts/build_so
  local t0 t1
  t0=$(date +%s)
  build_algorithm swarm_algo >/tmp/m.log 2>&1 || { echo "$2 BUILD FAILED"; tail -60 /tmp/m.log; return 1; }
  t1=$(date +%s)
  echo "### $2 total=$((t1-t0))s"
  grep -E '^\[phase\]' /tmp/m.log | sed "s/^/### $2 /"
  echo "### $2 tig_algorithms .ll units: $(ls -1 ${LLDIR}/tig_algorithms*.ll 2>/dev/null | wc -l)  bytes=$(cat ${LLDIR}/tig_algorithms*.ll 2>/dev/null | wc -c | awk '{printf "%.0fMB", $1/1048576}')"
  echo "### $2 slowest [file] steps:"; grep -E '^\[file\]' /tmp/m.log | sort -k2 -rn | head -8 | sed "s/^/###   /"
  cp "$OUT" "$3" 2>/dev/null || true
}

echo "::group::A  codegen-units=1 everywhere (current)"
restore_root
run /swarm/scripts/build_so.debug0 "CU1" /tmp/so_cu1.so || true
echo "::endgroup::"

echo "::group::B  tig_algorithms codegen-units=16 (per-package)"
add_pkg_override
run /swarm/scripts/build_so.cgupkg "PKG16" /tmp/so_pkg16.so || true
restore_root
echo "::endgroup::"

echo "==================== PER-PACKAGE CODEGEN-UNITS ===================="
echo "CU1   stripped .so=$( [ -f /tmp/so_cu1.so ]   && { strip --strip-debug /tmp/so_cu1.so 2>/dev/null;   ls -lh /tmp/so_cu1.so|awk '{print $5}'; }   || echo n/a )"
echo "PKG16 stripped .so=$( [ -f /tmp/so_pkg16.so ] && { strip --strip-debug /tmp/so_pkg16.so 2>/dev/null; ls -lh /tmp/so_pkg16.so|awk '{print $5}'; } || echo n/a )"
echo "VERDICT: win if PKG16 BUILDS, tig_algorithms fans into multiple .ll units, and"
echo "         loop/total << CU1. Fuel-identity then confirmed on GPU before shipping."
echo "=================================================================="
