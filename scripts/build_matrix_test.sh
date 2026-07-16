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
def fuel(p):
    if not os.path.exists(p): return None
    return [n.get('fuel_consumed') for n in (json.load(open(p)).get('nonces') or [])]
times={}; [times.__setitem__(*l.split()) for l in open('/tmp/times') if len(l.split())==2]
hashes={}; [hashes.__setitem__(*l.split()) for l in open('/tmp/hashes') if len(l.split())==2]
o,o2,d,s=(fuel(f'/tmp/{x}.json') for x in ('OFFICIAL','OFFICIAL2','DEBUG0','LLSPLIT'))
print(f"challenge={ch}  arch={arch}  difficulty={os.environ.get('DIFFICULTY')}  nonces={os.environ.get('NONCES','3')}")
print(f"build times (s): OFFICIAL={times.get('OFFICIAL','?')}  DEBUG0={times.get('DEBUG0','?')}  LLSPLIT={times.get('LLSPLIT','?')}")
try:
    ot=int(times['OFFICIAL']); print("speedup vs OFFICIAL: "+"  ".join(f"{v}={ot/int(times[v]):.2f}x" for v in ('DEBUG0','LLSPLIT') if times.get(v,'FAILED')!='FAILED'))
except Exception: pass
print(f"code_hash: OFFICIAL={hashes.get('OFFICIAL','-')}  DEBUG0={hashes.get('DEBUG0','-')}  LLSPLIT={hashes.get('LLSPLIT','-')}")
oh=hashes.get('OFFICIAL')
code_ok={v:(hashes.get(v)==oh and oh is not None) for v in ('DEBUG0','LLSPLIT')}
if arch=='amd64':
    print(f"(1) CODE identical to OFFICIAL:  DEBUG0={'YES' if code_ok['DEBUG0'] else 'NO'}  LLSPLIT={'YES' if code_ok['LLSPLIT'] else 'NO'}"
          + ("   => FUEL IDENTICAL" if all(code_ok.values()) else ""))
print(f"OFFICIAL fuel: {o}\nDEBUG0   fuel: {d}\nLLSPLIT  fuel: {s}")
det=(o is not None and o==o2 and all(x is not None for x in (o or [None])))
if det:
    print(f"(2) determinism: OK (OFFICIAL 2 runs agree) -> runtime fuel {'IDENTICAL' if (d==o and s==o) else 'DIFFERS'}")
else:
    print(f"(2) determinism: NON-DETERMINISTIC (OFFICIAL 2nd run: {o2}) -> runtime fuel inconclusive")
# Final verdict: prefer amd64 code-identity; else deterministic runtime fuel.
if arch=='amd64' and oh is not None and all(code_ok.values()):
    print("VERDICT: FUEL IDENTICAL (code-identical on amd64)")
elif det and d==o and s==o:
    print("VERDICT: FUEL IDENTICAL (deterministic runtime fuel)")
else:
    print("VERDICT: NOT PROVEN here — " + ("build failed" if 'FAILED' in times.values() else "non-deterministic seed / code differs; needs deterministic algo or amd64 code check"))
PY
echo "==============================================================================="
