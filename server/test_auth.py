"""Tests for the per-agent token auth contract and related hardening.

Runs standalone (`python test_auth.py` from the server dir). Points DATA_DIR
at a fresh temp dir before importing server modules.

Contract under test:
  1. GET /api/state?agent_id=X requires a token resolving to agent X
     (403 on missing/mismatched); the dashboard view (no agent_id) stays
     public.
  2. heartbeat / rename: token must resolve to the path agent_id.
  3. create_iteration: token's agent must equal req.agent_id.
  4. create_message: token's agent must equal req.agent_id.
Plus: resolve_challenge rejects unknown challenge names (400), register
degrades a taken custom name inside one transaction, payload size limits,
and ConnectionManager.broadcast not evicting mid-broadcast subscribers.
"""

import asyncio
import os
import sys
import tempfile

from fastapi import HTTPException

CHALLENGE = "knapsack"
TS = "2026-07-06T00:00:00Z"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _register_agent(db, agent_id="agentA", token="tok-A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, token) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, f"Agent {agent_id}", TS, TS, token),
        )
        await conn.commit()


def _expect_status(exc: HTTPException, status: int, what: str):
    assert isinstance(exc, HTTPException) and exc.status_code == status, \
        f"{what}: expected {status}, got {exc!r}"


async def _raises_http(coro, status, what):
    try:
        await coro
    except HTTPException as e:
        _expect_status(e, status, what)
        return
    raise AssertionError(f"{what}: expected HTTPException {status}, none raised")


async def test_token_resolution():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    assert await server.verify_agent_token("tok-A") == "agentA"
    await _raises_http(server.verify_agent_token(None), 403, "missing token")
    await _raises_http(server.verify_agent_token("nope"), 403, "unknown token")
    assert await server.optional_agent_token(None) is None
    assert await server.optional_agent_token("nope") is None
    assert await server.optional_agent_token("tok-A") == "agentA"
    print("PASS test_token_resolution")


