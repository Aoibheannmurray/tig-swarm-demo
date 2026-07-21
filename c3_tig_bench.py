#!/usr/bin/env python3
"""Benchmark tig-monorepo algorithms on C3 GPUs using the real TIG harness.

Why not `docker run ghcr.io/tig-foundation/tig-monorepo/<challenge>/dev`?
C3 jobs cannot pull GHCR images (Docker Hub only, see `c3 docs`), and there
is no docker-in-docker on C3 nodes. Instead this stages a minimal
tig-monorepo workspace, runs it inside nvidia/cuda (Docker Hub), and
reconstructs the dev image's toolchain at job start (apt + rustup
nightly-2025-02-10 + TIG LLVM + tig-runtime/tig-verifier), then uses the
monorepo's own build_algorithm / download_algorithm scripts and a JSON
variant of test_algorithm.

Usage:
  python3 c3_tig_bench.py --build cautious_adanw --download neural_extrem_v3 \
      --tracks n_hidden=4 --nonces 100 --seed test --walltime 01:30:00 \
      --out reports/tig_bench_n4.json

Build-time rollout canary:
  python3 c3_tig_bench.py --challenge hypergraph --build <algorithm> \
      --build-so optimized --tracks <track> --nonces 4 \
      --out reports/buildso_optimized.json

Needs a local tig-monorepo checkout: pass --monorepo, set $TIG_MONOREPO, or
clone it at ./tig-monorepo next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# tig-monorepo checkout to stage from: --monorepo > $TIG_MONOREPO > ./tig-monorepo.
DEFAULT_MONOREPO = Path(__file__).resolve().parent / "tig-monorepo"
OPTIMIZED_BUILD_SO = Path(__file__).resolve().parent / "scripts" / "build_so.llsplit"
IMAGE = "nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04"
POLL_SECS = 20

# Mainnet challenge ids (stable; from get-challenges). Used to build the
# tig-runtime/tig-verifier `settings` JSON. Override with --challenge-id.
CHALLENGE_IDS = {
    "satisfiability": "c001",
    "vehicle_routing": "c002",
    "knapsack": "c003",
    "vector_search": "c004",
    "hypergraph": "c005",
    "neuralnet_optimizer": "c006",
    "job_scheduling": "c007",
    "energy_arbitrage": "c008",
}

WORKSPACE_CRATES = [
    "tig-algorithms",
    "tig-binary",
    "tig-challenges",
    "tig-protocol",
    "tig-runtime",
    "tig-structs",
    "tig-utils",
    "tig-verifier",
]


def lf_write(path: Path, text: str, exe: bool = False) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    if exe:
        path.chmod(0o755)


def stage_workspace(stage: Path, monorepo: Path,
                    optimized_build_so: Path | None = None) -> None:
    for name in ("Cargo.toml", "Cargo.lock"):
        shutil.copy2(monorepo / name, stage / name)
    ignore = shutil.ignore_patterns(
        "target", "lib", "__pycache__", "*.pyc", ".git", "dist", "test_results"
    )
    for crate in WORKSPACE_CRATES:
        shutil.copytree(monorepo / crate, stage / crate, ignore=ignore)
    # build_so expects tig-binary/scripts and monorepo scripts
    shutil.copytree(monorepo / "scripts", stage / "scripts")
    if (monorepo / "tig-binary" / "scripts").exists():
        pass  # already copied with the crate
    # hyperparameters.py is imported by some monorepo scripts; copy if present
    for extra in ("hyperparameters.py",):
        src = monorepo / extra
        if src.exists():
            shutil.copy2(src, stage / extra)
    if optimized_build_so is not None:
        shutil.copy2(optimized_build_so, stage / "build_so.llsplit")


BENCH_PY = r'''#!/usr/bin/env python3
"""Run tig-runtime + tig-verifier per nonce, emit JSON results to stdout."""
import argparse, json, os, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

p = argparse.ArgumentParser()
p.add_argument("--algorithms", required=True, help="comma-separated algorithm names")
p.add_argument("--tracks", required=True, help="comma-separated track ids e.g. n_hidden=4")
p.add_argument("--nonces", type=int, default=100)
p.add_argument("--start", type=int, default=0)
p.add_argument("--seed", default="test")
p.add_argument("--fuel", type=int, default=int(100e9))
p.add_argument("--workers", type=int, default=3)
p.add_argument("--lib-dir", default="./tig-algorithms/lib")
p.add_argument("--configs", default=None,
               help="JSON file: list of {label, algorithm, hyperparameters} sweep entries")
args = p.parse_args()

CHALLENGE = os.environ["CHALLENGE"]
CHALLENGE_ID = os.environ["CHALLENGE_ID"]
rows = []

if args.configs:
    configs = json.load(open(args.configs))
else:
    configs = [{"label": a, "algorithm": a, "hyperparameters": None}
               for a in args.algorithms.split(",")]

def run_one(cfg, track, nonce):
    algo, label, hp = cfg["algorithm"], cfg["label"], cfg.get("hyperparameters")
    so = f"{args.lib_dir}/{CHALLENGE}/amd64/{algo}.so"
    ptx = f"{args.lib_dir}/{CHALLENGE}/ptx/{algo}.ptx"
    settings = json.dumps({"algorithm_id": "", "challenge_id": CHALLENGE_ID,
                           "track_id": track, "block_id": "", "player_id": ""},
                          separators=(",", ":"))
    cmd = ["tig-runtime", settings, args.seed, str(nonce), so,
           "--fuel", str(args.fuel), "--output", None,
           "--ptx", ptx, "--gpu", "0"]
    with tempfile.TemporaryDirectory() as td:
        cmd[cmd.index(None)] = td
        if hp:
            cmd += ["--hyperparameters", json.dumps(hp, separators=(",", ":"))]
        t0 = time.time()
        r1 = subprocess.run(cmd, capture_output=True, text=True)
        t1 = time.time()
        out_file = f"{td}/{nonce}.json"
        r2 = subprocess.run(["tig-verifier", settings, args.seed, str(nonce),
                             out_file, "--ptx", ptx, "--gpu", "0"],
                            capture_output=True, text=True)
        t2 = time.time()
        try:
            runtime_data = json.load(open(out_file))
        except (OSError, json.JSONDecodeError):
            runtime_data = {}
    quality = None
    for line in (r2.stdout or "").strip().splitlines()[::-1]:
        if line.startswith("quality: "):
            quality = int(line[len("quality: "):])
            break
    row = {"algorithm": algo, "label": label, "track": track, "nonce": nonce,
           "runtime_secs": round(t1 - t0, 3), "verify_secs": round(t2 - t1, 3),
           "runtime_exit": r1.returncode, "verifier_exit": r2.returncode,
           "ok": r2.returncode == 0 and quality is not None, "quality": quality,
           "fuel_consumed": runtime_data.get("fuel_consumed"),
           "runtime_signature": runtime_data.get("runtime_signature")}
    if r1.returncode == 87:
        row["error"] = "out of fuel"
    elif r1.returncode != 0:
        row["error"] = (r1.stderr or "")[-300:]
    elif r2.returncode != 0:
        row["error"] = (r2.stderr or "")[-300:]
    rows.append(row)
    done = len(rows)
    print(f"[{done}] {label} {track} nonce={nonce} ok={row['ok']} "
          f"quality={quality} rt={row['runtime_secs']}s", file=sys.stderr, flush=True)

jobs = [(c, t, n)
        for c in configs
        for t in args.tracks.split(",")
        for n in range(args.start, args.start + args.nonces)]
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    list(pool.map(lambda j: run_one(*j), jobs))

summary = {}
for c in configs:
    for t in args.tracks.split(","):
        sel = [r for r in rows if r["label"] == c["label"] and r["track"] == t]
        oks = [r["quality"] for r in sel if r["ok"]]
        summary[f"{c['label']}|{t}"] = {
            "n": len(sel), "n_ok": len(oks),
            "mean_quality": sum(oks) / len(oks) if oks else None,
            "min_quality": min(oks) if oks else None,
            "max_quality": max(oks) if oks else None,
            "mean_runtime_secs": round(sum(r["runtime_secs"] for r in sel) / len(sel), 3) if sel else None,
        }
print(json.dumps({"seed": args.seed, "fuel": args.fuel, "summary": summary,
                  "results": rows}, indent=1))
'''


def runner_script(build_algos: list[str], download_algos: list[str],
                  tracks: str, nonces: int, seed: str, fuel: int,
                  workers: int, challenge: str, challenge_id: str,
                  sweep: bool = False,
                  binaries_cache: bool = False) -> str:
    build_lines = "\n".join(
        f'''log "build_algorithm {a}"
build_start=$(date +%s)
bash tig-binary/scripts/build_algorithm {a} 2>&1 | tee "c3-artifacts/build-{a}.log"
echo "{a} $(( $(date +%s) - build_start ))" | tee -a c3-artifacts/build-times.txt'''
        for a in build_algos
    )
    dl_lines = "\n".join(
        f'log "download_algorithm {a}"\npython3 scripts/download_algorithm {a}'
        for a in download_algos
    )
    algos = ",".join(build_algos + download_algos)
    configs_arg = " --configs configs.json" if sweep else ""
    if binaries_cache:
        harness_build = """\
