"""Tests for multi-file / multi-`.cu` inactive-pool seeding (Component 1).

Runs standalone (`python test_seed_inactive_multifile.py` from the server dir).
Builds an isolated temp DB by pointing DATA_DIR at a fresh directory *before*
importing the server modules.

What this covers: the `seed_inactive` write path now carries a full
{relpath: content} map (multiple `.rs` modules AND multiple `.cu` kernels, names
preserved) end-to-end — deposited into `inactive_algorithms.algorithm_files` and
read back intact by the adoption query (`get_inactive_with_deactivations`), which
is what `server.py`'s `adopted_inactive` branch hands to a fresh agent.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "hypergraph"  # a GPU challenge — exercises multiple .cu kernels
TS = "2026-06-25T00:00:00Z"

# A multi-file bundle: entry mod.rs + a sibling .rs module + TWO named kernels.
FILES = {
    "mod.rs": "use super::*;\npub fn solve_challenge() {}\n",
    "neighborhood.rs": "// helper module\n",
    "kernels.cu": "// primary kernel\n",
    "reduce.cu": "// second kernel, name preserved\n",
}


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def test_multifile_roundtrip_through_adoption_read():
    """A multi-`.cu` seed deposits with its full map and reads back intact."""
    db, server = _fresh_modules()
    await db.init_db()

    files_json = server._files_json(FILES)
    assert files_json is not None, "_files_json dropped a non-empty map"

    async with db.connect() as conn:
        agent_id = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        rowid = await db.deposit_inactive(
            conn, agent_id, CHALLENGE,
            FILES["mod.rs"], None, TS,
            kernel_code=None,  # multiple kernels -> scalar stays None
            algorithm_files=files_json,
        )
        await conn.commit()
    assert rowid != -1, "multi-file seed was wrongly blocked"

    async with db.connect() as conn:
        pool = await db.get_inactive_with_deactivations(conn, CHALLENGE)
    assert len(pool) == 1, f"expected 1 pool entry, got {len(pool)}"

    picked = pool[0]
    # Entry file mirrored into algorithm_code for single-file consumers.
    assert picked["algorithm_code"] == FILES["mod.rs"]
    # Full map preserved (this is what _row_files decodes on adoption).
    got = server._row_files(picked)
    assert got == FILES, f"map not preserved:\n got={got}\n want={FILES}"
    # Both kernels survived with their original names (no rename to kernels.cu).
    cu = sorted(k for k in got if k.endswith(".cu"))
    assert cu == ["kernels.cu", "reduce.cu"], f"kernel names not preserved: {cu}"
    print("PASS test_multifile_roundtrip_through_adoption_read")


async def test_single_file_seed_has_no_files_map():
    """A single-file seed stores algorithm_code only; _files_json -> None."""
    db, server = _fresh_modules()
    await db.init_db()

    single = {"mod.rs": "use super::*;\npub fn solve_challenge() {}\n"}
    # Convention: a one-key map collapses to algorithm_code; _files_json is None.
    files_json = server._files_json(single if len(single) > 1 else None)
    assert files_json is None

    async with db.connect() as conn:
        agent_id = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        await db.deposit_inactive(
            conn, agent_id, CHALLENGE,
            single["mod.rs"], None, TS,
            algorithm_files=files_json,
        )
        await conn.commit()

    async with db.connect() as conn:
        pool = await db.get_inactive_with_deactivations(conn, CHALLENGE)
    assert server._row_files(pool[0]) is None, "single-file seed grew a files map"
    assert pool[0]["algorithm_code"] == single["mod.rs"]
    print("PASS test_single_file_seed_has_no_files_map")


async def test_already_seeded_guard():
    """A second seed from the same source for the same challenge is a no-op,
    but is re-allowed once the prior seed is consumed (removed) from the pool."""
    db, server = _fresh_modules()
    await db.init_db()

    async def _deposit_if_absent():
        async with db.connect() as conn:
            agent_id = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
            existing = await db.count_inactive_from_agent(conn, agent_id, CHALLENGE)
            if existing:
                return False
            await db.deposit_inactive(
                conn, agent_id, CHALLENGE, FILES["mod.rs"], None, TS,
                algorithm_files=server._files_json(FILES),
            )
            await conn.commit()
            return True

    assert await _deposit_if_absent() is True, "first seed should deposit"
    assert await _deposit_if_absent() is False, "duplicate seed should be skipped"

    async with db.connect() as conn:
        pool = await db.get_inactive_with_deactivations(conn, CHALLENGE)
        assert len(pool) == 1, f"guard let a duplicate through: {len(pool)} rows"
        # Consume the seed (as adoption does), then re-seeding is allowed again.
        await db.remove_inactive(conn, pool[0]["id"])
        await conn.commit()

    assert await _deposit_if_absent() is True, "re-seed after consume should deposit"
    print("PASS test_already_seeded_guard")


async def test_clear_inactive_pool_keeps_source():
    """clear_inactive_pool removes everything on the challenge except a kept
    source agent's entries."""
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        keep = await db.ensure_synthetic_agent(conn, "keep-me", TS)
        other = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        await db.deposit_inactive(conn, keep, CHALLENGE, "// keep\n", None, TS)
        await db.deposit_inactive(conn, other, CHALLENGE, "// a\n", None, TS)
        await db.deposit_inactive(conn, other, CHALLENGE, "// b\n", None, TS)
        # a different challenge must be untouched
        await db.deposit_inactive(conn, other, "knapsack", "// k\n", None, TS)
        await conn.commit()

    async with db.connect() as conn:
        deleted = await db.clear_inactive_pool(conn, CHALLENGE, keep_agent_id=keep)
        await conn.commit()
    assert deleted == 2, f"expected 2 deleted, got {deleted}"

    async with db.connect() as conn:
        pool = await db.get_inactive_with_deactivations(conn, CHALLENGE)
        knap = await db.get_inactive_with_deactivations(conn, "knapsack")
    assert len(pool) == 1 and pool[0]["algorithm_code"] == "// keep\n", pool
    assert len(knap) == 1, "other challenge's pool was wrongly cleared"
    print("PASS test_clear_inactive_pool_keeps_source")


async def _main():
    await test_multifile_roundtrip_through_adoption_read()
    await test_single_file_seed_has_no_files_map()
    await test_already_seeded_guard()
    await test_clear_inactive_pool_keeps_source()
    print("\nAll Component 1 + 4 seeding tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
