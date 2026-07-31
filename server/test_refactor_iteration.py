"""Tests for the `iteration_type="refactor"` publish path (cleaner agent —
docs/cleaner-agent.md).

A refactor is a behavior-preserving bloat reduction: the client benchmarks the
lean code and publishes it with a score at/near the parent's. The server must
  1. swap the trajectory-best CODE (and files map) for the lean version,
  2. KEEP the recorded best score (no ratchet erosion),
  3. count it as neither an improvement (no momentum/HPO credit) nor
     stagnation (runs_since_improvement untouched),
  4. preserve the parent's tuned hyperparameters when the client omits them,
  5. degrade safely: a refactor that outright beats the parent is a normal
     improvement; an infeasible one is a normal failed iteration.

Standalone (`python test_refactor_iteration.py` from the server dir); builds
an isolated temp DB by pointing DATA_DIR at a fresh dir before imports.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "job_scheduling"  # max-direction
TS = "2026-07-03T00:00:00Z"

FAT_CODE = "// fat\n" + "x" * 1000
LEAN_CODE = "// lean\n" + "x" * 100
FAT_FILES = {"mod.rs": FAT_CODE, "dup.rs": FAT_CODE}
LEAN_FILES = {"mod.rs": LEAN_CODE}


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


async def _register_agent(db, agent_id="agentA", name="Agent A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, name, TS, TS),
        )
        await conn.commit()


def _iter(score, feasible=True, *, code=FAT_CODE, files=None,
          iteration_type="mutation", hyperparameters=None, title="t"):
    from models import IterationCreate
    return IterationCreate(
        agent_id="agentA", title=title, strategy_tag="other",
        algorithm_code=code, algorithm_files=files,
        score=score, feasible=feasible, challenge=CHALLENGE,
        iteration_type=iteration_type, hyperparameters=hyperparameters,
    )


async def _trajectory_best(db):
    async with db.connect() as conn:
        return await db.get_trajectory_best(conn, "agentA", CHALLENGE)


async def _acs(db):
    async with db.connect() as conn:
        return await db.get_agent_challenge_state(conn, "agentA", CHALLENGE)


async def test_refactor_swaps_code_keeps_score():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    await _publish(server, _iter(
        1000.0, files=FAT_FILES,
        hyperparameters={"n=50,s=JOB_SHOP": {"iters": 7}}, title="parent"))
    acs0 = await _acs(db)
    assert acs0["improvements"] == 1 and acs0["runs_since_improvement"] == 0

    resp = await _publish(server, _iter(
        990.0, code=LEAN_CODE, files=LEAN_FILES,
        iteration_type="refactor", title="refactor"))
    assert resp.beats_trajectory_best is False, resp

    tb = await _trajectory_best(db)
    assert tb["algorithm_code"] == LEAN_CODE, "lean code must be adopted"
    assert "dup.rs" not in (tb["algorithm_files"] or ""), \
        "lean files map must replace the fat one"
    assert tb["score"] == 1000.0, "parent score must be KEPT (no erosion)"
    assert '"iters": 7' in (tb["hyperparameters"] or ""), \
        "parent's tuned hyperparameters must be preserved when client omits"

    acs = await _acs(db)
    assert acs["improvements"] == 1, "refactor must not count as improvement"
    assert acs["runs_since_improvement"] == 0, \
        "refactor must not count as stagnation"
    assert acs["experiments_completed"] == 2, "refactor still counts as a run"
    print("PASS refactor swaps code, keeps score, no improvement/stagnation")


async def test_refactor_that_beats_is_a_normal_improvement():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    await _publish(server, _iter(1000.0, title="parent"))
    resp = await _publish(server, _iter(
        1010.0, code=LEAN_CODE, iteration_type="refactor", title="lucky refactor"))
    assert resp.beats_trajectory_best is True
    tb = await _trajectory_best(db)
    assert tb["score"] == 1010.0 and tb["algorithm_code"] == LEAN_CODE
    assert (await _acs(db))["improvements"] == 2
    print("PASS refactor that beats the parent is a normal improvement")


async def test_infeasible_refactor_is_a_normal_failure():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    await _publish(server, _iter(1000.0, title="parent"))
    await _publish(server, _iter(
        995.0, feasible=False, code=LEAN_CODE,
        iteration_type="refactor", title="broken refactor"))
    tb = await _trajectory_best(db)
    assert tb["algorithm_code"] == FAT_CODE and tb["score"] == 1000.0, \
        "infeasible refactor must not touch the trajectory best"
    acs = await _acs(db)
    assert acs["runs_since_improvement"] == 1, \
        "infeasible refactor falls through to the normal failed path"
    print("PASS infeasible refactor leaves the trajectory best untouched")


async def test_refactor_without_parent_falls_through():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    resp = await _publish(server, _iter(
        500.0, code=LEAN_CODE, iteration_type="refactor", title="orphan refactor"))
    assert resp.beats_trajectory_best is True, \
        "no parent -> first feasible score is just a normal first best"
    tb = await _trajectory_best(db)
    assert tb["score"] == 500.0
    print("PASS refactor with no parent behaves as a normal first iteration")


async def test_client_hyperparameters_override_parent():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    await _publish(server, _iter(
        1000.0, hyperparameters={"n=50,s=JOB_SHOP": {"iters": 7}}, title="parent"))
    await _publish(server, _iter(
        992.0, code=LEAN_CODE, iteration_type="refactor",
        hyperparameters={"n=50,s=JOB_SHOP": {"iters": 9}}, title="refactor"))
    tb = await _trajectory_best(db)
    assert '"iters": 9' in (tb["hyperparameters"] or "")
    print("PASS client-sent hyperparameters override the parent's")


async def main():
    await test_refactor_swaps_code_keeps_score()
    await test_refactor_that_beats_is_a_normal_improvement()
    await test_infeasible_refactor_is_a_normal_failure()
    await test_refactor_without_parent_falls_through()
    await test_client_hyperparameters_override_parent()
    print("\nAll refactor-iteration tests passed.")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(main())