log "restore cached tig-runtime + tig-verifier"
tar -C /usr/local/bin --zstd -xf tig-binaries.tar.zst
chmod +x /usr/local/bin/tig-runtime /usr/local/bin/tig-verifier"""
    else:
        harness_build = """\
log "build tig-runtime + tig-verifier"
cargo build -r -p tig-runtime --features $CHALLENGE
cargo build -r -p tig-verifier --features $CHALLENGE
cp target/release/tig-runtime target/release/tig-verifier /usr/local/bin/"""
    return f"""\
#!/bin/bash
set -euo pipefail
cd "${{C3_JOB_WORKDIR:-/workspace}}"
mkdir -p "${{C3_ARTIFACTS_DIR}}" c3-artifacts
T0=$(date +%s)
log() {{ echo "[+$(($(date +%s)-T0))s] $*"; }}

log "apt install"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates build-essential zstd python3 python3-pip pkg-config libssl-dev

PIP_ROOT_USER_ACTION=ignore python3 -m pip install --break-system-packages -q blake3 requests

log "rustup nightly-2025-02-10"
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \\
  --default-toolchain nightly-2025-02-10 --component rust-src --profile minimal -q
export PATH="$HOME/.cargo/bin:$PATH"
RUST_TARGET=x86_64-unknown-linux-gnu
rustup target add $RUST_TARGET
RUST_LIBDIR=$(rustc --print target-libdir --target=$RUST_TARGET)
ln -sf "$RUST_LIBDIR" /usr/local/lib/rust
export LD_LIBRARY_PATH="${{LD_LIBRARY_PATH:-}}:/usr/local/lib/rust:$RUST_LIBDIR"

