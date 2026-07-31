"""Tests for the negative-trajectory cull (`negative_trajectory_limit`).

Runs standalone (`python test_negative_trajectory_cull.py` from the server dir)
and is also pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.

The gap this closes: a trajectory that keeps making SMALL improvements while
still below zero resets `runs_since_improvement` on every win, so it never
trips `stagnation_limit` — yet `deposit_inactive` refuses negative scores, so
nothing harvestable can ever come out of the line. Observed on fable-swarm:
many multi-edit trajectories grinding along at negative scores indefinitely.

With `negative_trajectory_limit = N` (> 0), a trajectory whose best is still
not better than 0 after N edits is reset on the next /api/state poll, exactly
like a stagnation trip; the state response carries `reason: "negative_cull"`.
Default is 0 (disabled) — challenges whose feasible scores are inherently
negative (neuralnet) must not cull everything.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "job_scheduling"  # max-direction; positive = better than baseline


def _fresh_modules():
    """Re-import db + server against a brand-new temp DB. Returns (db, server)."""
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _register_agent(db, agent_id="agentA", name="Agent A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, name, "2026-07-06T00:00:00Z", "2026-07-06T00:00:00Z"),
        )
        await conn.commit()


async def _set_config(db, server, **kv):
    async with db.connect() as conn:
        for key, value in kv.items():
            await conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        await conn.commit()
    server._invalidate_caches()


def _iter(server, agent_id, score, title, feasible=True):
    from models import IterationCreate
    return IterationCreate(
        agent_id=agent_id, title=title, strategy_tag="other",
        algorithm_code=f"// code {title}", score=score, feasible=feasible,
        challenge=CHALLENGE,
    )


async def test_improving_but_negative_line_is_culled():
    """Three improving-but-negative edits never trip stagnation_limit, but
    must trip negative_trajectory_limit=3 with reason=negative_cull. The
    culled code must NOT land in the adoption pool (negative deposit)."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _set_config(db, server,
                      negative_trajectory_limit=3, stagnation_limit=50)

    # Each edit improves, so runs_since_improvement stays 0 throughout.
    for i, score in enumerate((-300.0, -250.0, -200.0)):
        res = await server.create_iteration(
            _iter(server, "agentA", score, f"edit {i}"),
            token_agent_id="agentA")
        assert res.beats_trajectory_best is True, res

    async with db.connect() as conn:
        acs = await db.get_agent_challenge_state(conn, "agentA", CHALLENGE)
        old_traj_id = acs["current_trajectory_id"]
        traj = await db.get_trajectory(conn, old_traj_id)
        assert traj["num_edits"] == 3, traj

    state = await server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentA")
    reset = state.get("trajectory_reset")
    assert reset is not None, "expected a trajectory reset, got none"
    assert reset.get("reason") == "negative_cull", reset

    async with db.connect() as conn:
        traj = await db.get_trajectory(conn, old_traj_id)
        assert traj["status"] == "inactive", traj
        pool = await db.get_inactive_with_deactivations(conn, CHALLENGE)
        assert pool == [], (
            "negative code must not be deposited into the adoption pool", pool)

    # The next poll starts clean — no repeated cull on the fresh trajectory.
    state2 = await server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentA")
    assert state2.get("trajectory_reset") is None, state2.get("trajectory_reset")
    print("PASS test_improving_but_negative_line_is_culled")


async def test_positive_line_is_not_culled():
    """A trajectory whose best crossed zero must survive past the limit."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _set_config(db, server,
                      negative_trajectory_limit=2, stagnation_limit=50)

    for i, score in enumerate((-100.0, 50.0, 60.0)):
        await server.create_iteration(_iter(server, "agentA", score, f"edit {i}"), token_agent_id="agentA")

    state = await server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentA")
    assert state.get("trajectory_reset") is None, state.get("trajectory_reset")
    assert state.get("current_trajectory_best") == 60.0, state
    print("PASS test_positive_line_is_not_culled")


async def test_cull_disabled_by_default():
    """With no config set (default 0), an improving negative line is never
    culled no matter how many edits it accumulates."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _set_config(db, server, stagnation_limit=50)

    for i in range(8):
        await server.create_iteration(
            _iter(server, "agentA", -800.0 + i, f"edit {i}"),
            token_agent_id="agentA")

    state = await server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentA")
    assert state.get("trajectory_reset") is None, state.get("trajectory_reset")
    print("PASS test_cull_disabled_by_default")


async def test_stagnation_reset_reports_stagnation_reason():
    """A classic stagnation trip must be labelled reason=stagnation so the
    two reset causes stay distinguishable in logs and chat."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _set_config(db, server,
                      negative_trajectory_limit=0, stagnation_limit=2)

    await server.create_iteration(_iter(server, "agentA", 100.0, "baseline"), token_agent_id="agentA")
    # Two non-improving edits -> runs_since_improvement = 2 = stagnation_limit.
    await server.create_iteration(_iter(server, "agentA", 90.0, "worse 1"), token_agent_id="agentA")
    await server.create_iteration(_iter(server, "agentA", 80.0, "worse 2"), token_agent_id="agentA")

    state = await server.get_state(agent_id="agentA", challenge=CHALLENGE, token_agent_id="agentA")
    reset = state.get("trajectory_reset")
    assert reset is not None, "expected a stagnation reset"
    assert reset.get("reason") == "stagnation", reset
    print("PASS test_stagnation_reset_reports_stagnation_reason")


if __name__ == "__main__":
    asyncio.run(test_improving_but_negative_line_is_culled())
    asyncio.run(test_positive_line_is_not_culled())
    asyncio.run(test_cull_disabled_by_default())
    asyncio.run(test_stagnation_reset_reports_stagnation_reason())
    print("ALL PASS")
