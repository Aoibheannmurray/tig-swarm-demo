#!/usr/bin/env python3
"""Publish benchmark results to the swarm coordination server.

Usage:
    python3 scripts/benchmark.py 2>/dev/null \
      | python3 scripts/publish.py AGENT_ID "title" "description" strategy_tag "notes"
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The wizard rewrites the literal placeholder below to the swarm's URL.
# TIG_SWARM_SERVER env var overrides — useful for ad-hoc testing without
# rerunning setup. The startswith("$") check catches the un-substituted
# placeholder so a contributor who forgot to run setup.py join gets a
# loud failure instead of a silent post to nowhere.
SERVER = os.environ.get("TIG_SWARM_SERVER") or "https://t1-production-0047.up.railway.app//"
if SERVER.startswith("$"):
    sys.exit(
        "publish.py: server URL not configured. Run "
        "`python setup.py join <swarm-url>` (or set TIG_SWARM_SERVER)."
    )
# Strip trailing slashes — Railway's proxy turns POSTs to URLs with stacked
# slashes (e.g. `…railway.app///api/iterations`) into a redirect that drops
# the body / converts to GET, which surfaces as "Connection reset by peer"
# on large solution_data payloads and HTTP 405 on small ones. The trailing
# slash sneaks in through the wizard's URL substitution; normalise here so
# solution_data actually lands on the server.
SERVER = SERVER.rstrip("/")
ROOT = Path(__file__).parent.parent


def _resolve_algo_path() -> Path:
    """Determine the active challenge's algorithm file from swarm.config.json."""
    cfg_path = ROOT / "swarm.config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            algo = cfg.get("algorithm_path")
            if algo:
                return ROOT / algo
        except Exception:
            pass
    return ROOT / "src" / "job_scheduling" / "algorithm" / "mod.rs"


def main():
    if len(sys.argv) < 5:
        print(
            "Usage: python3 scripts/publish.py <agent_id> <title> <description> <strategy_tag> [notes]",
            file=sys.stderr,
        )
        sys.exit(1)

    agent_id = sys.argv[1]
    title = sys.argv[2]
    description = sys.argv[3]
    strategy_tag = sys.argv[4]
    notes = sys.argv[5] if len(sys.argv) > 5 else ""

    bench = json.load(sys.stdin)

    algo_path = _resolve_algo_path()
    if not algo_path.exists():
        sys.exit(f"publish.py: algorithm file not found: {algo_path}")
    code = algo_path.read_text()

    payload = {
        "agent_id": agent_id,
        "title": title,
        "description": description,
        "strategy_tag": strategy_tag,
        "algorithm_code": code,
        "score": bench["score"],
        "feasible": bench["feasible"],
        "num_vehicles": bench.get("num_vehicles", 0),
        "total_distance": bench.get("total_distance", bench["score"]),
        "notes": notes,
        "solution_data": bench.get("viz_data"),
        "track_scores": bench.get("track_scores"),
        "challenge": bench.get("challenge"),
    }

    req = urllib.request.Request(
        f"{SERVER}/api/iterations",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
            print(json.dumps(result, indent=2))
    except urllib.error.URLError as e:
        sys.exit(f"publish.py: failed to reach server at {SERVER}: {e}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"publish.py: server returned {e.code}: {body}")


if __name__ == "__main__":
    main()
