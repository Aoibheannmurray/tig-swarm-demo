#!/usr/bin/env python3
#
# tig_bench_driver — runs INSIDE the TIG custom image (Dockerfile.bench).
#
# The agent's algorithm is bind-mounted into the fixed `swarm_algo` slot before
# this runs. We compile it once, then run `modified_test_algorithm` per track,
# and emit ONE combined JSON on stdout that the host adapter (benchmark.py)
# reshapes into benchmark.json.
#
# Contract: stdout carries ONLY the final JSON. Everything else (build logs,
# per-track progress) goes to stderr, so the host can parse stdout cleanly.
#
# Inputs (env):
#   CHALLENGE            (set by the image) — challenge name
#   TIG_TRACKS           JSON {track_key: nonce_count}  (no "seed" key)
#   TIG_SEED             rand_hash / base seed string
#   TIG_FUEL             fuel cap (= max_fuel_budget)
#   TIG_HYPERPARAMETERS  "null" | flat JSON | per-track {track: {...}} JSON
#   TIG_WORKERS          worker threads for modified_test_algorithm (default 4)
import json
import os
import subprocess
import sys
import tempfile


def hp_for(hp: str, track_key: str) -> str:
    """Resolve the hyperparameters arg for one track (flat or per-track map)."""
    if hp in (None, "", "null"):
        return "null"
    try:
        obj = json.loads(hp)
    except Exception:
        return "null"
    if isinstance(obj, dict) and isinstance(obj.get(track_key), dict):
        return json.dumps(obj[track_key])
    return hp


def main() -> int:
    challenge = os.environ["CHALLENGE"]
    tracks = json.loads(os.environ.get("TIG_TRACKS", "{}"))
    seed = os.environ.get("TIG_SEED", "test")
    fuel = int(os.environ.get("TIG_FUEL", str(5_000_000_000_000)))
    hp = os.environ.get("TIG_HYPERPARAMETERS") or "null"
    workers = os.environ.get("TIG_WORKERS", "4")
    # Repo root: /app in the custom image (baked source); on C3 the uploaded
    # source dir is passed via TIG_WORKDIR.
    workdir = os.environ.get("TIG_WORKDIR", "/app")

    # Compile the injected algorithm once (slot already populated).
    print("[driver] building swarm_algo…", file=sys.stderr)
    b = subprocess.run(
        ["build_algorithm", "swarm_algo"],
        cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if b.returncode != 0:
        sys.stderr.write(b.stdout[-4000:] + "\n")
        print(json.dumps({"challenge": challenge, "fuel_limit": fuel,
                          "tracks": {}, "error": "build_failed"}))
        return b.returncode
    sys.stderr.write(b.stdout[-1000:] + "\n")

    out_tracks: dict[str, dict] = {}
    for track_key, count in tracks.items():
        if track_key == "seed" or not isinstance(count, int) or count <= 0:
            continue
        path = tempfile.mktemp(suffix=".json")
        cmd = [
            "modified_test_algorithm", "swarm_algo", track_key, hp_for(hp, track_key),
            "--seed", seed, "--nonces", str(count), "--fuel", str(fuel),
            "--workers", workers, "--output-json", path,
        ]
        print(f"[driver] track {track_key} ({count} nonces)…", file=sys.stderr)
        r = subprocess.run(cmd, cwd=workdir, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0 or not os.path.exists(path):
            print(f"[driver] track {track_key} failed: {(r.stderr or '')[-500:]}",
                  file=sys.stderr)
            out_tracks[track_key] = {"error": (r.stderr or "")[-500:]}
            continue
        with open(path) as fp:
            out_tracks[track_key] = json.load(fp)
        os.unlink(path)

    print(json.dumps({"challenge": challenge, "fuel_limit": fuel, "tracks": out_tracks}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
