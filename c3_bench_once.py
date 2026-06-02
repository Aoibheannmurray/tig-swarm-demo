#!/usr/bin/env python3
"""One-off: benchmark the current on-disk algorithm on C3 — no LLM iteration.

Stages src/<challenge>/algorithm/{mod.rs,kernels.cu} exactly as they sit on
disk, uploads to C3, runs scripts/benchmark.py on the configured GPU, and
prints the resulting benchmark.json. Reads connection/challenge/tracks from
.swarm-cache.json (same file the fleet uses).
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from c3_compute import run_benchmark_c3  # noqa: E402

config = json.loads((ROOT / ".swarm-cache.json").read_text())
server = config["server_url"]

# Optional track override: `python3 c3_bench_once.py <track_key> <count>`
# (e.g. n_hidden=7 2). Keeps the cache's seed; leaves .swarm-cache.json on
# disk untouched — only the staged copy uploaded to C3 sees the override.
if len(sys.argv) >= 2:
    track_key = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) >= 3 else 2
    seed = config.get("tracks", {}).get("seed", "test")
    config["tracks"] = {"seed": seed, track_key: count}

args = types.SimpleNamespace(
    c3_api_key=None,            # falls back to C3_API_KEY env / `c3 login`
    hardware=config.get("c3_hardware", "l40"),
    c3_time="02:00:00",        # walltime ceiling (build + run); plenty
    c3_provider=None,
    env=None,                  # None -> default CUDA cudnn-devel GPU image
)

print(f"Submitting {config['challenge']} benchmark to C3 ({args.hardware}, "
      f"tracks={config.get('tracks')})...")
bench, err = run_benchmark_c3(args, config, server)
if bench is None:
    print(err, file=sys.stderr)
    sys.exit(1)

print(json.dumps(bench, indent=2))
print(f"\nscore={bench.get('score')}  feasible={bench.get('feasible')}")
