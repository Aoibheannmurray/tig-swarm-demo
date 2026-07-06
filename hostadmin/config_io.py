"""Swarm config file I/O: swarm.admin.json / .swarm-cache.json / fleet
config reads, placeholder templating of tracked files, and atomic JSON
writes. Extracted verbatim from the root setup.py."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Repo root — hostadmin/ sits directly under it, so this matches the old
# `Path(__file__).parent` from the root setup.py in every clone and worktree.
ROOT = Path(__file__).resolve().parent.parent

# Files that carry swarm-specific values (URL, active challenge name) and
# get rewritten in-place by `setup.py create` / `setup.py sync`. benchmark.py
# and publish.py are intentionally excluded — they contain challenge-generic
# code (function names, data keys, docstrings for all five challenges) that
# must not be rewritten. They read the active challenge from .swarm-cache.json
# at runtime instead.
TEMPLATED_FILES = [
    ROOT / "README.md",
]

# Heuristic URL patterns treated as "the swarm URL" and rewritten when the
# server URL changes. Catches the canonical Railway domain and raw IP-form
# URLs from older self-host setups. Without this, a clone whose baked URL
# doesn't match the current `.swarm-cache.json` server_url (e.g. someone
# committed their templated state, or migrated between hosting styles) would
# silently fail to re-template.
_RAILWAY_URL_RE = re.compile(r"https?://[a-zA-Z0-9-]+\.up\.railway\.app")
_RAW_IP_URL_RE = re.compile(r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?")

# The literal placeholder strings the tracked files carry. NEVER replace
# arbitrary URLs — too easy to clobber rustup / GitHub / localhost dev URLs
# that happen to live in the same files.
PLACEHOLDER_URL = "${SERVER_URL}"
PLACEHOLDER_CHALLENGE = "${CHALLENGE_NAME}"
PLACEHOLDER_ALGO = "${ALGORITHM_PATH}"

AGENT_CONFIG_PATH = ROOT / "agent.config.json"


def _swap(text: str, placeholder: str, prior: str | None, new: str, is_url: bool = False) -> str:
    """Replace the placeholder and the previously-templated value with `new`.

    When `is_url` is True, also sweep Railway / raw-IP URLs — this catches
    stale baked URLs that don't match `prior`. The regex pass is skipped
    for non-URL substitutions (challenge name, algorithm path) so it can't
    clobber just-substituted server URLs."""
    text = text.replace(placeholder, new)
    if prior and prior != placeholder and prior != new:
        text = text.replace(prior, new)
    if is_url:
        text = _RAILWAY_URL_RE.sub(new, text)
        text = _RAW_IP_URL_RE.sub(new, text)
    return text


def template_files(
    server_url: str,
    challenge: str | None = None,
    algorithm_path: str | None = None,
    prior: dict | None = None,
) -> None:
    """Substitute swarm-specific placeholders into every tracked file that
    contains them, using prior values from .swarm-cache.json to undo
    previously-templated state."""
    prior = prior or {}
    prior_url = prior.get("server_url")
    prior_challenge = prior.get("challenge")
    prior_algo = prior.get("algorithm_path")

    for path in TEMPLATED_FILES:
        if not path.exists():
            print(f"  skipping {path} (missing)")
            continue
        text = path.read_text()
        new = _swap(text, PLACEHOLDER_URL, prior_url, server_url, is_url=True)
        if challenge:
            new = _swap(new, PLACEHOLDER_CHALLENGE, prior_challenge, challenge)
        if algorithm_path:
            new = _swap(new, PLACEHOLDER_ALGO, prior_algo, algorithm_path)
        if new != text:
            path.write_text(new)
            print(f"  templated {path.relative_to(ROOT)}")


# Three role-scoped files replace the legacy swarm.config.json:
#   swarm.admin.json  — host-only secrets and tuning (admin_key, stagnation knobs)
#   .swarm-cache.json — machine-managed mirror of /api/swarm_config
#   fleet.config.json — user-edited list of agents to spawn
_ADMIN_FIELDS = (
    "admin_key", "swarm_password", "owner_name", "swarm_name", "server_url",
    "challenges",
    "stagnation_threshold", "stagnation_limit",
    "hypothesis_recall_threshold",
    # Invite/revoke bookkeeping written back by run_invite / run_revoke.
    "issued_contributors", "revoked_contributors",
)
_CACHE_FIELDS = (
    "server_url", "active_challenge", "challenge", "swarm_type",
    "tracks", "timeout", "scoring_direction",
    "algorithm_path", "kernel_path", "is_gpu",
    # Non-secret tuning knobs the *client* needs. The driver
    # (run_loop.py) times tacit-knowledge distillation off stagnation_limit;
    # without these in the cache, config.get("stagnation_limit") is absent
    # and distillation never fires. They're a public mirror of
    # /api/swarm_config — the secret copies stay in swarm.admin.json.
    "stagnation_threshold", "stagnation_limit",
    # write_swarm_cache stamps synced_at itself, so the field is set even
    # when cfg doesn't carry one in.
    "synced_at",
)


def _read_json_dict(path: Path) -> dict:
    """Tolerant JSON read shared by every config-file reader: a missing
    file, a BOM (files edited on Windows carry utf-8-sig), malformed JSON,
    or a non-dict payload all collapse to `{}` so callers can `.get(...)`
    unconditionally."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON via tmp file + os.replace. These files are re-read (and
    rewritten by `setup.py sync`) every iteration by concurrently running
    agents, so a reader must never observe a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def write_swarm_admin(cfg: dict) -> None:
    """Slice the host-only fields out of `cfg` and write them to
    swarm.admin.json. Skips silently when no admin fields are present."""
    payload = {k: cfg[k] for k in _ADMIN_FIELDS if k in cfg}
    if not payload:
        return
    _write_json_atomic(ROOT / "swarm.admin.json", payload)


def write_swarm_cache(cfg: dict) -> None:
    """Slice the server-derived fields out of `cfg` and write them to
    .swarm-cache.json. Stamps `synced_at` so benchmark.py can show freshness."""
    payload = {k: cfg[k] for k in _CACHE_FIELDS if k in cfg}
    if not payload:
        return
    payload["synced_at"] = datetime.now(timezone.utc).isoformat()
    _write_json_atomic(ROOT / ".swarm-cache.json", payload)


def read_swarm_cache() -> dict:
    return _read_json_dict(ROOT / ".swarm-cache.json")


def read_swarm_admin() -> dict:
    return _read_json_dict(ROOT / "swarm.admin.json")


def resolve_server_url() -> str | None:
    """Find server_url in the new layout. Tries agent.config.json (worktree)
    first, then fleet.config.json (root), then .swarm-cache.json as a
    last-resort fallback. Returns None when nothing is configured yet.

    The user-edited configs win over the cache because the cache is a payload
    mirror of /api/swarm_config from a *specific* server — if the user points
    the swarm at a new URL, a leftover cache from the old swarm must not keep
    redirecting sync back to the dead server.
    """
    agent = _read_json_dict(AGENT_CONFIG_PATH)
    if agent.get("server_url"):
        return agent["server_url"]
    fleet = _read_json_dict(ROOT / "fleet.config.json")
    if fleet.get("server_url"):
        return fleet["server_url"]
    cache = read_swarm_cache()
    if cache.get("server_url"):
        return cache["server_url"]
    return None