log "TIG llvm"
curl -sL "https://github.com/tig-foundation/llvm/releases/download/amd64.rc4%2B19.1.7/llvm.tar.zst" -o llvm.tar.zst
mkdir -p /opt/llvm && tar -xf llvm.tar.zst -C /opt/llvm && rm llvm.tar.zst
ln -sf /opt/llvm/bin/* /usr/local/bin/ 2>/dev/null || true

export CHALLENGE={challenge}
export CHALLENGE_ID={challenge_id}
chmod +x tig-binary/scripts/* scripts/* 2>/dev/null || true
export PATH="$PWD/tig-binary/scripts:$PWD/scripts:$PATH"

if [ -f build_so.llsplit ]; then
  log "install optimized build_so.llsplit"
  install -m 0755 build_so.llsplit tig-binary/scripts/build_so
fi

{harness_build}
log "toolchain ready"

{build_lines}
{dl_lines}
find tig-algorithms/lib -type f | tee -a "${{C3_ARTIFACTS_DIR}}/build-info.txt"

# cache the harness binaries so later jobs can skip the cargo builds
tar -C /usr/local/bin --zstd -cf "${{C3_ARTIFACTS_DIR}}/tig-binaries.tar.zst" tig-runtime tig-verifier
du -sh target 2>/dev/null | tee -a "${{C3_ARTIFACTS_DIR}}/build-info.txt" || true

log "benchmark start"
status=0
python3 bench_tig.py --algorithms {algos} --tracks "{tracks}"{configs_arg} \\
  --nonces {nonces} --seed "{seed}" --fuel {fuel} --workers {workers} \\
  > "${{C3_ARTIFACTS_DIR}}/results.json" \\
  2> "${{C3_ARTIFACTS_DIR}}/bench.stderr" || status=$?
log "benchmark done status=$status"

cp "${{C3_ARTIFACTS_DIR}}/results.json" c3-artifacts/results.json 2>/dev/null || true
cp "${{C3_ARTIFACTS_DIR}}/bench.stderr" c3-artifacts/bench.stderr 2>/dev/null || true
tail -5 "${{C3_ARTIFACTS_DIR}}/bench.stderr" || true
exit "$status"
"""


def write_c3(stage: Path, walltime: str, run_id: str, challenge: str,
             gpu: str = "l40") -> None:
    lf_write(stage / ".c3", f"""\
project: tig-bench
script: runner.sh
gpu: {gpu}
time: "{walltime}"
job_name: tig-{challenge}-{run_id}

docker:
  image: "{IMAGE}"

output:
  - ./c3-artifacts
""")


def parse_job_id(text: str) -> str | None:
    m = re.search(r"\b(job_[a-zA-Z0-9_-]+)\b", text)
    return m.group(1) if m else None


def job_status(job_id: str, cwd: Path) -> str | None:
    r = subprocess.run(["c3", "squeue", "--json"], capture_output=True,
                       text=True, cwd=cwd)
    try:
        data = json.loads(r.stdout or "null")
    except json.JSONDecodeError:
        return None

    def walk(node):
        if isinstance(node, list):
            for x in node:
                yield from walk(x)
        elif isinstance(node, dict):
            if any(k in node for k in ("id", "job_id", "jobId")):
                yield node
            for k in ("jobs", "items", "data", "results", "queue"):
                yield from walk(node.get(k))

    for job in walk(data):
        if job_id in {str(job.get(k, "")) for k in ("id", "job_id", "jobId")}:
            s = job.get("status") or job.get("state") or job.get("phase") or ""
            return str(s).strip().replace("-", "_").replace(" ", "_").upper()
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--challenge", default="neuralnet_optimizer",
                    choices=sorted(CHALLENGE_IDS),
                    help="TIG challenge to benchmark (default: neuralnet_optimizer)")
    ap.add_argument("--challenge-id", default=None,
                    help="override the mainnet challenge id (default: looked up from --challenge)")
    ap.add_argument("--build", default="", help="comma-separated local algorithms to build")
    ap.add_argument("--download", default="", help="comma-separated mainnet algorithms to download")
    ap.add_argument("--tracks", default="n_hidden=4")
    ap.add_argument("--nonces", type=int, default=100)
    ap.add_argument("--seed", default="test")
    ap.add_argument("--fuel", type=int, default=int(100e9))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--walltime", default="01:30:00")
    ap.add_argument("--gpu", default="l40")
    ap.add_argument("--out", default="reports/tig_bench.json")
    ap.add_argument("--sweep-file", default=None,
                    help="local JSON file: list of {label, algorithm, hyperparameters}")
    ap.add_argument("--binaries-cache", default=None,
                    help="local tig-binaries.tar.zst from a previous job (skips harness build)")
    ap.add_argument("--build-so", choices=("official", "optimized"),
                    default="official",
                    help="TIG build_so implementation (default: official; optimized is the rollout canary)")
    ap.add_argument("--monorepo", default=None,
                    help="path to a tig-monorepo checkout "
                         "(default: $TIG_MONOREPO, then ./tig-monorepo)")
    args = ap.parse_args()

    monorepo = Path(
        args.monorepo or os.environ.get("TIG_MONOREPO") or DEFAULT_MONOREPO
    ).expanduser()
    if not (monorepo / "Cargo.toml").is_file():
        print(
            f"tig-monorepo not found at {monorepo} — clone "
            f"https://github.com/tig-foundation/tig-monorepo there, or point "
            f"--monorepo / $TIG_MONOREPO at an existing checkout.",
            file=sys.stderr,
        )
        return 2
    if args.build_so == "optimized" and not OPTIMIZED_BUILD_SO.is_file():
        print(f"optimized build_so not found at {OPTIMIZED_BUILD_SO}", file=sys.stderr)
        return 2

    challenge = args.challenge
    challenge_id = args.challenge_id or CHALLENGE_IDS[challenge]

    build_algos = [a for a in args.build.split(",") if a]
    download_algos = [a for a in args.download.split(",") if a]
    if not build_algos and not download_algos:
        print("nothing to benchmark", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="tig-c3-") as tmp:
        stage = Path(tmp)
        stage_workspace(
            stage,
            monorepo,
            OPTIMIZED_BUILD_SO if args.build_so == "optimized" else None,
        )
        lf_write(stage / "bench_tig.py", BENCH_PY)
        if args.sweep_file:
            shutil.copy2(args.sweep_file, stage / "configs.json")
        if args.binaries_cache:
            shutil.copy2(args.binaries_cache, stage / "tig-binaries.tar.zst")
        lf_write(stage / "runner.sh",
                 runner_script(build_algos, download_algos, args.tracks,
                               args.nonces, args.seed, args.fuel, args.workers,
                               challenge, challenge_id,
                               sweep=bool(args.sweep_file),
                               binaries_cache=bool(args.binaries_cache)),
                 exe=True)
        write_c3(stage, args.walltime, run_id, challenge, args.gpu)

        print(f"[deploy] staging {sum(1 for _ in stage.rglob('*'))} files, run {run_id}")
        proc = subprocess.Popen(["c3", "deploy"], cwd=stage, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            out_lines.append(line)
            print(f"[c3] {line}", flush=True)
        proc.wait()
        if proc.returncode != 0:
            print(f"c3 deploy failed ({proc.returncode})", file=sys.stderr)
            return 1
        job_id = parse_job_id("\n".join(out_lines))
        if not job_id:
            print("could not parse job id", file=sys.stderr)
            return 1
        print(f"[poll] job {job_id}")

        terminal_ok = ("COMPLETED", "SYNCED", "SUCCEEDED", "SUCCESS")
        terminal_bad = ("FAILED", "CANCELLED", "CANCELED", "ERROR", "TIMED_OUT", "TIMEOUT")
        deadline = time.monotonic() + 3 * 3600
        final = "timeout"
        n = 0
        while time.monotonic() < deadline:
            s = job_status(job_id, stage)
            if s and any(t in s for t in terminal_ok):
                final = "completed"
                break
            if s and any(t in s for t in terminal_bad):
                final = "failed"
                break
            if n % 6 == 0:
                print(f"[poll] {job_id}: {s or 'unknown'}", flush=True)
            n += 1
            time.sleep(POLL_SECS)

        subprocess.run(["c3", "pull", job_id], cwd=stage)
        logs = subprocess.run(["c3", "logs", job_id], cwd=stage,
                              capture_output=True, text=True)
        log_text = (logs.stdout or "") + (logs.stderr or "")

        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        results = None
        for f in sorted(stage.rglob("results.json"), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            if f.stat().st_size:
                try:
                    results = json.loads(f.read_text())
                    break
                except json.JSONDecodeError:
                    continue
        stderr_text = ""
        for f in sorted(stage.rglob("bench.stderr"), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            if f.stat().st_size:
                stderr_text = f.read_text(errors="replace")
                break
        # keep the binaries cache for future fast jobs
        for f in stage.rglob("tig-binaries.tar.zst"):
            dst = out_path.parent / "tig-binaries.tar.zst"
            shutil.copy2(f, dst)
            print(f"[cache] saved {dst} ({f.stat().st_size // (1 << 20)} MB)")
            break

        if results is None:
            print(f"[{final}] no results.json; job logs tail:\n{log_text[-3000:]}")
            if stderr_text:
                print(f"bench.stderr tail:\n{stderr_text[-3000:]}")
            return 1

        results["job_id"] = job_id
        results["final_status"] = final
        results["build_so"] = args.build_so
        build_times = {}
        for f in stage.rglob("build-times.txt"):
            for line in f.read_text(errors="replace").splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1].isdigit():
                    build_times[fields[0]] = int(fields[1])
        results["build_seconds"] = build_times
        build_phases = {}
        for f in stage.rglob("build-*.log"):
            phases = {}
            for line in f.read_text(errors="replace").splitlines():
                match = re.search(r"\[phase\] ([^:]+): (\d+)s", line)
                if match:
                    phases[match.group(1)] = int(match.group(2))
            if phases:
                build_phases[f.stem.removeprefix("build-")] = phases
        results["build_phase_seconds"] = build_phases
        out_path.write_text(json.dumps(results, indent=1))
        print(f"[done] wrote {out_path}")
        for key, s in results["summary"].items():
            print(f"  {key}: n_ok={s['n_ok']}/{s['n']} mean={s['mean_quality']} "
                  f"min={s['min_quality']} max={s['max_quality']} "
                  f"rt={s['mean_runtime_secs']}s")
        return 0


if __name__ == "__main__":
    sys.exit(main())
