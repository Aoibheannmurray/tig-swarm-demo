#!/usr/bin/env python3
"""Clear the leaderboard for a single challenge.

Drops `trajectory_bests` and `best_history` rows for the named challenge so the
next feasible publish becomes the new global best. Preserves `experiments`,
`hypotheses`, and `trajectories` — research history is intact.

Usage:
    python3 scripts/admin_reset_challenge.py <challenge> --admin-key <KEY>

The admin key was printed on first server boot and persisted in the
server's `config` table; if you launched the swarm via setup.py it's also
in `swarm.admin.json` (key: `admin_key`). On Railway, it's the value of the
ADMIN_KEY env var.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _resolve_server_url() -> str:
    env = os.environ.get("TIG_SWARM_SERVER")
    if env:
        return env.rstrip("/")
    cfg = ROOT / ".swarm-cache.json"
    if cfg.exists():
        try:
            url = json.loads(cfg.read_text()).get("server_url", "")
            if url:
                return url.rstrip("/")
        except Exception:
            pass
    sys.exit(
        "admin_reset_challenge.py: server URL not configured. "
        "Set TIG_SWARM_SERVER or run `python setup.py sync`."
    )


def _resolve_admin_key(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("ADMIN_KEY")
    if env:
        return env
    cfg = ROOT / "swarm.admin.json"
    if cfg.exists():
        try:
            key = json.loads(cfg.read_text()).get("admin_key", "")
            if key:
                return key
        except Exception:
            pass
    sys.exit(
        "admin_reset_challenge.py: admin key not provided. Pass --admin-key, "
        "set ADMIN_KEY, or run `python setup.py create` to write swarm.admin.json."
    )


def _known_challenges() -> list[str]:
    # Single source of truth for challenge names: import the server's registry
    # so this script never drifts when a new challenge is added.
    sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
    try:
        from challenges import CHALLENGE_NAMES
    finally:
        sys.path.pop(0)
    return list(CHALLENGE_NAMES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset a single challenge's leaderboard.")
    parser.add_argument("challenge", choices=_known_challenges())
    parser.add_argument("--admin-key", default=None,
                        help="Admin key (defaults to $ADMIN_KEY or swarm.admin.json).")
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
