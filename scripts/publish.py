#!/usr/bin/env python3
"""Publish benchmark results to the swarm coordination server.

Usage:
    python3 scripts/benchmark.py 2>/dev/null \
      | python3 scripts/publish.py AGENT_ID "title" "description" strategy_tag "notes"

Thin CLI wrapper around `swarm_client.publish_results` — the request payload
(algorithm code, kernel, multi-file map, track scores, token counters, …) is
built there, so this stays in lockstep with what run_loop.py publishes. This
module adds only the stdin/argv plumbing plus the pre/post-POST diagnostics
that make silently-dropped solution_data visible.
"""

import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).parent.parent

from swarm_client import publish_results, resolve_server_url, server_get  # noqa: E402


def _resolve_agent_token() -> str:
    """Read the agent token from agent.config.json. The token is persisted
    by run_loop.py after the first successful /api/agents/register; gates
    every non-register write. publish.py refuses to run without it because
    /api/iterations requires X-Agent-Token."""
    cfg_path = ROOT / "agent.config.json"
    if cfg_path.exists():
        try:
            tok = (json.loads(cfg_path.read_text()).get("agent_token") or "").strip()
            if tok:
                return tok
        except Exception:
            pass
    sys.exit(
        "publish.py: agent_token missing from agent.config.json. "
        "Run `python scripts/run_loop.py` once first so the agent registers "
        "with the swarm and the token gets persisted."
    )


def _load_swarm_config() -> dict:
    """The active challenge's config from .swarm-cache.json. Its
    algorithm_path / kernel_path tell publish_results which files to read."""
    cfg_path = ROOT / ".swarm-cache.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            if isinstance(cfg, dict) and cfg.get("algorithm_path"):
                return cfg
        except Exception:
            pass
    print(
        "error: .swarm-cache.json missing or has no algorithm_path — run setup.py sync first",
        file=sys.stderr,
    )
    sys.exit(1)


def main():
    if len(sys.argv) < 5:
        print(
            "Usage: python3 scripts/publish.py <agent_id> <title> <description> <strategy_tag> [notes]",
            file=sys.stderr,
        )
        sys.exit(1)

    agent_id = sys.argv[1]
    mutation = {
        "title": sys.argv[2],
        "description": sys.argv[3],
        "strategy_tag": sys.argv[4],
        "notes": sys.argv[5] if len(sys.argv) > 5 else "",
    }

    server = resolve_server_url("publish.py")
    agent_token = _resolve_agent_token()

    # Keep server's agents.name aligned with the agent.config.json `name`
    # before publishing. Best-effort: if the sync fails (server down, name
    # collision), publish continues — the user can fix the name later.
    try:
        from sync_identity import sync_identity
        sync_identity(server, agent_id, agent_token=agent_token)
    except Exception as e:
        print(f"[publish] identity sync skipped: {e}", file=sys.stderr)

    bench = json.load(sys.stdin)
    config = _load_swarm_config()

    algo_path = ROOT / config["algorithm_path"]
    if not algo_path.exists():
        sys.exit(f"publish.py: algorithm file not found: {algo_path}")

    # Pre-POST: surface what we're sending so a silent drop is visible at
    # publish time (the proxy / size class of bug we hit earlier was
    # invisible until the dashboard didn't render).
    sd = bench.get("viz_data")
    if sd is None:
        print("[publish] solution_data: none", file=sys.stderr)
    else:
        n_inst = len(sd) if isinstance(sd, dict) else 0
        print(
            f"[publish] solution_data: {n_inst} instance(s), "
            f"{len(json.dumps(sd)) / 1024:.1f} KB",
            file=sys.stderr,
        )

    try:
        result = publish_results(
            server, agent_id, bench, mutation, config,
            agent_token=agent_token,
        )
    # HTTPError first — it subclasses URLError, so the order matters.
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        sys.exit(f"publish.py: server returned {e.code}: {body_text}")
    except urllib.error.URLError as e:
        sys.exit(f"publish.py: failed to reach server at {server}: {e}")

    print(json.dumps(result, indent=2))

    # Post-POST: when this iteration is the new global best AND we sent
    # solution_data, verify the server actually persisted it. A NULL
    # `best_solution_data` here means the body was dropped somewhere
    # between us and the DB (Railway proxy, body limit, schema
    # mismatch) — exactly the failure mode that previously stayed
    # invisible until the dashboard came up empty.
    if sd is not None and result.get("is_new_best"):
        try:
            ch = bench.get("challenge") or ""
            url = f"{server}/api/state?challenge={ch}" if ch else f"{server}/api/state"
            state = server_get(url, timeout=10)
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
