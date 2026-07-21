"""Tests for POST /api/admin/seeds — the read-only seed-pool listing.

The endpoint exists so the Admin Console can show whether a challenge's seed
pool is actually populated (twice now an "agents got the stub" incident came
down to an invisibly empty pool). It must return every row — infeasible and
harvested included — as metadata only, never the code bodies.

Standalone: `python server/test_admin_seeds.py` (no pytest in this repo).
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "knapsack"
TS = "2026-07-10T00:00:00Z"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _admin_key(db) -> str:
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT value FROM config WHERE key = 'admin_key'")).fetchone()
    return row["value"]


async def test_lists_all_rows_without_code():
    db, server = _fresh_modules()
    await db.init_db()
    from models import AdminSeedsQuery

    async with db.connect() as conn:
        await db.insert_seed(
            conn, CHALLENGE, "greedy", "// authored code\n" * 100,
            created_at=TS, source="authored", feasible=True,
        )
        await db.insert_seed(
            conn, CHALLENGE, "greedy", "// harvested\n",
            created_at=TS, source="harvested", score=42.0,
            feasible=False, kernel_code="// kernel\n",
            origin_agent_id="agentX",
        )
        # A different challenge's seed must NOT appear.
        await db.insert_seed(
            conn, "satisfiability", "other", "// sat\n",
            created_at=TS, source="authored", feasible=True,
        )
        await conn.commit()

    key = await _admin_key(db)
    res = await server.admin_list_seeds(
        AdminSeedsQuery(admin_key=key, challenge=CHALLENGE))
    assert res["challenge"] == CHALLENGE
    assert res["count"] == 2, res
    tags = {(s["strategy_tag"], s["source"]) for s in res["seeds"]}
    assert tags == {("greedy", "authored"), ("greedy", "harvested")}, tags

    harvested = next(s for s in res["seeds"] if s["source"] == "harvested")
    assert harvested["feasible"] == 0 and harvested["score"] == 42.0, harvested
    assert harvested["origin_agent_id"] == "agentX"
    assert harvested["kernel_chars"] > 0
    # Metadata only — no code bodies in the payload.
    for s in res["seeds"]:
        assert "algorithm_code" not in s and "kernel_code" not in s, s
        assert s["code_chars"] > 0
    print("PASS test_lists_all_rows_without_code")


async def test_empty_pool_and_bad_key():
    db, server = _fresh_modules()
    await db.init_db()
    from fastapi import HTTPException
    from models import AdminSeedsQuery

    key = await _admin_key(db)
    res = await server.admin_list_seeds(
        AdminSeedsQuery(admin_key=key, challenge=CHALLENGE))
    assert res["count"] == 0 and res["seeds"] == [], res

    try:
        await server.admin_list_seeds(
            AdminSeedsQuery(admin_key="wrong", challenge=CHALLENGE))
    except HTTPException as e:
        assert e.status_code == 403, e
    else:
        raise AssertionError("bad admin key must 403")
    print("PASS test_empty_pool_and_bad_key")


if __name__ == "__main__":
    asyncio.run(test_lists_all_rows_without_code())
    asyncio.run(test_empty_pool_and_bad_key())
    print("ALL PASS")
