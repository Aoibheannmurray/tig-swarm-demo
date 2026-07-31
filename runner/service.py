"""Hosted fleet runner service (see runner/README.md).

A separate FastAPI service (its own Railway container + volume) that runs
fleets on behalf of contributors who opt into cloud-run — so they contribute
with zero local install. It never holds the swarm's base password: contributor
auth is delegated to the coordination server's /api/contributor/me, and LLM/C3
keys are stored Fernet-encrypted in the runner's own SQLite.

Endpoints (all under /api/runner):
  POST   /enroll        enroll or update: store keys, validate plan, launch
  GET    /status        this contributor's enrollment + fleet state (masked)
  GET    /logs          recent fleet log lines (console stream)
  DELETE /enroll        unenroll: stop fleet, purge keys
  POST   /admin/revoke  host teardown (RUNNER_ADMIN_KEY), used by setup.py revoke
  GET    /health        readiness (vault configured, capacity)

Run: `uvicorn runner.service:app` (see runner/README.md).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import auth, store, validation, vault
from .supervisor import Supervisor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 4 else "set"


supervisor = Supervisor()


def _resume_running_fleets() -> None:
    """Durability across runner restarts: relaunch fleets that were running
    from their stored plan + decrypted keys. A bad row is marked errored, not
    fatal to startup."""
    if not vault.available():
        return
    for row in store.list_enrollments():
        if row.get("status") != "running":
            continue
        config = store.config_of(row)
        keys = vault.decrypt_map(row["encrypted_keys"])
        if config and keys:
            try:
                supervisor.start(row["username"], config, keys)
            except Exception as e:
                store.set_status(row["username"], "error", _now(), str(e)[:300])


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    _resume_running_fleets()
    yield
    supervisor.stop_all()


app = FastAPI(title="TIG Swarm Runner", docs_url=None, redoc_url=None, lifespan=lifespan)

# The contributor's browser is served the join page BY the coordination server,
# then calls this runner cross-origin (with X-Username / X-Swarm-Password
# headers, not cookies) to enroll. Allow that origin — plus any explicitly
# configured extras. No wildcard: only the swarm's own front-ends may call in.
import os as _os  # noqa: E402  (local alias; keeps the env read next to its use)

_allowed = {o for o in (
    auth.COORDINATION_SERVER_URL,
    *_os.environ.get("RUNNER_ALLOWED_ORIGINS", "").split(","),
) if o}
if _allowed:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(_allowed),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["X-Username", "X-Swarm-Password", "X-Admin-Key", "Content-Type"],
    )


# ── Auth dependency ──


async def _contributor(
    x_username: str | None = Header(default=None, alias="X-Username"),
    x_swarm_password: str | None = Header(default=None, alias="X-Swarm-Password"),
) -> tuple[str, str]:
    """Resolve + validate the caller against the coordination server. Returns
    (username, password) so handlers that then fetch the plan can reuse the
    password."""
    try:
        me = auth.verify_contributor(x_username or "", x_swarm_password or "")
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return me["username"], x_swarm_password or ""


# ── Endpoints ──


@app.post("/api/runner/enroll")
async def enroll(payload: dict, creds: tuple[str, str] = Depends(_contributor)):
    """Opt into cloud-run. Body: {"keys": {"ANTHROPIC_API_KEY": "…", "C3_API_KEY": "…"}}.
    The fleet plan is read from the contributor's console config on the
    coordination server; keys are validated to cover it, encrypted, stored,
    and the fleet is launched."""
    if not vault.available():
        raise HTTPException(
            status_code=503,
            detail="Runner not configured for secrets (RUNNER_SECRET_KEY unset).",
        )
    username, password = creds
    keys = payload.get("keys") or {}
    if not isinstance(keys, dict) or not all(isinstance(v, str) for v in keys.values()):
        raise HTTPException(status_code=422, detail="keys must be a name→value object")

    try:
        config = auth.fetch_contributor_config(username, password)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

    # Validate the plan against hosted-run rules + capacity (excluding this
    # contributor's own current enrollment from the global tally).
    others = sum(
        len((store.config_of(r) or {}).get("agents") or [])
        for r in store.list_enrollments() if r["username"] != username
    )
    try:
        agents = validation.validate_plan(config, existing_total_agents=others)
    except validation.EnrollmentError as e:
        raise HTTPException(status_code=422, detail=str(e))

    missing = validation.required_env_vars(agents) - set(keys)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing keys for: {', '.join(sorted(missing))}. Paste them "
                   "so your hosted agents can call their provider and C3.",
        )

    timestamp = _now()
    store.upsert_enrollment(username, vault.encrypt_map(keys), config, timestamp)
    try:
        supervisor.start(username, config, keys)
        store.set_status(username, "running", timestamp)
    except Exception as e:
        store.set_status(username, "error", timestamp, str(e)[:300])
        raise HTTPException(status_code=500, detail=f"Failed to start fleet: {e}")
    return {"enrolled": True, "status": "running", "agents": len(agents)}


@app.get("/api/runner/status")
async def status(creds: tuple[str, str] = Depends(_contributor)):
    username, _ = creds
    row = store.get_enrollment(username)
    if not row:
        return {"enrolled": False}
    config = store.config_of(row) or {}
    keys = vault.decrypt_map(row["encrypted_keys"]) if vault.available() else {}
    return {
        "enrolled": True,
        "status": "running" if supervisor.is_running(username) else row["status"],
        "agents": len((config.get("agents") or [])),
        "keys": {name: _mask(val) for name, val in keys.items()},
        "last_error": row.get("last_error"),
        "updated_at": row.get("updated_at"),
    }


@app.get("/api/runner/logs")
async def logs(creds: tuple[str, str] = Depends(_contributor)):
    username, _ = creds
    return {"lines": supervisor.logs(username)}


@app.delete("/api/runner/enroll")
async def unenroll(creds: tuple[str, str] = Depends(_contributor)):
    """Stop the fleet and purge stored keys — full opt-out."""
    username, _ = creds
    supervisor.stop(username)
    store.delete_enrollment(username)
    return {"enrolled": False}


@app.post("/api/runner/admin/revoke")
async def admin_revoke(payload: dict, x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    """Host-triggered teardown (wired from `setup.py revoke`). Stops the named
    contributor's fleet and purges their keys, regardless of enrollment state."""
    try:
        auth.verify_admin(x_admin_key)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="username required")
    stopped = supervisor.stop(username)
    store.delete_enrollment(username)
    return {"revoked": True, "was_running": stopped}


@app.get("/api/runner/health")
async def health():
    running = supervisor.running_usernames()
    return {
        "status": "ok",
        "vault_configured": vault.available(),
        "coordination_server": bool(auth.COORDINATION_SERVER_URL),
        "running_fleets": len(running),
        "max_agents_per_contributor": validation.max_agents_per_contributor(),
        "max_total_agents": validation.max_total_agents(),
    }
