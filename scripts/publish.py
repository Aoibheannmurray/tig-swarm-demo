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

ROOT = Path(__file__).parent.parent

def _resolve_server_url() -> str:
    if os.environ.get("TIG_SWARM_SERVER"):
        return os.environ["TIG_SWARM_SERVER"].rstrip("/")
    cfg_path = ROOT / "swarm.config.json"
    if cfg_path.exists():
        try:
            url = json.loads(cfg_path.read_text()).get("server_url", "")
            if url and not url.startswith("$"):
                return url.rstrip("/")
        except Exception:
            pass
    sys.exit(
        "publish.py: server URL not configured. Run "
        "`python setup.py join <swarm-url>` (or set TIG_SWARM_SERVER)."
    )

SERVER = _resolve_server_url()


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
    print("error: swarm.config.json missing or has no algorithm_path — run setup.py first", file=sys.stderr)
    sys.exit(1)


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
        "notes": notes,
        "solution_data": bench.get("viz_data"),
        "track_scores": bench.get("track_scores"),
        "challenge": bench.get("challenge"),
    }
    # VRP-only fields. benchmark.py omits these for non-VRP challenges; we
    # forward them only when present so SAT / knapsack / etc. payloads
    # don't carry meaningless num_vehicles=0 / total_distance=score.
    if bench.get("num_vehicles") is not None:
        payload["num_vehicles"] = bench["num_vehicles"]
    if bench.get("total_distance") is not None:
        payload["total_distance"] = bench["total_distance"]

    # Pre-POST: surface what we're sending so a silent drop is visible at
    # publish time (the proxy / size class of bug we hit earlier was
    # invisible until the dashboard didn't render).
    sd = payload["solution_data"]
    body = json.dumps(payload).encode()
    if sd is None:
        print("[publish] solution_data: none", file=sys.stderr)
    else:
        n_inst = len(sd) if isinstance(sd, dict) else 0
        print(
            f"[publish] solution_data: {n_inst} instance(s), "
            f"payload {len(body) / 1024:.1f} KB",
            file=sys.stderr,
        )

    req = urllib.request.Request(
        f"{SERVER}/api/iterations",
        data=body,
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
        body_text = e.read().decode(errors="replace")
        sys.exit(f"publish.py: server returned {e.code}: {body_text}")

    # Post-POST: when this iteration is the new global best AND we sent
    # solution_data, verify the server actually persisted it. A NULL
    # `best_solution_data` here means the body was dropped somewhere
    # between us and the DB (Railway proxy, body limit, schema
    # mismatch) — exactly the failure mode that previously stayed
    # invisible until the dashboard came up empty.
    if sd is not None and result.get("is_new_best"):
        try:
            ch = bench.get("challenge") or ""
            url = f"{SERVER}/api/state?challenge={ch}" if ch else f"{SERVER}/api/state"
            with urllib.request.urlopen(url, timeout=10) as r:
                state = json.load(r)
            if state.get("best_solution_data") is None:
                print(
                    "[publish] WARNING: solution_data sent and this is a new "
                    "global best, but server's best_solution_data is NULL — "
                    "likely proxy/body-size dropped the field.",
                    file=sys.stderr,
                )
            else:
                print("[publish] verified: solution_data persisted server-side.", file=sys.stderr)
        except Exception as e:
            print(f"[publish] verification GET failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
