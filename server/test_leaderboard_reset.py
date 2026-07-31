"""Regression tests for the admin "Reset leaderboard" action.

Runs standalone (`python server/test_leaderboard_reset.py`) and is also
pytest-compatible. Each test builds an isolated temp DB by pointing DATA_DIR at
a fresh directory *before* importing the server modules.

The bug: reset_challenge deleted `best_history` and `trajectory_bests`, but
`get_global_best` reads `experiments` — which the reset deliberately preserves.
So the reset cleared the CHART while the old peak kept winning the publish gate.
After a change that makes scores incomparable (new instance counts, a different
solver timeout), every legitimate new run scored lower and was classified "not
an improvement" forever, which is precisely what the button is reached for.

The fix is a per-challenge score epoch in `config`: rows published before the
reset stop counting as the global best, without deleting anything. These tests
pin the behaviour the fix exists to provide, plus the parts that must NOT change
(experiments survive; other challenges are untouched).
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "vehicle_routing"   # max-direction (swarm scores: higher is better)
OTHER_CHALLENGE = "knapsack"    # a second challenge, for the isolation check
TS = "2026-07-22T00:00:00Z"


def _fresh_modules():
    """Re-import db + server against a brand-new temp DB. Returns (db, server)."""
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _publish(server, req):
    return await server.create_iteration(req, token_agent_id=req.agent_id)


async def _register_agent(db, agent_id="agentA", name="Agent A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, name, TS, TS),
        )
        await conn.commit()


def _iter(agent_id, score, title, challenge=CHALLENGE):
    from models import IterationCreate
    return IterationCreate(
        agent_id=agent_id, title=title, strategy_tag="other",
        algorithm_code="// code", score=score, feasible=True,
        challenge=challenge,
    )


async def _reset(server, challenge=CHALLENGE):
    from models import AdminResetChallenge
    async with server.db.connect() as conn:
        cfg = await server.db.get_config(conn)
    return await server.admin_reset_challenge(AdminResetChallenge(
        admin_key=cfg.get("admin_key", ""), challenge=challenge,
    ))


async def test_lower_score_becomes_best_after_reset():
    """The point of the button: after a reset, a LOWER score must be accepted
    as the new global best — the instances changed, so the old number is not
    comparable to anything published now."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    strong = await _publish(server, _iter("agentA", 180_000.0, "pre-reset best"))
    assert strong.is_new_best is True, strong

    # Without a reset, a lower score is correctly rejected.
    worse = await _publish(server, _iter("agentA", 100_000.0, "lower, pre-reset"))
    assert worse.is_new_best is False, worse

    await _reset(server)

    after = await _publish(server, _iter("agentA", 100_000.0, "first post-reset run"))
    assert after.is_new_best is True, (
        "after a reset the next feasible publish must become the new best, "
        "even when it scores lower than the pre-reset peak", after)
    print("PASS test_lower_score_becomes_best_after_reset")


async def test_reset_preserves_research_history():
    """Non-destructive: the experiments (and so the inspiration matrix and the
    trajectory charts that read them) survive the reset."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _publish(server, _iter("agentA", 100_000.0, "run one"))
    await _publish(server, _iter("agentA", 120_000.0, "run two"))

    async with db.connect() as conn:
        before = (await (await conn.execute(
            "SELECT COUNT(*) c FROM experiments WHERE challenge = ?",
            (CHALLENGE,))).fetchone())["c"]
    assert before == 2, before

    res = await _reset(server)
    assert res["score_epoch"], res

    async with db.connect() as conn:
        after = (await (await conn.execute(
            "SELECT COUNT(*) c FROM experiments WHERE challenge = ?",
            (CHALLENGE,))).fetchone())["c"]
        # Leaderboard ordering reads best_ever_score; a surviving peak there
        # would contradict the reset best.
        peaks = await (await conn.execute(
            "SELECT best_ever_score FROM agent_challenge_state WHERE challenge = ?",
            (CHALLENGE,))).fetchall()
    assert after == before, ("experiments must not be deleted", after)
    assert all(r["best_ever_score"] is None for r in peaks), [dict(r) for r in peaks]
    print("PASS test_reset_preserves_research_history")


async def test_reset_is_scoped_to_one_challenge():
    """Resetting one challenge must not disturb another's best."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _publish(server, _iter("agentA", 100_000.0, "vrp best"))
    await _publish(server, _iter("agentA", 500.0, "knapsack best", OTHER_CHALLENGE))

    await _reset(server, CHALLENGE)

    async with db.connect() as conn:
        other = await db.get_global_best(conn, OTHER_CHALLENGE, direction="max")
        same = await db.get_global_best(conn, CHALLENGE, direction="max")
    assert other is not None and other["score"] == 500.0, other
    assert same is None, ("the reset challenge has no best until something is "
                          "published after the epoch", same)
    print("PASS test_reset_is_scoped_to_one_challenge")


async def test_epoch_is_reversible():
    """Nothing was deleted, so clearing the epoch row restores the old best —
    the escape hatch if a reset was fired by mistake."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _publish(server, _iter("agentA", 100_000.0, "pre-reset best"))
    await _reset(server)

    async with db.connect() as conn:
        assert await db.get_global_best(conn, CHALLENGE, direction="max") is None
        await conn.execute("DELETE FROM config WHERE key = ?",
                           (db.score_epoch_key(CHALLENGE),))
        await conn.commit()
        restored = await db.get_global_best(conn, CHALLENGE, direction="max")
    assert restored is not None and restored["score"] == 100_000.0, restored
    print("PASS test_epoch_is_reversible")


async def main():
    await test_lower_score_becomes_best_after_reset()
    await test_reset_preserves_research_history()
    await test_reset_is_scoped_to_one_challenge()
    await test_epoch_is_reversible()
    print("\nAll leaderboard-reset tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
