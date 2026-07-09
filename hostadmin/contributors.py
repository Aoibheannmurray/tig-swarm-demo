"""Contributor management host-admin commands: invite / revoke / list.

Moved from the root setup.py; the verbatim admin-creds preamble that
run_revoke / run_list both carried is deduped into `_admin_creds`, and their
identical POST error handling into `_admin_post`."""

from __future__ import annotations

import json
import sys
import urllib.error

from .config_io import read_swarm_admin, read_swarm_cache, write_swarm_admin
from .http import post_json

_INVITE_ADJECTIVES = [
    "swift", "bold", "keen", "calm", "bright", "sharp", "vivid", "steady",
    "fierce", "noble", "agile", "lucid", "rapid", "silent", "cosmic",
    "astral", "polar", "solar", "lunar", "crystal", "quantum", "neural",
    "primal", "sonic", "radiant", "golden", "silver", "iron", "amber",
    "crimson", "azure", "obsidian", "phantom", "blazing", "frozen",
]
_INVITE_NOUNS = [
    "falcon", "wolf", "hawk", "lynx", "otter", "raven", "viper", "fox",
    "crane", "tiger", "cobra", "eagle", "shark", "puma", "elk", "owl",
    "mantis", "phoenix", "hydra", "sphinx", "atlas", "nova", "pulse",
    "spark", "orbit", "flux", "prism", "forge", "nexus", "cipher",
    "vector", "vertex", "helix", "quasar", "photon", "beacon",
]


def _generate_invite_slug(taken: set[str]) -> str:
    import random
    for _ in range(200):
        name = f"{random.choice(_INVITE_ADJECTIVES)}-{random.choice(_INVITE_NOUNS)}"
        if name not in taken:
            return name
    # Fallback with a numeric suffix once the namespace is saturated.
    return f"contrib-{random.randint(10000, 99999)}"


def build_join_link(server_url: str | None, username: str, derived: str) -> str | None:
    """One-link invite: `<server>/join#u=<username>&p=<derived>`.

    The credentials ride in the URL *fragment*, which browsers never send to
    the server — they stay out of Railway/proxy logs. The hosted /join page
    reads the fragment client-side (see docs/server-first-onboarding-plan.md
    §5). Returns None when no usable server URL is known (fresh host machine
    before `setup.py create`), so callers can skip the link line rather than
    print a broken one.
    """
    import urllib.parse
    url = (server_url or "").strip().rstrip("/")
    if not url or url.startswith("<") or url.startswith("$"):
        return None
    return (
        f"{url}/join"
        f"#u={urllib.parse.quote(username, safe='')}"
        f"&p={urllib.parse.quote(derived, safe='')}"
    )


def run_invite(username: str | None) -> int:
    """Issue a per-contributor swarm password by computing
    sha256(username + ':' + base_password). Prints the username + derived
    hash for the host to share out-of-band with the contributor.

    If `username` is None, an adjective-noun slug is generated for the
    host; previously-issued names are tracked in swarm.admin.json
    (`issued_contributors`) to avoid collisions across invite calls."""
    import hashlib
    admin = read_swarm_admin()
    base = (admin.get("swarm_password") or "").strip()
    if not base:
        print(
            "invite: no swarm_password in swarm.admin.json — "
            "run `setup.py create` first (host machine only).",
            file=sys.stderr,
        )
        return 1
    issued: list[str] = list(admin.get("issued_contributors") or [])
    revoked: list[str] = list(admin.get("revoked_contributors") or [])
    if username:
        username = username.strip()
        if not username:
            print("invite: username must be non-empty", file=sys.stderr)
            return 1
        if username in revoked:
            # Re-issuing the same hash for a revoked name won't help — the
            # server's revoked list rejects by username, not by hash.
            print(
                f"invite: {username!r} is on the revoked list; "
                f"pick a different name or rotate the base password.",
                file=sys.stderr,
            )
            return 1
    else:
        username = _generate_invite_slug(set(issued) | set(revoked))
    derived = hashlib.sha256(f"{username}:{base}".encode()).hexdigest()
    server_url = admin.get("server_url") or read_swarm_cache().get("server_url") or "<paste server URL>"
    if username not in issued:
        issued.append(username)
        admin["issued_contributors"] = issued
        write_swarm_admin(admin)
    join_link = build_join_link(server_url, username, derived)
    if join_link:
        print()
        print(f"  Join link (share this one line):")
        print(f"    {join_link}")
        print()
        print("  It opens the swarm's join page with these credentials —")
        print("  treat it like the password it contains.")
    print()
    print(f'  "server_url": {json.dumps(server_url)},')
    print(f'  "username": {json.dumps(username)},')
    print(f'  "swarm_password": {json.dumps(derived)},')
    print()
    print("  Or share the three lines above for the manual flow: the")
    print("  contributor pastes them into their fleet.config.json (replacing")
    print("  the matching keys), then runs `python scripts/run_fleet.py`.")
    print()
    return 0


