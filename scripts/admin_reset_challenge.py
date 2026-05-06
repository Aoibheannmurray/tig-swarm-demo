#!/usr/bin/env python3
"""Clear the leaderboard for a single challenge.

Drops `agent_bests` and `best_history` rows for the named challenge so the
next feasible publish becomes the new global best. Preserves `experiments`,
`hypotheses`, and `trajectories` — research history is intact.

Usage:
    python3 scripts/admin_reset_challenge.py <challenge> --admin-key <KEY>

The admin key was printed on first server boot and persisted in the
server's `config` table; if you launched the swarm via setup.py it's also
in `swarm.config.json` (key: `admin_key`). On Railway, it's the value of
the ADMIN_KEY env var.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _resolve_server_url() -> str:
    env = os.environ.get("TIG_SWARM_SERVER")
    if env:
        return env.rstrip("/")
    cfg = Path(__file__).parent.parent / "swarm.config.json"
    if cfg.exists():
        try:
            url = json.loads(cfg.read_text()).get("server_url", "")
            if url:
                return url.rstrip("/")
        except Exception:
            pass
    sys.exit(
        "admin_reset_challenge.py: server URL not configured. "
        "Set TIG_SWARM_SERVER or run `python setup.py join <url>`."
    )


def _resolve_admin_key(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("ADMIN_KEY")
    if env:
        return env
    cfg = Path(__file__).parent.parent / "swarm.config.json"
    if cfg.exists():
        try:
            key = json.loads(cfg.read_text()).get("admin_key", "")
            if key:
                return key
        except Exception:
            pass
    sys.exit(
        "admin_reset_challenge.py: admin key not provided. Pass --admin-key, "
        "set ADMIN_KEY, or add `admin_key` to swarm.config.json."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset a single challenge's leaderboard.")
    parser.add_argument("challenge", choices=[
        "satisfiability", "vehicle_routing", "knapsack",
        "job_scheduling", "energy_arbitrage",
    ])
    parser.add_argument("--admin-key", default=None,
                        help="Admin key (defaults to $ADMIN_KEY or swarm.config.json).")
    args = parser.parse_args()

    server = _resolve_server_url()
    admin_key = _resolve_admin_key(args.admin_key)

    payload = {"admin_key": admin_key, "challenge": args.challenge}
    req = urllib.request.Request(
        f"{server}/api/admin/reset_challenge",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(json.dumps(json.load(resp), indent=2))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"server returned {e.code}: {body}")
    except urllib.error.URLError as e:
        sys.exit(f"failed to reach {server}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
