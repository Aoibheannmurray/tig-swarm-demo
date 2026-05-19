#!/usr/bin/env python3
"""Keep the server's `agents.name` in sync with this clone's local config.

Reads the agent display name from `agent.config.json` (`name`, materialized
from fleet.config.json by `scripts/run_fleet.py`). If the local name
diverges from what the server has on file (e.g. the user renamed in their
fleet config), POSTs a rename so the dashboard label catches up. Without
this, the same agent_id could appear under two names in chat messages vs.
experiment events.

Usage:
    python3 scripts/sync_identity.py <agent_id>

Library:
    from sync_identity import sync_identity, sync_identity_with_state

`sync_identity_with_state` is the preferred entry point for callers that
already have an `/api/state` response in hand — it avoids the extra GET.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent


def _resolve_server_url() -> str:
    if os.environ.get("TIG_SWARM_SERVER"):
        return os.environ["TIG_SWARM_SERVER"].rstrip("/")
    cfg_path = ROOT / ".swarm-cache.json"
    if cfg_path.exists():
        try:
            url = json.loads(cfg_path.read_text()).get("server_url", "")
            if url and not url.startswith("$"):
                return url.rstrip("/")
        except Exception:
            pass
    sys.exit(
        "sync_identity.py: server URL not configured. "
        "Run `python setup.py sync` (or set TIG_SWARM_SERVER)."
    )


def _read_contributor_name() -> Optional[str]:
    """Read this agent's display name from the worktree's agent.config.json
    (materialized from fleet.config.json's `name` field)."""
    cfg_path = ROOT / "agent.config.json"
    if not cfg_path.exists():
        return None
    try:
        name = (json.loads(cfg_path.read_text()).get("name") or "").strip()
    except Exception:
        return None
    return name or None


def _read_agent_token() -> Optional[str]:
    """Read the agent token from agent.config.json. The token is persisted
    by run_loop.py after the first successful /api/agents/register call;
    it's the per-agent secret that gates every non-register write."""
    cfg_path = ROOT / "agent.config.json"
    if not cfg_path.exists():
        return None
    try:
        tok = (json.loads(cfg_path.read_text()).get("agent_token") or "").strip()
    except Exception:
        return None
    return tok or None


def _post_rename(
    server: str, agent_id: str, new_name: str,
    *, agent_token: Optional[str] = None,
) -> bool:
    """POST /api/agents/{id}/rename. Returns True on success, False on
    non-fatal failure (e.g. 409 collision). Raises on transport errors."""
    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    req = urllib.request.Request(
        f"{server}/api/agents/{agent_id}/rename",
        data=json.dumps({"agent_name": new_name}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.load(resp)
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(
            f"[sync_identity] rename failed ({e.code}): {body}",
            file=sys.stderr,
        )
        return False


def sync_identity_with_state(
    server: str,
    agent_id: str,
    state: dict,
    *,
    desired_name: Optional[str] = None,
    agent_token: Optional[str] = None,
) -> Optional[str]:
    """Reconcile server's agents.name (from a /api/state response) with the
    local contributor_name. Returns the new name if a rename happened,
    None if nothing to do."""
    if desired_name is None:
        desired_name = _read_contributor_name()
    if not desired_name:
        return None
    server_name = (state.get("agent_name") or "").strip()
    if not server_name or server_name == desired_name:
        return None
    print(
        f"[sync_identity] server says {server_name!r}, local config says "
        f"{desired_name!r} — POSTing rename.",
        file=sys.stderr,
    )
    if _post_rename(server, agent_id, desired_name, agent_token=agent_token):
        return desired_name
    return None


def sync_identity(
    server: str, agent_id: str,
    *, agent_token: Optional[str] = None,
) -> Optional[str]:
    """Standalone entry point: GET /api/state, compare, POST rename if
    needed. Returns the new name if a rename happened, None otherwise."""
    desired = _read_contributor_name()
    if not desired:
        return None
    try:
        url = f"{server}/api/state?agent_id={agent_id}"
        with urllib.request.urlopen(url, timeout=10) as r:
            state = json.load(r)
    except Exception as e:
        print(f"[sync_identity] state fetch failed: {e}", file=sys.stderr)
        return None
    return sync_identity_with_state(
        server, agent_id, state,
        desired_name=desired, agent_token=agent_token,
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/sync_identity.py <agent_id>", file=sys.stderr)
        return 1
    agent_id = sys.argv[1]
    server = _resolve_server_url()
    renamed = sync_identity(server, agent_id, agent_token=_read_agent_token())
    if renamed:
        print(f"renamed to {renamed!r}")
    else:
        print("identity in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
