"""Regression test: concurrent stagnation resets must not double-fire.

Runs standalone (`python test_concurrent_trajectory_reset.py` from the server
dir). Points DATA_DIR at a fresh temp dir before importing server modules.

The trajectory-reset machine (server/trajectory_reset.py) is a multi-step
read-modify-write: deactivate trajectory → pick from the inactive pool →
consume the pick → deposit the stagnated best → upsert/clear
trajectory_bests. It runs inside BEGIN IMMEDIATE and re-checks the stagnation
condition under the write lock, so two concurrent GET /api/state calls for
the same stagnated agent must serialize: exactly ONE resets (consuming the
pool seed once, depositing the old best once); the other observes the
post-reset state. Before the machine was transactional, both calls could pass
the check and each consume a seed / deposit the old best.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "knapsack"  # max-direction, positive feasible scores
TS = "2026-07-06T00:00:00Z"
SEED_CODE = "// adopted seed\n"
MY_CODE = "// my stagnated best\n"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "trajectory_reset", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _publish(server, req):
    """create_iteration requires the caller's token to resolve to
    req.agent_id; tests bypass HTTP, so pass the resolved id directly."""
    return await server.create_iteration(req, token_agent_id=req.agent_id)


async def _register_agent(db, agent_id="agentA"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, tier) "
            "VALUES (?, ?, ?, ?, 'frontier')",
            (agent_id, f"Agent {agent_id}", TS, TS),
        )
        await conn.commit()


async def _stagnate(db, server, agent_id="agentA"):
    """Post one good iteration (creates traj_best + current_trajectory_id),
    then force runs_since_improvement past the stagnation limit."""
    from models import IterationCreate
    await _publish(server, IterationCreate(
        agent_id=agent_id, title="seed", strategy_tag="greedy",
        algorithm_code=MY_CODE, score=100.0, feasible=True,
        challenge=CHALLENGE, role="explorer",
    ))
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE agent_challenge_state SET runs_since_improvement = 99 "
            "WHERE agent_id = ? AND challenge = ?",
            (agent_id, CHALLENGE),
        )
        await conn.commit()


async def _state(server, agent_id="agentA"):
    return await server.get_state(
        agent_id=agent_id, challenge=CHALLENGE, role="explorer",
        token_agent_id=agent_id,
    )


async def test_concurrent_reset_fires_once():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _stagnate(db, server)
    # One scored pool seed. With 1 trajectory and 0 deactivations the fresh
    # start rule (n^1.5 < D) is false, so a reset MUST take the
    # adopted_inactive branch and consume this row.
    async with db.connect() as conn:
        src = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        await db.deposit_inactive(conn, src, CHALLENGE, SEED_CODE, 50.0, TS)
        await conn.commit()

    s1, s2 = await asyncio.gather(_state(server), _state(server))

    # Exactly one call performed the reset; the loser re-read post-reset
    # state instead of resetting again.
    resets = [s for s in (s1, s2) if s.get("trajectory_reset")]
    assert len(resets) == 1, (
        f"expected exactly one reset, got {len(resets)}: "
        f"{[s.get('trajectory_reset') for s in (s1, s2)]}"
    )
    r = resets[0]["trajectory_reset"]
    assert r["type"] == "adopted_inactive", r
    assert r["prior_score"] == 50.0, r

    # BOTH responses serve the adopted floor: counters zeroed, seed code.
    for s in (s1, s2):
        assert s["my_runs_since_improvement"] == 0, s["my_runs_since_improvement"]
        assert s["current_trajectory_best"] == 50.0, s["current_trajectory_best"]
        assert s["best_algorithm_code"] == SEED_CODE, s["best_algorithm_code"]

    # Pool: the admin seed was consumed exactly once, and the stagnated best
    # was deposited exactly once — a double reset would leave two deposits
    # (and/or re-consume the winner's deposit).
    async with db.connect() as conn:
        rows = await db.get_inactive_with_deactivations(conn, CHALLENGE)
        n_traj, total_deact = await db.trajectory_counts(conn, CHALLENGE)
    assert len(rows) == 1, f"expected exactly one pool row, got {rows}"
    assert rows[0]["agent_id"] == "agentA", rows[0]
    assert rows[0]["score"] == 100.0, rows[0]
    assert rows[0]["algorithm_code"] == MY_CODE, rows[0]
    # The old trajectory was deactivated exactly once.
    assert total_deact == 1, (n_traj, total_deact)
    print("PASS test_concurrent_reset_fires_once")


async def test_machine_level_concurrent_reset():
    """Drive a second maybe_reset_trajectory attempt while the first is
    paused MID-MACHINE (right after taking its write lock). The second
    attempt must park on BEGIN IMMEDIATE until the first commits, then see
    the re-checked stagnation condition fail and return None — one reset,
    one pool seed consumed. Without the BEGIN IMMEDIATE + re-check, the
    second attempt would run the whole machine concurrently and
    double-consume."""
    db, server = _fresh_modules()
    import trajectory_reset
    await db.init_db()
    await _register_agent(db)
    await _stagnate(db, server)
    async with db.connect() as conn:
        src = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        await db.deposit_inactive(conn, src, CHALLENGE, SEED_CODE, 50.0, TS)
        await conn.commit()

    cutoff_ts = await server.inactive_cutoff()
    a_locked = asyncio.Event()
    release_a = asyncio.Event()

    class _PauseAfterBegin:
        """Wraps attempt A's connection: pause right after BEGIN IMMEDIATE
        so the test can start attempt B while A holds the write lock."""
        def __init__(self, conn):
            self._conn = conn

        async def execute(self, sql, *args):
            cur = await self._conn.execute(sql, *args)
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                a_locked.set()
                await release_a.wait()
            return cur

        def __getattr__(self, name):
            return getattr(self._conn, name)

    async def _attempt(wrap: bool):
        async with db.connect() as conn:
            target = _PauseAfterBegin(conn) if wrap else conn
            return await trajectory_reset.maybe_reset_trajectory(
                target, agent_id="agentA", challenge=CHALLENGE,
                direction="max", cutoff_ts=cutoff_ts, stagnation_limit=5,
                agent_tier="frontier", agent_role="explorer",
                seed_fn=server.seed_for_agent, timestamp=server.now(),
            )

    task_a = asyncio.create_task(_attempt(wrap=True))
    await a_locked.wait()                # A holds the write lock, mid-machine
    task_b = asyncio.create_task(_attempt(wrap=False))
    await asyncio.sleep(0.2)             # B is now parked on A's write lock
    release_a.set()
    r_a, r_b = await asyncio.gather(task_a, task_b)

    assert r_a is not None, "paused attempt must still complete its reset"
    assert r_b is None, f"second attempt must lose the race, got {r_b}"
    assert r_a.reset_info["type"] == "adopted_inactive", r_a.reset_info
    assert r_a.current_trajectory_best == 50.0, r_a

    async with db.connect() as conn:
        rows = await db.get_inactive_with_deactivations(conn, CHALLENGE)
        n_traj, total_deact = await db.trajectory_counts(conn, CHALLENGE)
        acs = await db.get_agent_challenge_state(conn, "agentA", CHALLENGE)
    assert len(rows) == 1 and rows[0]["score"] == 100.0, rows
    assert total_deact == 1, (n_traj, total_deact)
    assert acs["runs_since_improvement"] == 0, acs
    print("PASS test_machine_level_concurrent_reset")


async def test_sequential_reset_still_works():
    """Sanity: the non-concurrent path (one caller) still resets normally."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _stagnate(db, server)
    async with db.connect() as conn:
        src = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        await db.deposit_inactive(conn, src, CHALLENGE, SEED_CODE, 50.0, TS)
        await conn.commit()

    s = await _state(server)
    assert s["trajectory_reset"] is not None
    assert s["trajectory_reset"]["type"] == "adopted_inactive"
    # Next poll: no reset (counter is back at 0), still on the adopted floor.
    s2 = await _state(server)
    assert s2["trajectory_reset"] is None
    assert s2["current_trajectory_best"] == 50.0
    print("PASS test_sequential_reset_still_works")


async def _main():
    await test_concurrent_reset_fires_once()
    await test_machine_level_concurrent_reset()
    await test_sequential_reset_still_works()
    print("\nAll concurrent trajectory-reset tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
