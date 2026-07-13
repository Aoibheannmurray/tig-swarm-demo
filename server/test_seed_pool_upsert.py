"""Tests for authored seed-pool upsert semantics + duplicate cleanup.

Regression: when seed-pool diversity moved to similarity-based admission, the
(challenge, strategy_tag, source) UNIQUE index was dropped but
/api/admin/seed_pool kept INSERT OR IGNORE semantics that no longer ignored
anything — every `setup.py create` re-run duplicated each authored seed, and
an EDITED seed file never replaced the stale pool copy. Authored deposits are
now a true upsert (db.upsert_authored_seed) and init_db collapses legacy
duplicates.

Self-running: `python server/test_seed_pool_upsert.py` from the repo.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Isolate from the repo's real initial_algorithms/ (first-boot would seed the
# pool and skew counts) — same pattern as test_role_multifile_hpo.py.
os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _fresh_db():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "first_boot", "challenges"):
        sys.modules.pop(mod, None)
    import db
    return db


async def test_upsert_insert_unchanged_update():
    db = _fresh_db()
    await db.init_db()
    async with db.connect() as conn:
        a1 = await db.upsert_authored_seed(
            conn, "knapsack", "greedy", "code v1", created_at="t1")
        a2 = await db.upsert_authored_seed(
            conn, "knapsack", "greedy", "code v1", created_at="t2")
        a3 = await db.upsert_authored_seed(
            conn, "knapsack", "greedy", "code v2", created_at="t3",
            kernel_code="kern")
        await conn.commit()
        seeds = await db.list_seeds(conn, "knapsack")
    assert (a1, a2, a3) == ("inserted", "unchanged", "updated"), (a1, a2, a3)
    assert len(seeds) == 1, seeds
    assert seeds[0]["algorithm_code"] == "code v2"
    assert seeds[0]["kernel_code"] == "kern"
    print("PASS test_upsert_insert_unchanged_update")


async def test_upsert_leaves_harvested_seeds_alone():
    db = _fresh_db()
    await db.init_db()
    async with db.connect() as conn:
        # Harvested seeds may legitimately share a tag (similarity admission).
        await db.insert_seed(conn, "knapsack", "greedy", "harvest A",
                             created_at="t1", source="harvested")
        await db.insert_seed(conn, "knapsack", "greedy", "harvest B",
                             created_at="t2", source="harvested")
        await db.upsert_authored_seed(
            conn, "knapsack", "greedy", "authored", created_at="t3")
        await conn.commit()
        seeds = await db.list_seeds(conn, "knapsack")
    codes = sorted(s["algorithm_code"] for s in seeds)
    assert codes == ["authored", "harvest A", "harvest B"], codes
    print("PASS test_upsert_leaves_harvested_seeds_alone")


async def test_init_db_collapses_legacy_authored_duplicates():
    db = _fresh_db()
    await db.init_db()
    async with db.connect() as conn:
        # Simulate the duplicate pile-up from the no-dedupe era.
        for created, code in (("t1", "old"), ("t2", "mid"), ("t3", "new")):
            await db.insert_seed(conn, "knapsack", "greedy", code,
                                 created_at=created, source="authored")
        await db.insert_seed(conn, "knapsack", "greedy", "harvested copy",
                             created_at="t4", source="harvested")
        await conn.commit()
    await db.init_db()  # migrations are idempotent and run every boot
    async with db.connect() as conn:
        seeds = await db.list_seeds(conn, "knapsack")
    authored = [s for s in seeds if s["algorithm_code"] != "harvested copy"]
    assert len(authored) == 1, seeds
    assert authored[0]["algorithm_code"] == "new", authored  # newest row kept
    assert len(seeds) == 2, seeds  # harvested row untouched
    print("PASS test_init_db_collapses_legacy_authored_duplicates")


if __name__ == "__main__":
    asyncio.run(test_upsert_insert_unchanged_update())
    asyncio.run(test_upsert_leaves_harvested_seeds_alone())
    asyncio.run(test_init_db_collapses_legacy_authored_duplicates())
    print("ALL PASS")
