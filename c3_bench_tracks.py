#!/usr/bin/env python3
"""One-off: benchmark the on-disk algorithm on C3 across ALL TIG tracks.

Like c3_bench_once.py, but overrides the track set to every mainnet track
(n_hidden=4/7/10/14/18) with a fixed per-track instance count, and saves the
full benchmark.json to a result file. Never publishes.

Usage: python3 c3_bench_tracks.py <count_per_track> <out_json> [walltime]
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from c3_compute import run_benchmark_c3  # noqa: E402

count = int(sys.argv[1]) if len(sys.argv) >= 2 else 200
out_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else ROOT / "reports" / "bench_tracks.json"
walltime = sys.argv[3] if len(sys.argv) >= 4 else "05:00:00"

config = json.loads((ROOT / ".swarm-cache.json").read_text())
server = config["server_url"]
seed = config.get("tracks", {}).get("seed", "test")
config["tracks"] = {
    "seed": seed,
    "n_hidden=4": count,
    "n_hidden=7": count,
    "n_hidden=10": count,
    "n_hidden=14": count,
    "n_hidden=18": count,
}

args = types.SimpleNamespace(
    c3_api_key=None,           # falls back to C3_API_KEY env / `c3 login`
    hardware=config.get("c3_hardware", "l40"),
    c3_time=walltime,
    c3_provider=None,
    env=None,
)

print(f"Submitting {config['challenge']} benchmark to C3 ({args.hardware}, "
      f"tracks={config['tracks']}, walltime={walltime})...", flush=True)
bench, err = run_benchmark_c3(args, config, server)
if bench is None:
    print(err, file=sys.stderr)
    sys.exit(1)

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(bench, indent=2))
print(f"wrote {out_path}")
print(f"score={bench.get('score')}  feasible={bench.get('feasible')}  "
      f"track_scores={bench.get('track_scores')}")
