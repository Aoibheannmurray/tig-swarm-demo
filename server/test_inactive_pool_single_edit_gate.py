"""Regression tests for the single-edit / negative-score inactive-pool gate.

Runs standalone (`python test_inactive_pool_single_edit_gate.py` from the server
dir) and is pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.

The problem: an agent makes ONE algorithm that scores below zero, then fails to
make another edit and goes offline. Its trajectory best would be deposited into
the inactive trajectory pool (`inactive_algorithms`), where other agents adopt
it on a fresh reset — polluting the pool with a one-and-done weak attempt.

The gate (db.deposit_inactive): block the deposit when the trajectory has <= 1
edit AND its score is negative. Trajectories that iterated (>1 edit) or whose
single shot scored >= 0 still deposit. Covered here.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "neuralnet_optimizer"
TS = "2026-06-09T00:00:00Z"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    return db


async def _make_trajectory(db, traj_id, num_edits):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES ('agentA', 'Agent A', ?, ?)",
            (TS, TS),
        )
        await conn.execute(
            "INSERT INTO trajectories (id, challenge, started_at, status, num_edits) "
            "VALUES (?, ?, ?, 'active', ?)",
            (traj_id, CHALLENGE, TS, num_edits),
        )
        await conn.commit()


async def _pool_size(db):
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(*) c FROM inactive_algorithms WHERE challenge = ?",
            (CHALLENGE,),
        )).fetchone()
    return row["c"]


async def _deposit(db, traj_id, score):
    async with db.connect() as conn:
        rowid = await db.deposit_inactive(
            conn, "agentA", CHALLENGE, "// code", score, TS,
            trajectory_id=traj_id,
        )
        await conn.commit()
    return rowid


async def test_single_edit_negative_is_blocked():
    """1 edit + negative score → not deposited."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=1)
    rowid = await _deposit(db, "t1", -2_200_000.0)
    assert rowid == -1, f"expected skip sentinel, got {rowid}"
    assert await _pool_size(db) == 0, "single-edit negative trajectory polluted the pool"
    print("PASS test_single_edit_negative_is_blocked")


async def test_single_edit_positive_is_kept():
    """1 edit + non-negative score → deposited (a good one-shot still seeds)."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=1)
    rowid = await _deposit(db, "t1", 5_000_000.0)
    assert rowid != -1, "good single-edit trajectory was wrongly blocked"
    assert await _pool_size(db) == 1
    print("PASS test_single_edit_positive_is_kept")


async def test_multi_edit_negative_is_kept():
    """>1 edit + negative score → deposited (a real iterated line, kept)."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=4)
    rowid = await _deposit(db, "t1", -2_200_000.0)
    assert rowid != -1, "iterated trajectory was wrongly blocked"
    assert await _pool_size(db) == 1
    print("PASS test_multi_edit_negative_is_kept")


async def test_zero_edit_negative_is_blocked():
    """0 edits + negative score → not deposited (edge of the <= 1 boundary)."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=0)
    rowid = await _deposit(db, "t1", -1.0)
    assert rowid == -1, f"expected skip sentinel, got {rowid}"
    assert await _pool_size(db) == 0
    print("PASS test_zero_edit_negative_is_blocked")


async def _main():
    await test_single_edit_negative_is_blocked()
    await test_single_edit_positive_is_kept()
    await test_multi_edit_negative_is_kept()
    await test_zero_edit_negative_is_blocked()
    print("\nAll inactive-pool single-edit-gate tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
