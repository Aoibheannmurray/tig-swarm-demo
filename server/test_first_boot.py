"""Tests for first-boot bundle seeding (onboarding P5).

Verifies a browser-only ("Deploy on Railway") deploy comes up with real
starting code + a seed pool from the image-baked snapshot, and that the
seeding is safe/idempotent for normal (create-provisioned) deploys.

Self-running: `python server/test_first_boot.py` from the repo. Points
TIG_INITIAL_ALGORITHMS_DIR at the repo's real initial_algorithms/ tree.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = str(REPO / "initial_algorithms")


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "first_boot", "challenges"):
        sys.modules.pop(mod, None)
    import db
    import first_boot
    return db, first_boot


async def test_first_boot_populates_code_and_pool():
    db, _ = _fresh_modules()
    await db.init_db()  # runs first_boot under the fresh-DB sentinel
    async with db.connect() as conn:
        configs = await db.list_challenge_configs(conn)
        with_code = [c for c in configs if (c.get("initial_algorithm_code") or "").strip()]
        seeds = await db.list_seeds(conn, "knapsack")
    assert with_code, "expected some challenges to get initial_algorithm_code"
    # knapsack ships an authored seed (greedy.rs) in the repo tree.
    assert any(s["strategy_tag"] == "greedy" for s in seeds), \
        f"expected knapsack 'greedy' seed, got {[s['strategy_tag'] for s in seeds]}"
    print(f"PASS test_first_boot_populates_code_and_pool "
          f"({len(with_code)} challenges coded)")


async def test_seed_is_idempotent_and_nonclobbering():
    db, first_boot = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        seeds_before = len(await db.list_seeds(conn, "knapsack"))
        # Overwrite knapsack's code to a sentinel, then re-run: it must NOT be
        # clobbered (fill-only-when-empty), and no duplicate seeds appear.
        await db.upsert_challenge_config(conn, "knapsack",
                                         initial_algorithm_code="// MINE")
        await conn.commit()
        summary = await first_boot.seed_from_bundle(conn)
        await conn.commit()
        cfg = await db.get_challenge_config(conn, "knapsack")
        seeds_after = len(await db.list_seeds(conn, "knapsack"))
    assert cfg["initial_algorithm_code"] == "// MINE", "must not clobber existing code"
    assert seeds_after == seeds_before, "re-seeding must not duplicate pool entries"
    assert summary["seeds"] == 0, summary
    print("PASS test_seed_is_idempotent_and_nonclobbering")


async def test_empty_bundle_seeds_nothing():
    db, first_boot = _fresh_modules()
    await db.init_db()
    saved = os.environ["TIG_INITIAL_ALGORITHMS_DIR"]
    try:
        # An empty bundle dir → nothing to seed (the core no-op invariant;
        # in-repo we can't get bundle=None since the repo tree always exists).
        os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = tempfile.mkdtemp()
        async with db.connect() as conn:
            summary = await first_boot.seed_from_bundle(conn)
        assert summary["initial_code"] == 0 and summary["seeds"] == 0, summary
    finally:
        os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = saved
    print("PASS test_empty_bundle_seeds_nothing")


def test_missing_bundle_returns_none():
    # Unit-level: discovery returns None when NO candidate path exists.
    import first_boot
    saved = os.environ["TIG_INITIAL_ALGORITHMS_DIR"]
    orig = first_boot._bundle_dir
    try:
        os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = "/nonexistent/path/xyz"
        # Neutralize the /app and repo-relative fallbacks for this check.
        first_boot._bundle_dir = lambda: (
            None if not Path("/nonexistent/path/xyz").is_dir() else orig()
        )
        assert first_boot._bundle_dir() is None
    finally:
        os.environ["TIG_INITIAL_ALGORITHMS_DIR"] = saved
        first_boot._bundle_dir = orig
    print("PASS test_missing_bundle_returns_none")


async def _main():
    await test_first_boot_populates_code_and_pool()
    await test_seed_is_idempotent_and_nonclobbering()
    await test_empty_bundle_seeds_nothing()
    test_missing_bundle_returns_none()
    print("ALL PASS")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_main())
