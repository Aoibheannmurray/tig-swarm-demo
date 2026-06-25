"""Tests for benchmark-on-first-adoption of unscored seeds (Component 3).

Runs standalone (`python test_seed_benchmark_on_adoption.py` from the server
dir). Points DATA_DIR at a fresh temp dir before importing server modules.

When a stagnated agent adopts an inactive-pool entry that was deposited WITHOUT
a score (admin/mainnet seed), the server's trajectory_reset must carry
`needs_benchmark: True` so the agent benchmarks the seed unchanged before
mutating (establishing the seed's true score as the trajectory floor). A seed
that already has a score inherits it and needs no benchmark pass.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "knapsack"  # max-direction, positive feasible scores
TS = "2026-06-25T00:00:00Z"
SEED_FILES = {
    "mod.rs": "use super::*;\npub fn solve_challenge() {}\n",
    "kernels.cu": "// k\n",
    "extra.cu": "// second kernel preserved\n",
}


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _register_agent(db, agent_id="agentA"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, tier) "
            "VALUES (?, ?, ?, ?, 'frontier')",
            (agent_id, f"Agent {agent_id}", TS, TS),
        )
        await conn.commit()


async def _stagnate(db, server, agent_id="agentA"):
    """Post one good iteration (creates traj_best + current_trajectory_id), then
    force runs_since_improvement up to the stagnation limit."""
    from models import IterationCreate
    await server.create_iteration(IterationCreate(
        agent_id=agent_id, title="seed", strategy_tag="greedy",
        algorithm_code="// code\n", score=100.0, feasible=True, challenge=CHALLENGE,
        role="explorer",
    ))
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE agent_challenge_state SET runs_since_improvement = 99 "
            "WHERE agent_id = ? AND challenge = ?",
            (agent_id, CHALLENGE),
        )
        await conn.commit()


async def _deposit_seed(db, score):
    async with db.connect() as conn:
        src = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        import server
        await db.deposit_inactive(
            conn, src, CHALLENGE, SEED_FILES["mod.rs"], score, TS,
            algorithm_files=server._files_json(SEED_FILES),
        )
        await conn.commit()


async def test_unscored_seed_sets_needs_benchmark():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _stagnate(db, server)
    await _deposit_seed(db, score=None)  # admin/mainnet seed, no score

    state = await server.get_state(agent_id="agentA", challenge=CHALLENGE, role="explorer")
    reset = state.get("trajectory_reset")
    assert reset is not None, "no trajectory_reset emitted on stagnation"
    assert reset["type"] == "adopted_inactive", f"expected adoption, got {reset}"
    assert reset.get("needs_benchmark") is True, f"unscored seed must flag benchmark: {reset}"
    assert reset.get("prior_score") is None, reset
    # The adopted multi-.cu map flows to the agent intact.
    got = server._row_files({"algorithm_files": state.get("best_algorithm_files")
                             and __import__("json").dumps(state["best_algorithm_files"])})
    assert got == SEED_FILES, f"adopted map not delivered intact: {got}"
    print("PASS test_unscored_seed_sets_needs_benchmark")


async def test_scored_seed_does_not_set_needs_benchmark():
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _stagnate(db, server)
    await _deposit_seed(db, score=4242.0)  # already benchmarked

    state = await server.get_state(agent_id="agentA", challenge=CHALLENGE, role="explorer")
    reset = state.get("trajectory_reset")
    assert reset is not None and reset["type"] == "adopted_inactive", reset
    assert reset.get("needs_benchmark") is False, f"scored seed must NOT flag benchmark: {reset}"
    assert reset.get("prior_score") == 4242.0, reset
    print("PASS test_scored_seed_does_not_set_needs_benchmark")


async def _main():
    await test_unscored_seed_sets_needs_benchmark()
    await test_scored_seed_does_not_set_needs_benchmark()
    print("\nAll Component 3 benchmark-on-adoption tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
