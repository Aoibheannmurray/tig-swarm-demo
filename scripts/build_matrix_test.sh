#!/usr/bin/env bash
# Build-variant matrix for ONE challenge, run inside that challenge's dev image
# (monorepo at /work, swarm repo at /swarm). Builds the seed 3 ways, measures build
# time, and compares RUNTIME FUEL across builds — the arch-independent instrument
# (on arm64 the MachineOutliner perturbs .text downstream of fuel metering, so a code
# hash is unreliable there; runtime fuel is not).
#   OFFICIAL = image's stock build_so   (debuginfo=2, codegen-units=1)
#   DEBUG0   = scripts/build_so.debug0    (debuginfo=0, codegen-units=1)
#   LLSPLIT  = scripts/build_so.llsplit   (debuginfo=0, llvm-split parallel)
#
# Determinism self-check: OFFICIAL is solved TWICE. Fuel can only be meaningfully
# compared across builds if the algorithm is deterministic (many seeds are NOT: std
# HashMap iteration order, time-bounded search). If OFFICIAL's two runs disagree, the
# result is INCONCLUSIVE for that challenge (algo noise, not a build effect).
#
# Env: CHALLENGE, SEED (path under /swarm), DIFFICULTY (Track key=val), NONCES.
set -uo pipefail
: "${CHALLENGE:?}"; : "${SEED:?}"; : "${DIFFICULTY:?}"; NONCES="${NONCES:-3}"
export CHALLENGE BUILD_SO_OBJ_CACHE_DIR=""
cd /work
mkdir -p "tig-algorithms/src/${CHALLENGE}"
cp "/swarm/${SEED}" "tig-algorithms/src/${CHALLENGE}/matrixalgo.rs"
if [ -f "/swarm/${SEED%.rs}.cu" ]; then
  mkdir -p "tig-algorithms/src/${CHALLENGE}/matrixalgo"
  mv "tig-algorithms/src/${CHALLENGE}/matrixalgo.rs" "tig-algorithms/src/${CHALLENGE}/matrixalgo/mod.rs"
  cp "/swarm/${SEED%.rs}.cu" "tig-algorithms/src/${CHALLENGE}/matrixalgo/kernels.cu"
fi
grep -q "pub mod matrixalgo;" "tig-algorithms/src/${CHALLENGE}/mod.rs" \
  || printf '\npub mod matrixalgo;\n' >> "tig-algorithms/src/${CHALLENGE}/mod.rs"

STOCK=/usr/local/bin/tig-scripts/build_so
cp "$STOCK" /tmp/build_so.official

solve() { # $1=out.json
  python3 /swarm/scripts/modified_test_algorithm matrixalgo "$DIFFICULTY" null \
    --seed test --start 0 --nonces "$NONCES" --fuel 5000000000000 --workers 1 \
    --output-json "$1" >/dev/null 2>&1 || true
}
variant() {  # $1=label $2=build_so ; echo "label buildsecs"
  cd /work; rm -rf target
  install -m0755 "$2" "$STOCK"
  local t0 t1; t0=$(date +%s)
  if ! build_algorithm matrixalgo >"/tmp/b_$1.log" 2>&1; then echo "$1 FAILED"; return 0; fi
  t1=$(date +%s); echo "$1 $((t1-t0))"
  solve "/tmp/$1.json"
}

{ variant OFFICIAL /tmp/build_so.official
  variant DEBUG0   /swarm/scripts/build_so.debug0
  variant LLSPLIT  /swarm/scripts/build_so.llsplit ; } >/tmp/times
solve /tmp/OFFICIAL2.json   # determinism self-check (2nd run of OFFICIAL build)

echo "==================== ${CHALLENGE}: build + fuel matrix ===================="
python3 - "$CHALLENGE" <<'PY'
import json,sys,os
ch=sys.argv[1]
def fuel(p):
    if not os.path.exists(p): return None
    d=json.load(open(p))
    return [n.get('fuel_consumed') for n in (d.get('nonces') or [])]
times={}
for l in open('/tmp/times'):
    p=l.split()
    if len(p)==2: times[p[0]]=p[1]
o,o2=fuel('/tmp/OFFICIAL.json'),fuel('/tmp/OFFICIAL2.json')
d,s=fuel('/tmp/DEBUG0.json'),fuel('/tmp/LLSPLIT.json')
print(f"challenge={ch}  arch={os.uname().machine}  difficulty={os.environ.get('DIFFICULTY')}  nonces={os.environ.get('NONCES','3')}")
print(f"build times (s): OFFICIAL={times.get('OFFICIAL','?')}  DEBUG0={times.get('DEBUG0','?')}  LLSPLIT={times.get('LLSPLIT','?')}")
try:
    ot=int(times['OFFICIAL']); print(f"speedup vs OFFICIAL: DEBUG0={ot/int(times['DEBUG0']):.2f}x  LLSPLIT={ot/int(times['LLSPLIT']):.2f}x")
except Exception: pass
print(f"OFFICIAL fuel: {o}\nDEBUG0   fuel: {d}\nLLSPLIT  fuel: {s}")
deterministic = (o is not None and o==o2 and all(x is not None for x in (o or [None])))
if not deterministic:
    print(f"OFFICIAL 2nd run: {o2}")
    print("VERDICT: INCONCLUSIVE — algorithm is NON-DETERMINISTIC (fuel varies run-to-run); build-neutrality can't be shown via runtime fuel for this seed.")
else:
    ok = (d==o and s==o)
    print(f"VERDICT: {'FUEL IDENTICAL across OFFICIAL/DEBUG0/LLSPLIT (deterministic algo)' if ok else 'FUEL DIFFERS — investigate'}")
PY
echo "=========================================================================="