def _resolve_host_server_url(admin: dict) -> str | None:
    """Host-admin URL resolver. Distinct from `resolve_server_url()`, which
    checks fleet.config.json — that file points at the swarm the *contributor*
    is participating in, which may be a different swarm from the one the
    host owns. For admin commands (invite/revoke/list) we want the URL of
    the swarm whose admin_key we have, not whatever swarm this clone is
    also contributing to. Precedence:
      1. swarm.admin.json `server_url` (written by setup.py create)
      2. .swarm-cache.json `server_url` (refreshed by setup.py sync)
    """
    url = (admin.get("server_url") or "").strip()
    if url:
        return url
    cache_url = (read_swarm_cache().get("server_url") or "").strip()
    return cache_url or None


def _admin_creds(command: str) -> tuple[dict, str, str] | None:
    """Shared preamble for the admin-key-gated commands (revoke / list):
    load swarm.admin.json and require an admin_key + a resolvable
    server_url, printing the same actionable errors (prefixed with the
    command name) both commands used to carry verbatim. Returns
    (admin, admin_key, server_url), or None after printing the error."""
    admin = read_swarm_admin()
    admin_key = (admin.get("admin_key") or "").strip()
    if not admin_key:
        print(
            f"{command}: no admin_key in swarm.admin.json — "
            "run `setup.py create` first (host machine only).",
            file=sys.stderr,
        )
        return None
    server_url = _resolve_host_server_url(admin)
    if not server_url:
        print(
            f"{command}: no server_url found in swarm.admin.json or "
            ".swarm-cache.json — run `python setup.py sync` first.",
            file=sys.stderr,
        )
        return None
    return admin, admin_key, server_url


