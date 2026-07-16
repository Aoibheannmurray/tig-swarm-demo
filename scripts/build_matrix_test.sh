#!/usr/bin/env bash
# Build-variant matrix for ONE challenge, run inside that challenge's dev image
# (monorepo at /work, swarm repo at /swarm). Builds the seed 3 ways and, for each,
# measures build time, hashes the stripped .so's CODE, and solves the same nonces.
#   OFFICIAL = image's stock build_so   (debuginfo=2, codegen-units=1)
#   DEBUG0   = scripts/build_so.debug0    (debuginfo=0, codegen-units=1)
#   LLSPLIT  = scripts/build_so.llsplit   (debuginfo=0, llvm-split parallel)
#
# Two independent fuel-identity signals:
#   (1) CODE identity (amd64): fuel is a pure function of executed machine code, so
#       identical stripped .text => identical fuel for EVERY input. Reliable on amd64
#       (x86_64 has no default MachineOutliner); NOT on arm64 (outliner perturbs .text
#       downstream of fuel metering).
#   (2) RUNTIME fuel, gated by a determinism self-check: OFFICIAL's .so is solved
#       TWICE (in place, before the next build overwrites it). Runtime fuel is only
#       comparable across builds if those two runs agree; otherwise the seed is
#       non-deterministic and (2) is INCONCLUSIVE (fall back to (1) on amd64).
#
# Env: CHALLENGE, SEED (path under /swarm), DIFFICULTY (Track key=val), NONCES.
set -uo pipefail
: "${CHALLENGE:?}"; : "${SEED:?}"; : "${DIFFICULTY:?}"; NONCES="${NONCES:-3}"
export CHALLENGE BUILD_SO_OBJ_CACHE_DIR=""
cd /work
ARCH=$(uname -m); [ "$ARCH" = "aarch64" ] && A=arm64 || A=amd64
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
SO="tig-algorithms/lib/${CHALLENGE}/${A}/matrixalgo.so"
codehash() { nm --print-size --defined-only "$1" 2>/dev/null | awk '$3=="t"||$3=="T"{print $2}' | sort | sha256sum | cut -c1-16; }
solve() { python3 /swarm/scripts/modified_test_algorithm matrixalgo "$DIFFICULTY" null \
    --seed test --start 0 --nonces "$NONCES" --fuel 5000000000000 --workers 1 --output-json "$1" >/dev/null 2>&1 || true; }

: >/tmp/times; : >/tmp/hashes
variant() {  # $1=label $2=build_so
  cd /work; rm -rf target
  install -m0755 "$2" "$STOCK"
  local t0 t1; t0=$(date +%s)
  if ! build_algorithm matrixalgo >"/tmp/b_$1.log" 2>&1; then
    echo "### $1 BUILD FAILED"; tail -25 "/tmp/b_$1.log"; echo "$1 FAILED" >>/tmp/times; return 0
  fi
  t1=$(date +%s); echo "$1 $((t1-t0))" >>/tmp/times
  cp "$SO" "/tmp/$1.so"; strip --strip-debug "/tmp/$1.so" 2>/dev/null || true
  echo "$1 $(codehash "/tmp/$1.so")" >>/tmp/hashes
  solve "/tmp/$1.json"
  [ "$1" = OFFICIAL ] && solve /tmp/OFFICIAL2.json   # determinism: 2nd solve of SAME .so
}

variant OFFICIAL /tmp/build_so.official
variant DEBUG0   /swarm/scripts/build_so.debug0
variant LLSPLIT  /swarm/scripts/build_so.llsplit

echo "==================== ${CHALLENGE} (${A}): build + fuel matrix ===================="
python3 - "$CHALLENGE" "$A" <<'PY'
import json,sys,os
ch,arch=sys.argv[1],sys.argv[2]
TOL=float(os.environ.get('FUEL_TOL_PCT','2.0'))   # acceptable fuel drift vs official (%)
def fuel(p):
    if not os.path.exists(p): return None
    return [n.get('fuel_consumed') for n in (json.load(open(p)).get('nonces') or [])]
times={}; [times.__setitem__(*l.split()) for l in open('/tmp/times') if len(l.split())==2]
o,o2,d,s=(fuel(f'/tmp/{x}.json') for x in ('OFFICIAL','OFFICIAL2','DEBUG0','LLSPLIT'))
# did LLSPLIT actually split, or hit the TLS-safe fallback?
try: llmode=[l.strip() for l in open('/tmp/b_LLSPLIT.log') if '[phase] llsplit' in l][-1]
except Exception: llmode='(no llsplit phase line)'
print(f"challenge={ch}  arch={arch}  difficulty={os.environ.get('DIFFICULTY')}  nonces={os.environ.get('NONCES','3')}  tol={TOL}%")
print(f"build times (s): OFFICIAL={times.get('OFFICIAL','?')}  DEBUG0={times.get('DEBUG0','?')}  LLSPLIT={times.get('LLSPLIT','?')}")
try:
    ot=int(times['OFFICIAL']); print("speedup vs OFFICIAL: "+"  ".join(f"{v}={ot/int(times[v]):.2f}x" for v in ('DEBUG0','LLSPLIT') if times.get(v,'FAILED')!='FAILED'))
except Exception: pass
print(f"llsplit mode: {llmode}")
print(f"OFFICIAL fuel: {o}\nDEBUG0   fuel: {d}\nLLSPLIT  fuel: {s}")
def maxpct(a,b):
    if not a or not b or len(a)!=len(b) or any(x is None for x in a+b): return None
    return max(abs(x-y)/y*100.0 for x,y in zip(b,a))   # % drift of b vs baseline a
det=(o is not None and o==o2 and all(x is not None for x in (o or [None])))
if det:
    dd,ss=maxpct(o,d),maxpct(o,s)
    print(f"(determinism OK) max fuel drift vs OFFICIAL:  DEBUG0={dd:.3f}%  LLSPLIT={('%.3f%%'%ss) if ss is not None else 'n/a(build failed)'}")
    within=(dd is not None and dd<=TOL) and (ss is None or ss<=TOL)
    built=('FAILED' not in times.values())
    if built and within: print(f"VERDICT: PASS — build succeeds, fuel within {TOL}% of official")
    elif not built:      print("VERDICT: FAIL — a build failed (see log above)")
    else:                print(f"VERDICT: FAIL — fuel drift exceeds {TOL}%")
else:
    noise=maxpct(o,o2)
    print(f"(determinism) NON-DETERMINISTIC — OFFICIAL run-to-run noise {('%.3f%%'%noise) if noise is not None else '?'} (2nd run {o2}); fuel comparison inconclusive for this seed")
    print("VERDICT: " + ("BUILD OK, fuel inconclusive (non-deterministic seed)" if 'FAILED' not in times.values() else "FAIL — a build failed"))
PY
echo "==============================================================================="
