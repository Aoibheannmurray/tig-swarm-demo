"""Tests for the contributor-owned `seeded_start` override.

`seeded_start` (per-agent in fleet.config.json, reported on each /api/state
poll like `role`) overrides seed_for_agent's tier/role/GPU seeding policy:
True forces the working-code chain (seed pool → best peer → stub), False
forces the bare stub, None/absent keeps the auto policy.

Runs standalone (`python test_seeded_start_override.py` from the server dir)
and is also pytest-compatible. Each test builds an isolated temp DB by
pointing DATA_DIR at a fresh directory *before* importing the server modules.
"""

import asyncio
import os
import sys
import tempfile

CPU_CHALLENGE = "satisfiability"        # is_gpu=False: frontier explorers keep the stub
GPU_CHALLENGE = "neuralnet_optimizer"   # is_gpu=True: everyone is seeded by default
TS = "2026-07-08T00:00:00Z"
SEED_CODE = "// pool seed"


def _fresh_modules():
    """Re-import db + server against a brand-new temp DB. Returns (db, server)."""
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _setup(challenge):
    """Fresh modules + one registered agent + one pool seed for `challenge`."""
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            ("agentA", "Agent A", TS, TS),
        )
        await db.insert_seed(
            conn, challenge, "other", SEED_CODE, created_at=TS,
        )
        await conn.commit()
    return db, server


async def _pick(db, server, challenge, tier, role, seeded):
    async with db.connect() as conn:
        code, _kernel, _files, start = await server.seed_for_agent(
            conn, "agentA", challenge, tier, role,
            direction="max", cutoff_ts=TS, seeded=seeded,
        )
    return code, start


async def test_auto_policy_unchanged():
    """seeded=None keeps today's behavior: frontier explorer on a CPU
    challenge bootstraps from the stub; standard tier gets a pool seed."""
    db, server = await _setup(CPU_CHALLENGE)
    _, start = await _pick(db, server, CPU_CHALLENGE, "frontier", "explorer", None)
    assert start == "stub", start
    code, start = await _pick(db, server, CPU_CHALLENGE, "standard", "explorer", None)
    assert start == "seed" and code == SEED_CODE, (start, code)
    print("PASS test_auto_policy_unchanged")


async def test_true_seeds_a_frontier_explorer():
    """seeded=True forces working code where the auto policy says stub."""
    db, server = await _setup(CPU_CHALLENGE)
    code, start = await _pick(db, server, CPU_CHALLENGE, "frontier", "explorer", True)
    assert start == "seed" and code == SEED_CODE, (start, code)
    print("PASS test_true_seeds_a_frontier_explorer")


async def test_false_forces_the_stub():
    """seeded=False forces the stub even where the auto policy seeds:
    standard tier, exploiter role, and GPU challenges alike."""
    db, server = await _setup(CPU_CHALLENGE)
    _, start = await _pick(db, server, CPU_CHALLENGE, "standard", "exploiter", False)
    assert start == "stub", start

    db, server = await _setup(GPU_CHALLENGE)
    _, start = await _pick(db, server, GPU_CHALLENGE, "frontier", "explorer", False)
    assert start == "stub", start
    print("PASS test_false_forces_the_stub")


async def test_true_still_falls_back_when_pool_is_empty():
    """seeded=True with an empty pool and no peers falls through the chain
    to the stub instead of erroring."""
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            ("agentA", "Agent A", TS, TS),
        )
        await conn.commit()
    _, start = await _pick(db, server, CPU_CHALLENGE, "frontier", "explorer", True)
    assert start == "stub", start
    print("PASS test_true_still_falls_back_when_pool_is_empty")


async def _main():
    await test_auto_policy_unchanged()
    await test_true_seeds_a_frontier_explorer()
    await test_false_forces_the_stub()
    await test_true_still_falls_back_when_pool_is_empty()
    print("ALL PASS")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_main())
