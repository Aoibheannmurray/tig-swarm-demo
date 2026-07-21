"""Tests for gating code-bearing fields behind the swarm password.

Contract under test:
  1. optional_swarm_password returns True/False instead of raising, for
     valid / missing / wrong / revoked credentials respectively.
  2. _dashboard_state (the public /api/state view) only populates
     best_algorithm_code / best_algorithm_files / best_kernel_code when
     credentialed=True; every other field is unaffected.
  3. GET /api/agent_experiments and /api/trajectory_experiments 403 when
     include_code=true is requested without valid credentials, and still
     work (code included) with them.

Runs standalone (`python test_code_endpoint_gating.py` from the server dir).
"""

import asyncio
import hashlib
import os
import sys
import tempfile

from fastapi import HTTPException

BASE = "base-secret"
USERNAME = "alice"
CHALLENGE = "knapsack"
TS = "2026-07-20T00:00:00Z"


def _derived(username: str) -> str:
    return hashlib.sha256(f"{username}:{BASE}".encode()).hexdigest()


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _setup(revoked=None):
    import json
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        for key, value in (
            ("swarm_password", BASE),
            ("swarm_name", "test-swarm"),
            ("active_challenge", CHALLENGE),
            ("revoked_contributors", json.dumps(revoked or [])),
        ):
            await conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    return db, server


async def _register_agent(db, agent_id="agentA", token="tok-A", name="Agent A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, token) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, name, TS, TS, token),
        )
        await conn.commit()


async def test_optional_swarm_password_outcomes():
    _, server = await _setup(revoked=["bob"])

    assert await server.optional_swarm_password(None, None) is False
    assert await server.optional_swarm_password(USERNAME, None) is False
    assert await server.optional_swarm_password(
        USERNAME, "wrong-hash",
    ) is False
    assert await server.optional_swarm_password(
        "bob", _derived("bob"),
    ) is False, "revoked contributor must not be credentialed"
    assert await server.optional_swarm_password(
        USERNAME, _derived(USERNAME),
    ) is True
    print("PASS test_optional_swarm_password_outcomes")


async def test_dashboard_state_hides_code_unless_credentialed():
    db, server = await _setup()

    anon = await server._dashboard_state(CHALLENGE, credentialed=False)
    assert anon["best_algorithm_code"] is None
    assert anon["best_algorithm_files"] is None
    assert anon["best_kernel_code"] is None
    # Non-code fields must still be present/public.
    assert "best_score" in anon and "leaderboard" in anon

    creds = await server._dashboard_state(CHALLENGE, credentialed=True)
    # No experiments recorded yet in this fresh DB, so the served code falls
    # back to the initial/seed algorithm — but it must not be None, proving
    # credentialed callers get *something* where anon callers get None.
    assert creds["best_algorithm_code"] is not None
    print("PASS test_dashboard_state_hides_code_unless_credentialed")


async def test_include_code_endpoints_require_credentials():
    db, server = await _setup()
    await _register_agent(db)

    async def _get_agent_experiments(include_code, credentialed):
        return await server.get_agent_experiments(
            agent_id="agentA", challenge=CHALLENGE,
            include_code=include_code, credentialed=credentialed,
        )

    # include_code=False never requires credentials.
    resp = await _get_agent_experiments(False, False)
    assert resp["agent_id"] == "agentA"

    # include_code=True without credentials -> 403.
    try:
        await _get_agent_experiments(True, False)
        raise AssertionError("expected 403 for include_code without credentials")
    except HTTPException as e:
        assert e.status_code == 403

    # include_code=True with credentials -> succeeds.
    resp = await _get_agent_experiments(True, True)
    assert resp["agent_id"] == "agentA"

    async def _get_trajectory_experiments(include_code, credentialed):
        return await server.get_trajectory_experiments(
            challenge=CHALLENGE, trajectory_id=None,
            include_code=include_code, credentialed=credentialed,
        )

    resp = await _get_trajectory_experiments(False, False)
    assert "trajectories" in resp

    try:
        await _get_trajectory_experiments(True, False)
        raise AssertionError("expected 403 for include_code without credentials")
    except HTTPException as e:
        assert e.status_code == 403

    resp = await _get_trajectory_experiments(True, True)
    assert "trajectories" in resp
    print("PASS test_include_code_endpoints_require_credentials")


async def _main():
    await test_optional_swarm_password_outcomes()
    await test_dashboard_state_hides_code_unless_credentialed()
    await test_include_code_endpoints_require_credentials()
    print("\nAll code-gating tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
