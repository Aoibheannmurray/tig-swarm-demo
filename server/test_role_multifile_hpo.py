"""Integration tests for the role tag, multi-file storage, seed-pool admission,
and the HPO has_tuned gate signal.

Standalone: `python server/test_role_multifile_hpo.py` (no pytest in this repo).
Each test points DATA_DIR at a fresh temp dir before importing server modules.
"""

import asyncio
import json
import os
import sys
import tempfile

CHALLENGE = "knapsack"  # max-direction, positive feasible scores
TS = "2026-06-23T00:00:00Z"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    # Running from a repo checkout, first-boot seeding would find the real
    # initial_algorithms/ tree and deposit authored seeds into the fresh DB,
    # breaking the exact seed-pool counts below. Point it at an empty dir
    # (same isolation as test_first_boot.py).
    os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = tempfile.mkdtemp()
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _publish(server, req):
    """create_iteration requires the caller's token to resolve to
    req.agent_id; tests bypass HTTP, so pass the resolved id directly."""
    return await server.create_iteration(req, token_agent_id=req.agent_id)


async def _register_agent(db, agent_id="agentA", name=None, tier="frontier"):
    name = name or f"Agent {agent_id}"
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, tier) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, name, TS, TS, tier),
        )
        await conn.commit()


def _iter(agent_id, score, *, role=None, algorithm_files=None, code="// code\n",
          hyperparameters=None, title="t"):
    from models import IterationCreate
    return IterationCreate(
        agent_id=agent_id, title=title, strategy_tag="greedy",
        algorithm_code=code, score=score, feasible=True, challenge=CHALLENGE,
        role=role, algorithm_files=algorithm_files, hyperparameters=hyperparameters,
    )


async def test_role_stored_on_hypothesis():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _publish(server, _iter("agentA", 100.0, role="explorer"))
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT role FROM hypotheses WHERE agent_id = ?", ("agentA",))).fetchone()
    assert row["role"] == "explorer", dict(row)
    print("PASS test_role_stored_on_hypothesis")


async def test_multifile_round_trips_to_storage():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    files = {"mod.rs": "use super::*;\nmod helpers;\n", "helpers.rs": "pub fn h() {}\n"}
    await _publish(server, 
        _iter("agentA", 100.0, role="explorer", algorithm_files=files,
              code=files["mod.rs"]))
    async with db.connect() as conn:
        exp = await (await conn.execute(
            "SELECT algorithm_files FROM experiments WHERE agent_id = ?",
            ("agentA",))).fetchone()
        best = await (await conn.execute(
            "SELECT algorithm_files FROM trajectory_bests WHERE agent_id = ?",
            ("agentA",))).fetchone()
    assert json.loads(exp["algorithm_files"]) == files, exp["algorithm_files"]
    assert json.loads(best["algorithm_files"]) == files, best["algorithm_files"]
    print("PASS test_multifile_round_trips_to_storage")


async def test_frontier_multifile_is_harvested_as_seed():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db, tier="frontier")
    files = {"mod.rs": "use super::*;\nfn solve_challenge() {}\n", "h.rs": "// helper\n"}
    await _publish(server, 
        _iter("agentA", 100.0, role="explorer", algorithm_files=files,
              code=files["mod.rs"]))
    async with db.connect() as conn:
        seeds = await db.list_seeds(conn, CHALLENGE)
    assert len(seeds) == 1, seeds
    assert json.loads(seeds[0]["algorithm_files"]) == files, seeds[0]
    print("PASS test_frontier_multifile_is_harvested_as_seed")


async def test_standard_tier_does_not_harvest():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db, tier="standard")
    await _publish(server, _iter("agentA", 100.0, role="exploiter"))
    async with db.connect() as conn:
        seeds = await db.list_seeds(conn, CHALLENGE)
    assert seeds == [], seeds
    print("PASS test_standard_tier_does_not_harvest")


async def test_near_duplicate_seed_rejected():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db, "a1", tier="frontier")
    await _register_agent(db, "a2", tier="frontier")
    code = "use super::*;\nfn solve_challenge() {\n    let x = greedy_pick();\n}\n"
    await _publish(server, _iter("a1", 100.0, role="explorer", code=code))
    # An (almost) identical algorithm from another frontier agent must NOT be
    # admitted as a second seed — diversity is by code similarity now.
    await _publish(server, _iter("a2", 200.0, role="explorer", code=code))
    async with db.connect() as conn:
        seeds = await db.list_seeds(conn, CHALLENGE)
    assert len(seeds) == 1, f"near-duplicate should be rejected, got {len(seeds)} seeds"
    print("PASS test_near_duplicate_seed_rejected")


async def test_trajectory_has_tuned():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    async with db.connect() as conn:
        # Untuned experiment on traj T → has_tuned False.
        await conn.execute(
            "INSERT INTO experiments (id, agent_id, challenge, score, feasible, "
            "trajectory_id, created_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            ("e1", "agentA", CHALLENGE, 100.0, "T", TS))
        await conn.commit()
        assert await db.trajectory_has_tuned(conn, "T") is False
        # A tuned experiment (non-null hyperparameters) flips it True.
        await conn.execute(
            "INSERT INTO experiments (id, agent_id, challenge, score, feasible, "
            "trajectory_id, hyperparameters, created_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            ("e2", "agentA", CHALLENGE, 110.0, "T", json.dumps({"t": {"a": 1}}), TS))
        await conn.commit()
        assert await db.trajectory_has_tuned(conn, "T") is True
        # A different trajectory with no tuning stays False.
        assert await db.trajectory_has_tuned(conn, "OTHER") is False
        assert await db.trajectory_has_tuned(conn, None) is False
    print("PASS test_trajectory_has_tuned")


if __name__ == "__main__":
    asyncio.run(test_role_stored_on_hypothesis())
    asyncio.run(test_multifile_round_trips_to_storage())
    asyncio.run(test_frontier_multifile_is_harvested_as_seed())
    asyncio.run(test_standard_tier_does_not_harvest())
    asyncio.run(test_near_duplicate_seed_rejected())
    asyncio.run(test_trajectory_has_tuned())
    print("\nAll role/multifile/HPO tests passed.")
