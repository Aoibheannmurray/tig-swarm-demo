"""Regression tests for the negative-score inactive-pool gate.

Runs standalone (`python test_inactive_pool_negative_gate.py` from the server
dir) and is pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.

The problem: the inactive trajectory pool (`inactive_algorithms`) is what
other agents adopt from on a fresh reset. A trajectory whose best score is
negative — whether a one-and-done weak attempt or an iterated line that never
climbed above zero — hands known-bad code to whoever draws it.

The gate (db.deposit_inactive): block any deposit whose score is negative,
regardless of edit count. Non-negative scores still deposit. Covered here.
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


async def test_multi_edit_negative_is_blocked():
    """>1 edit + negative score → not deposited (iterating doesn't redeem a dead end)."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=4)
    rowid = await _deposit(db, "t1", -2_200_000.0)
    assert rowid == -1, f"expected skip sentinel, got {rowid}"
    assert await _pool_size(db) == 0, "multi-edit negative trajectory polluted the pool"
    print("PASS test_multi_edit_negative_is_blocked")


async def test_multi_edit_positive_is_kept():
    """>1 edit + non-negative score → deposited."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=4)
    rowid = await _deposit(db, "t1", 1_000.0)
    assert rowid != -1, "iterated positive trajectory was wrongly blocked"
    assert await _pool_size(db) == 1
    print("PASS test_multi_edit_positive_is_kept")


async def test_negative_without_trajectory_is_blocked():
    """Negative score with no trajectory_id (e.g. admin seed) → not deposited."""
    db = _fresh_modules()
    await db.init_db()
    rowid = await _deposit(db, None, -1.0)
    assert rowid == -1, f"expected skip sentinel, got {rowid}"
    assert await _pool_size(db) == 0
    print("PASS test_negative_without_trajectory_is_blocked")


async def test_zero_score_is_kept():
    """Score of exactly 0 → deposited (gate is strictly negative)."""
    db = _fresh_modules()
    await db.init_db()
    await _make_trajectory(db, "t1", num_edits=1)
    rowid = await _deposit(db, "t1", 0.0)
    assert rowid != -1, "zero-score trajectory was wrongly blocked"
    assert await _pool_size(db) == 1
    print("PASS test_zero_score_is_kept")


async def _main():
    await test_single_edit_negative_is_blocked()
    await test_single_edit_positive_is_kept()
    await test_multi_edit_negative_is_blocked()
    await test_multi_edit_positive_is_kept()
    await test_negative_without_trajectory_is_blocked()
    await test_zero_score_is_kept()
    print("\nAll inactive-pool negative-gate tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
