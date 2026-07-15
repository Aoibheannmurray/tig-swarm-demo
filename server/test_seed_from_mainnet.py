"""POST /api/admin/seed_from_mainnet — server-side mainnet seeding for the
Admin Console. The network fetch (mainnet_seed.fetch_top_reshaped) is stubbed;
this covers the deposit routing into the seed and/or inactive pools.

Standalone: `python server/test_seed_from_mainnet.py`.
"""

import asyncio
import json
import os
import sys
import tempfile

CHALLENGE = "knapsack"
TS = "2026-07-15T00:00:00Z"

_INFO = {
    "algo_name": "topalgo",
    "adoption": 37 * 10**16,
    "code_files": {"mod.rs": "use tig_challenges::knapsack::*;\nfn solve_challenge(){}\n",
                   "helpers.rs": "// helper\n"},
    "kernel_code": None,
}


def _fresh():
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = tempfile.mkdtemp()
    for mod in ("db", "server", "mainnet_seed"):
        sys.modules.pop(mod, None)
    import db
    import server
    import mainnet_seed
    # Stub the network fetch: knapsack succeeds, everything else "no algo".
    mainnet_seed.fetch_top_reshaped = lambda ch: (
        (dict(_INFO), "") if ch == CHALLENGE else (None, "no compiled mainnet algorithm found"))
    return db, server


async def _key(db):
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT value FROM config WHERE key = 'admin_key'")).fetchone()
    return row["value"]


async def test_seeds_seed_pool_by_default_challenge():
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet

    key = await _key(db)
    res = await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=CHALLENGE, target="seed_pool"))
    assert res["results"][0]["ok"] is True
    assert res["results"][0]["actions"]["seed_pool"] == "inserted"
    assert res["results"][0]["algorithm"] == "topalgo"
    async with db.connect() as conn:
        seeds = [s for s in await db.list_seeds(conn, CHALLENGE)
                 if s["strategy_tag"] == "mainnet"]
    assert len(seeds) == 1
    assert json.loads(seeds[0]["algorithm_files"]) == _INFO["code_files"]
    print("PASS test_seeds_seed_pool_by_default_challenge")


async def test_both_targets_deposit_seed_and_inactive():
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet

    key = await _key(db)
    res = await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=CHALLENGE, target="both"))
    acts = res["results"][0]["actions"]
    assert acts["seed_pool"] == "inserted" and acts["inactive"] == "seeded", acts
    async with db.connect() as conn:
        agent_id = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        assert await db.count_inactive_from_agent(conn, agent_id, CHALLENGE) == 1
    print("PASS test_both_targets_deposit_seed_and_inactive")


async def test_challenge_with_no_mainnet_is_skipped_not_failed():
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet

    key = await _key(db)
    res = await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge="vehicle_routing", target="seed_pool"))
    r = res["results"][0]
    assert r["ok"] is False and "no compiled" in r["reason"], r
    print("PASS test_challenge_with_no_mainnet_is_skipped_not_failed")


async def test_all_challenges_when_none_given():
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet

    async with db.connect() as conn:
        await db.upsert_challenge_config(conn, CHALLENGE, tracks="{}")
        await db.upsert_challenge_config(conn, "vehicle_routing", tracks="{}")
        await conn.commit()
    key = await _key(db)
    res = await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=None, target="seed_pool"))
    by_ch = {r["challenge"]: r for r in res["results"]}
    assert by_ch[CHALLENGE]["ok"] is True
    assert by_ch["vehicle_routing"]["ok"] is False
    print("PASS test_all_challenges_when_none_given")


async def test_bad_key_rejected():
    db, server = _fresh()
    await db.init_db()
    from fastapi import HTTPException
    from models import AdminSeedFromMainnet
    try:
        await server.admin_seed_from_mainnet(
            AdminSeedFromMainnet(admin_key="wrong", challenge=CHALLENGE))
    except HTTPException as e:
        assert e.status_code in (401, 403), e.status_code
        print("PASS test_bad_key_rejected")
        return
    raise AssertionError("bad admin key must be rejected")


async def _main():
    await test_seeds_seed_pool_by_default_challenge()
    await test_both_targets_deposit_seed_and_inactive()
    await test_challenge_with_no_mainnet_is_skipped_not_failed()
    await test_all_challenges_when_none_given()
    await test_bad_key_rejected()
    print("\nAll seed-from-mainnet tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