async def test_state_agent_view_requires_matching_token():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _register_agent(db, agent_id="agentB", token="tok-B")

    await _raises_http(
        server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id=None),
        403, "state without token")
    await _raises_http(
        server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentB"),
        403, "state with another agent's token")
    state = await server.get_state(
        agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentA")
    assert state["challenge"] == CHALLENGE
    # Dashboard view stays public: no agent_id, no token.
    dash = await server.get_state(challenge=CHALLENGE, token_agent_id=None)
    assert dash["challenge"] == CHALLENGE and "leaderboard" in dash
    print("PASS test_state_agent_view_requires_matching_token")


async def test_heartbeat_and_rename_require_matching_token():
    db, server = _fresh_modules()
    from models import HeartbeatRequest, RenameRequest
    await db.init_db()
    await _register_agent(db)

    await _raises_http(
        server.heartbeat("agentA", HeartbeatRequest(), token_agent_id="agentB"),
        403, "heartbeat with mismatched token")
    ack = await server.heartbeat("agentA", HeartbeatRequest(), token_agent_id="agentA")
    assert ack["ack"] is True

    await _raises_http(
        server.rename_agent("agentA", RenameRequest(agent_name="x"), token_agent_id="agentB"),
        403, "rename with mismatched token")
    out = await server.rename_agent(
        "agentA", RenameRequest(agent_name="renamed-a"), token_agent_id="agentA")
    assert out["agent_name"] == "renamed-a"
    print("PASS test_heartbeat_and_rename_require_matching_token")


async def test_iterations_and_messages_require_matching_token():
    db, server = _fresh_modules()
    from models import IterationCreate, MessageCreate
    await db.init_db()
    await _register_agent(db)

    req = IterationCreate(
        agent_id="agentA", title="t", strategy_tag="greedy",
        algorithm_code="// c\n", score=1.0, feasible=True, challenge=CHALLENGE,
    )
    await _raises_http(
        server.create_iteration(req, token_agent_id="agentB"),
        403, "iteration published with another agent's token")
    resp = await server.create_iteration(req, token_agent_id="agentA")
    assert resp.experiment_id

    msg = MessageCreate(agent_id="agentA", agent_name="ignored", content="hi",
                        challenge=CHALLENGE)
    await _raises_http(
        server.create_message(msg, token_agent_id="agentB"),
        403, "message posted with another agent's token")
    out = await server.create_message(msg, token_agent_id="agentA")
    assert out["message_id"]
    print("PASS test_iterations_and_messages_require_matching_token")


async def test_resolve_challenge_rejects_unknown():
    db, server = _fresh_modules()
    await db.init_db()
    await _raises_http(server.resolve_challenge("no_such_challenge"), 400,
                       "unknown challenge")
    assert await server.resolve_challenge(CHALLENGE) == CHALLENGE
    assert await server.resolve_challenge(None)  # falls back to active
    print("PASS test_resolve_challenge_rejects_unknown")


async def test_register_degrades_taken_name_and_reserves_it():
    db, server = _fresh_modules()
    from models import RegisterRequest
    import names
    await db.init_db()

    first = await server.register_agent(
        RegisterRequest(agent_name="my-custom-name"), contributor_username="alice")
    assert first.agent_name == "my-custom-name"
    # Accepted custom name lands in the generator's used-set (post-boot).
    assert "my-custom-name" in names._used_names
    second = await server.register_agent(
        RegisterRequest(agent_name="my-custom-name"), contributor_username="bob")
    assert second.agent_name != "my-custom-name", \
        "duplicate custom name must degrade to a generated one"
    assert second.agent_token and second.agent_token != first.agent_token
    print("PASS test_register_degrades_taken_name_and_reserves_it")


def test_payload_size_limits():
    _, _ = _fresh_modules()
    from pydantic import ValidationError
    from models import IterationCreate, MessageCreate, RenameRequest

    def _iter(**overrides):
        base = dict(agent_id="a", title="t", score=1.0, challenge=CHALLENGE)
        base.update(overrides)
        return IterationCreate(**base)

    for what, build in [
        ("notes > 64KB", lambda: _iter(notes="x" * (64 * 1024 + 1))),
        ("title > 300", lambda: _iter(title="t" * 301)),
        ("algorithm_code > 2MB", lambda: _iter(algorithm_code="x" * (2 * 1024 * 1024 + 1))),
        ("one algorithm_files entry > 2MB",
         lambda: _iter(algorithm_files={"mod.rs": "x" * (2 * 1024 * 1024 + 1)})),
        ("algorithm_files total > 8MB",
         lambda: _iter(algorithm_files={
             f"f{i}.rs": "x" * (2 * 1024 * 1024) for i in range(5)})),
        ("message content > 32KB",
         lambda: MessageCreate(agent_name="a", content="x" * (32 * 1024 + 1))),
        ("rename > 300", lambda: RenameRequest(agent_name="x" * 301)),
    ]:
        try:
            build()
        except ValidationError:
            continue
        raise AssertionError(f"{what} must be rejected")
    # Sane payloads still validate.
    _iter(notes="ok", algorithm_files={"mod.rs": "// fine\n"})
    print("PASS test_payload_size_limits")


async def test_broadcast_keeps_midflight_subscribers():
    _, server = _fresh_modules()
    import ws_events

    mgr = server.ConnectionManager()
    newcomer = type("WS", (), {})()

    class Good:
        async def send_json(self, payload):
            pass

    class Bad:
        async def send_json(self, payload):
            # Simulate a subscriber connecting while the broadcast awaits.
            mgr.connections.append(newcomer)
            raise RuntimeError("dead socket")

    good, bad = Good(), Bad()
    mgr.connections = [good, bad]
    await mgr.broadcast(ws_events.ResetEvt(challenge=CHALLENGE, timestamp=TS))
    assert bad not in mgr.connections, "failed socket must be pruned"
    assert good in mgr.connections, "healthy socket must survive"
    assert newcomer in mgr.connections, \
        "subscriber that connected mid-broadcast must not be evicted"
    print("PASS test_broadcast_keeps_midflight_subscribers")


async def _main():
    await test_token_resolution()
    await test_state_agent_view_requires_matching_token()
    await test_heartbeat_and_rename_require_matching_token()
    await test_iterations_and_messages_require_matching_token()
    await test_resolve_challenge_rejects_unknown()
    await test_register_degrades_taken_name_and_reserves_it()
    test_payload_size_limits()
    await test_broadcast_keeps_midflight_subscribers()
    print("\nAll auth/hardening tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