def _admin_post(command: str, server_url: str, endpoint: str, payload: dict) -> dict | None:
    """POST to an /api/admin/* endpoint with the error handling revoke and
    list previously duplicated. Returns the decoded response body, or None
    after printing the command-prefixed error."""
    try:
        return post_json(
            f"{server_url.rstrip('/')}{endpoint}", payload, timeout=10,
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"{command}: server returned {e.code}: {body}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"{command}: failed to reach {server_url} ({e})", file=sys.stderr)
        return None


def run_revoke(username: str) -> int:
    """Revoke a contributor by username. POSTs to /api/admin/revoke which
    adds the name to the server's revoked list (blocks future registers)
    and clears the per-agent tokens on any of their existing agents so
    in-flight workers stop authenticating immediately."""
    username = (username or "").strip()
    if not username:
        print("revoke: username must be non-empty", file=sys.stderr)
        return 1
    creds = _admin_creds("revoke")
    if creds is None:
        return 1
    admin, admin_key, server_url = creds
    result = _admin_post(
        "revoke", server_url, "/api/admin/revoke",
        {"admin_key": admin_key, "username": username},
    )
    if result is None:
        return 1
    # Mirror the server-side revoke into swarm.admin.json so `setup.py
    # invite` can warn before re-issuing a revoked name.
    revoked = list(admin.get("revoked_contributors") or [])
    if username not in revoked:
        revoked.append(username)
        admin["revoked_contributors"] = revoked
        write_swarm_admin(admin)

    # If a hosted runner is configured, tear down the contributor's cloud fleet
    # and purge their stored keys too (best-effort — a revoke on the
    # coordination server already stops their agents authenticating). The
    # admin_key doubles as the runner's RUNNER_ADMIN_KEY.
    runner_teardown = _revoke_hosted_fleet(admin, admin_key, username)

    print()
    print(f"  Revoked:        {username}")
    print(f"  Agents stopped: {result.get('agents_invalidated', 0)}")
    if runner_teardown is not None:
        print(f"  Hosted fleet:   {runner_teardown}")
    print(f"  Future register attempts under this username will be rejected.")
    print()
    return 0


def run_set_runner(runner_url: str) -> int:
    """Point the swarm at its hosted fleet runner (the zero-install Tier-1
    service). Two effects:
      1. Sets the server's `runner_url` config, which makes the contributor
         join page show the "Run in the cloud" tab.
      2. Mirrors it into swarm.admin.json so `setup.py revoke` also tears
         down a contributor's hosted fleet + purges their stored keys.
    Pass an empty string to unset (hides the tab; stops revoke teardown)."""
    import urllib.parse

    runner_url = (runner_url or "").strip().rstrip("/")
    if runner_url and not runner_url.startswith(("http://", "https://")):
        print("set-runner: runner URL must start with http:// or https://",
              file=sys.stderr)
        return 1
    creds = _admin_creds("set-runner")
    if creds is None:
        return 1
    admin, admin_key, server_url = creds
    endpoint = (
        f"{server_url.rstrip('/')}/api/admin/config"
        f"?key=runner_url&value={urllib.parse.quote(runner_url, safe='')}"
    )
    try:
        post_json(endpoint, {"admin_key": admin_key})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        print(f"set-runner: server returned {e.code}: {body}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"set-runner: failed to reach {server_url} ({e})", file=sys.stderr)
        return 1
    admin["runner_url"] = runner_url
    write_swarm_admin(admin)
    print()
    if runner_url:
        print(f"  Hosted runner set: {runner_url}")
        print("  NOTE: the join page's cloud tab is currently disabled in the UI —")
        print("  the runner is reachable via its API only (see runner/README.md).")
        print("  `setup.py revoke` will also tear down a contributor's hosted fleet.")
    else:
        print("  Hosted runner unset.")
    print()
    return 0


def _revoke_hosted_fleet(admin: dict, admin_key: str, username: str) -> str | None:
    """Best-effort teardown of a contributor's hosted (Tier-1) fleet via the
    runner's admin webhook. Returns a human status string, or None when no
    runner is configured (the common case). Never fails the revoke."""
    runner_url = (admin.get("runner_url") or "").strip().rstrip("/")
    if not runner_url:
        return None
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"{runner_url}/api/runner/admin/revoke",
        data=json.dumps({"username": username}).encode(),
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
        return "stopped + keys purged" if body.get("was_running") else "no active fleet"
    except urllib.error.HTTPError as e:
        return f"runner returned HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return f"runner unreachable ({e})"


def run_list() -> int:
    """List contributors. POSTs to /api/admin/contributors and pretty-prints
    one row per contributor (joined name, agent counts, last heartbeat,
    revoked flag). Also flags any name in swarm.admin.json that the server
    doesn't know about — a quick way to spot invites that haven't been used yet."""
    creds = _admin_creds("list")
    if creds is None:
        return 1
    admin, admin_key, server_url = creds
    data = _admin_post(
        "list", server_url, "/api/admin/contributors",
        {"admin_key": admin_key},
    )
    if data is None:
        return 1

    rows = data.get("contributors") or []
    if not rows:
        print()
        print("  No contributors registered yet.")
        print(f"  Issue an invite with:  python setup.py invite [<username>]")
        print()
        return 0

    header = ("USERNAME", "AGENTS", "ACTIVE", "LAST HEARTBEAT", "STATE")
    table = [header]
    for r in rows:
        if r["revoked"]:
            state = "revoked"
        elif r["agents_invalidated"] and r["agents_invalidated"] == r["agent_count"]:
            # Edge case: not in the revoked set but every agent has had its
            # token cleared. Shouldn't normally happen, but flag it rather
            # than silently labelling them "ok".
            state = "tokens cleared"
        else:
            state = "ok"
        table.append((
            r["username"] or "",
            str(r["agent_count"]),
            str(r["agents_active"]),
            r["last_heartbeat"] or "—",
            state,
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    print()
    for i, row in enumerate(table):
        print("  " + "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print("  " + "  ".join("-" * w for w in widths))

    # Cross-check against the local invite log so the host can spot names
    # they've issued credentials for but who haven't joined yet.
    issued = set(admin.get("issued_contributors") or [])
    joined = {r["username"] for r in rows}
    pending = sorted(issued - joined)
    if pending:
        print()
        print(f"  Issued but not yet joined ({len(pending)}):")
        for u in pending:
            print(f"    - {u}")
    print()
    print(f"  Active window: heartbeats since {data.get('inactive_cutoff', '?')}.")
    print()
    return 0
