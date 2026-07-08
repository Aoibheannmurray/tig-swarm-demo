"""Authentication for the hosted runner (P3).

The runner is a separate service and deliberately does NOT hold the swarm's
base password. It authenticates a contributor by forwarding their credentials
to the coordination server's `GET /api/contributor/me` (added in P0) — the same
check that gates agent registration. So revocation, rate limiting, and the
derived-password scheme are all inherited, with no secret duplicated here.

The revoke webhook is gated by a shared `RUNNER_ADMIN_KEY` (set to the swarm's
admin_key), so only the host can tear down a contributor's hosted fleet.
"""

from __future__ import annotations

import os
import secrets as _secrets
import urllib.error
import urllib.request

COORDINATION_SERVER_URL = os.environ.get("COORDINATION_SERVER_URL", "").rstrip("/")


class AuthError(RuntimeError):
    """Credentials rejected or the coordination server was unreachable. The
    service maps this to 401/502 as appropriate via `status`."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def verify_contributor(username: str, password: str, *, timeout: int = 15) -> dict:
    """Return the coordination server's contributor summary, or raise
    AuthError. `username`/`password` are the X-Username / X-Swarm-Password
    values from the enrolling request."""
    if not COORDINATION_SERVER_URL:
        raise AuthError(
            "Runner misconfigured: COORDINATION_SERVER_URL is unset.", status=503,
        )
    if not username or not password:
        raise AuthError("Missing credentials.")
    req = urllib.request.Request(
        f"{COORDINATION_SERVER_URL}/api/contributor/me",
        headers={"X-Username": username, "X-Swarm-Password": password},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError("Invalid or revoked credentials.", status=401)
        raise AuthError(f"Coordination server error (HTTP {e.code}).", status=502)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise AuthError("Could not reach the coordination server.", status=502)


def fetch_contributor_config(username: str, password: str, *, timeout: int = 15) -> dict:
    """Fetch the contributor's stored fleet plan from the coordination server
    (GET /api/contributor/config). Returns {} when they've saved nothing yet
    (404). Raises AuthError on transport failure so enrollment fails cleanly."""
    if not COORDINATION_SERVER_URL:
        raise AuthError("Runner misconfigured: COORDINATION_SERVER_URL is unset.", status=503)
    req = urllib.request.Request(
        f"{COORDINATION_SERVER_URL}/api/contributor/config",
        headers={"X-Username": username, "X-Swarm-Password": password},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        if e.code in (401, 403):
            raise AuthError("Invalid or revoked credentials.", status=401)
        raise AuthError(f"Coordination server error (HTTP {e.code}).", status=502)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise AuthError("Could not reach the coordination server.", status=502)
    cfg = body.get("config") or {}
    if body.get("tacit"):
        cfg["tacit"] = body["tacit"]
    return cfg


def verify_admin(admin_key: str | None) -> None:
    """Constant-time check of the revoke webhook's shared secret."""
    expected = os.environ.get("RUNNER_ADMIN_KEY", "")
    if not expected:
        raise AuthError("Runner misconfigured: RUNNER_ADMIN_KEY is unset.", status=503)
    if not admin_key or not _secrets.compare_digest(admin_key, expected):
        raise AuthError("Invalid admin key.", status=403)
