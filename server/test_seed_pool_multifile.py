"""Multi-file support in the SEED pool (initial pool), used when a mainnet
algorithm is seeded there. Mirrors test_seed_inactive_multifile but for the
`seed_pool` / `upsert_authored_seed` write path + the `/api/admin/seed_pool`
endpoint threading `algorithm_files` through.

Runs standalone (`python test_seed_pool_multifile.py` from the server dir).
"""

import asyncio
import json
import os
import sys
import tempfile

CHALLENGE = "hypergraph"  # GPU: exercises multiple .cu kernels
TS = "2026-07-15T00:00:00Z"
FILES = {
    "mod.rs": "use super::*;\npub fn solve_challenge() {}\n",
    "neighborhood.rs": "// helper module\n",
    "kernels.cu": "// primary kernel\n",
    "reduce.cu": "// second kernel\n",
}


def _fresh():
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    return db


def _mainnet_seeds(db_seeds):
    return [s for s in db_seeds if s["strategy_tag"] == "mainnet"]


async def test_multifile_seed_pool_roundtrip():
    db = _fresh()
    await db.init_db()
    async with db.connect() as conn:
        action = await db.upsert_authored_seed(
            conn, CHALLENGE, "mainnet", FILES["mod.rs"],
            created_at=TS, algorithm_files=json.dumps(FILES), kernel_code=None,
        )
        await conn.commit()
        assert action == "inserted", action
        seeds = _mainnet_seeds(await db.list_seeds(conn, CHALLENGE))
        assert len(seeds) == 1
        assert json.loads(seeds[0]["algorithm_files"]) == FILES
    print("PASS test_multifile_seed_pool_roundtrip")


async def test_reupsert_is_idempotent_then_updates_on_change():
    db = _fresh()
    await db.init_db()
    async with db.connect() as conn:
        kw = dict(created_at=TS, algorithm_files=json.dumps(FILES), kernel_code=None)
        assert await db.upsert_authored_seed(conn, CHALLENGE, "mainnet", FILES["mod.rs"], **kw) == "inserted"
        # identical -> no-op
        assert await db.upsert_authored_seed(conn, CHALLENGE, "mainnet", FILES["mod.rs"], **kw) == "unchanged"
        # a changed sidecar file -> updated (even though mod.rs is unchanged)
        changed = {**FILES, "neighborhood.rs": "// EDITED\n"}
        assert await db.upsert_authored_seed(
            conn, CHALLENGE, "mainnet", FILES["mod.rs"],
            created_at="t2", algorithm_files=json.dumps(changed), kernel_code=None,
        ) == "updated"
        await conn.commit()
        seeds = _mainnet_seeds(await db.list_seeds(conn, CHALLENGE))
        assert len(seeds) == 1  # updated in place, not duplicated
        assert json.loads(seeds[0]["algorithm_files"]) == changed
    print("PASS test_reupsert_is_idempotent_then_updates_on_change")


async def test_authored_and_mainnet_coexist_by_tag():
    db = _fresh()
    await db.init_db()
    async with db.connect() as conn:
        await db.upsert_authored_seed(conn, CHALLENGE, "mainnet", "// mainnet",
                                      created_at=TS, algorithm_files=json.dumps(FILES))
        await db.upsert_authored_seed(conn, CHALLENGE, "construction", "// authored single-file",
                                      created_at=TS)
        await conn.commit()
        tags = {s["strategy_tag"] for s in await db.list_seeds(conn, CHALLENGE)}
        assert {"mainnet", "construction"} <= tags, tags
    print("PASS test_authored_and_mainnet_coexist_by_tag")


async def _main():
    await test_multifile_seed_pool_roundtrip()
    await test_reupsert_is_idempotent_then_updates_on_change()
    await test_authored_and_mainnet_coexist_by_tag()
    print("\nAll seed-pool multi-file tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
