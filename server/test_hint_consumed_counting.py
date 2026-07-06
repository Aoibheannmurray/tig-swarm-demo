"""Regression: the leaderboard counts CONSUMED hints, not OFFERED ones.

Standalone (`python server/test_hint_consumed_counting.py`). Fresh temp DB per test.

The hint lifecycle:
  - OFFER (GET /api/state while stagnating) bumps the raw
    `agent_challenge_state.tacit_knowledge_count` / `.inspiration_count` columns
    AND stashes `pending_hint`. A client stuck in a fetch->fail->retry loop can
    inflate these without ever publishing (observed 703 offers vs 6 consumed).
  - CONSUME (POST /api/iterations) reads `pending_hint`, stamps it onto
    `experiments.received_hint`, and clears it.

The leaderboard must derive its TK / Insp counts from `experiments.received_hint`
(consumed), NOT from the raw acs offer counters. This locks that in.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "knapsack"  # max-direction, positive feasible scores
TS = "2026-06-30T00:00:00Z"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _publish(server, req):
    """create_iteration requires the caller's token to resolve to
    req.agent_id; tests bypass HTTP, so pass the resolved id directly."""
    return await server.create_iteration(req, token_agent_id=req.agent_id)


async def _register(db, agent_id="agentA"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, tier) "
            "VALUES (?, ?, ?, ?, 'frontier')",
            (agent_id, "Agent A", TS, TS),
        )
        await conn.commit()


def _iter(agent_id, score):
    from models import IterationCreate
    return IterationCreate(
        agent_id=agent_id, title="t", strategy_tag="greedy",
        algorithm_code="// code\n", score=score, feasible=True,
        challenge=CHALLENGE, role="explorer",
    )


async def test_leaderboard_counts_consumed_not_offered():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)

    OFFERED, CONSUMED = 5, 2

    # Simulate OFFERED offers: bump the raw acs counter (what the offer path does).
    async with db.connect() as conn:
        await db.ensure_agent_challenge_state(conn, "agentA", CHALLENGE, TS)
        await db.increment_agent_challenge_counters(
            conn, "agentA", CHALLENGE, tacit_knowledge_inc=OFFERED,
        )
        await conn.commit()

    # CONSUME a hint CONSUMED times: stash pending_hint, then publish — the
    # server stamps experiments.received_hint and clears pending_hint.
    for i in range(CONSUMED):
        async with db.connect() as conn:
            await db.update_agent_challenge_state(
                conn, "agentA", CHALLENGE,
                set_fields={"pending_hint": "tacit_knowledge"},
            )
            await conn.commit()
        await _publish(server, _iter("agentA", 100.0 + i))

    # A plain iteration with no pending hint (not counted).
    await _publish(server, _iter("agentA", 50.0))

    async with db.connect() as conn:
        raw = await (await conn.execute(
            "SELECT tacit_knowledge_count FROM agent_challenge_state "
            "WHERE agent_id = ? AND challenge = ?",
            ("agentA", CHALLENGE),
        )).fetchone()
        consumed = await (await conn.execute(
            "SELECT COUNT(*) AS c FROM experiments "
            "WHERE agent_id = ? AND received_hint = 'tacit_knowledge'",
            ("agentA",),
        )).fetchone()
        lb = await db.compute_leaderboard(conn, CHALLENGE, None, direction="max")

    raw_offered = raw["tacit_knowledge_count"]
    assert raw_offered == OFFERED, f"raw acs offered should be {OFFERED}, got {raw_offered}"
    assert consumed["c"] == CONSUMED, f"experiments consumed should be {CONSUMED}, got {consumed['c']}"

    row = next(r for r in lb if r["agent_id"] == "agentA")
    assert row["tacit_knowledge_count"] == CONSUMED, (
        f"leaderboard must show CONSUMED ({CONSUMED}), got {row['tacit_knowledge_count']}"
    )
    assert row["tacit_knowledge_count"] != raw_offered, (
        "leaderboard is reading the OFFERED acs counter — regression!"
    )
    print("PASS test_leaderboard_counts_consumed_not_offered")


async def test_no_hint_means_zero_consumed():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)
    # Two iterations, no pending hint ever set.
    await _publish(server, _iter("agentA", 100.0))
    await _publish(server, _iter("agentA", 110.0))
    async with db.connect() as conn:
        lb = await db.compute_leaderboard(conn, CHALLENGE, None, direction="max")
    row = next(r for r in lb if r["agent_id"] == "agentA")
    assert row["tacit_knowledge_count"] == 0 and row["inspiration_count"] == 0, row
    print("PASS test_no_hint_means_zero_consumed")


async def _main():
    await test_leaderboard_counts_consumed_not_offered()
    await test_no_hint_means_zero_consumed()
    print("\nAll consumed-hint counting tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
