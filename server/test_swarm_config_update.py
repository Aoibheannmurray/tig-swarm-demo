"""Tests for POST /api/swarm_config partial updates.

Two behaviors the Admin Console's Settings + Instances editors rely on:
  1. A challenges payload carrying ONLY `tracks` must not clobber the other
     sub-config fields. ChallengeSubConfig's old non-None defaults meant a
     tracks-only update silently blanked initial_algorithm_code and flipped
     scoring_direction back to "max".
  2. The four HPO knobs round-trip: POST writes them, GET reflects them.

Standalone: `python server/test_swarm_config_update.py`.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "job_scheduling"


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


async def test_tracks_only_update_preserves_subconfig():
    db, server = _fresh_modules()
    await db.init_db()
    from models import SwarmConfigUpdate, ChallengeSubConfig
    key = await _admin_key(db)

    # Seed a full sub-config (what `setup.py create` pushes).
    await server.update_swarm_config(SwarmConfigUpdate(
        admin_key=key,
        challenges={CHALLENGE: ChallengeSubConfig(
            tracks={"seed": "test", "n=50,s=flow_shop": 2},
            scoring_direction="min",
            initial_algorithm_code="// the initial algorithm\n",
        )},
    ))

    # Console instances editor: tracks-only partial update.
    await server.update_swarm_config(SwarmConfigUpdate(
        admin_key=key,
        challenges={CHALLENGE: ChallengeSubConfig(
            tracks={"seed": "test", "n=50,s=flow_shop": 5},
        )},
    ))

    cfg = await server.get_swarm_config()
    sub = cfg["available_challenges"][CHALLENGE]
    assert sub["tracks"]["n=50,s=flow_shop"] == 5, sub["tracks"]
    assert sub["tracks"]["seed"] == "test", sub["tracks"]
    assert sub["scoring_direction"] == "min", sub
    assert sub["has_initial_algorithm"] is True, sub
    # Canonical track labels ride along so the console can offer 0-instance
    # tracks (absent from the configured `tracks` dict).
    assert "n=50,s=flow_shop" in sub["track_keys"], sub["track_keys"]
    assert len(sub["track_keys"]) >= len(
        [k for k, v in sub["tracks"].items() if isinstance(v, int)]), sub
    algo = await server.get_initial_algorithm(CHALLENGE)
    assert algo["algorithm_code"] == "// the initial algorithm\n", algo
    print("PASS test_tracks_only_update_preserves_subconfig")


async def test_hpo_and_stagnation_knobs_roundtrip():
    db, server = _fresh_modules()
    await db.init_db()
    from models import SwarmConfigUpdate
    key = await _admin_key(db)

    await server.update_swarm_config(SwarmConfigUpdate(
        admin_key=key,
        stagnation_threshold=7,
        stagnation_limit=9,
        hpo_first_tune_improvements=20,
        hpo_min_improvements=6,
        hpo_search_budget=25,
        hpo_num_suggested_configs=8,
    ))
    cfg = await server.get_swarm_config()
    assert cfg["stagnation_threshold"] == 7, cfg
    assert cfg["stagnation_limit"] == 9, cfg
    assert cfg["hpo_first_tune_improvements"] == 20, cfg
    assert cfg["hpo_min_improvements"] == 6, cfg
    assert cfg["hpo_search_budget"] == 25, cfg
    assert cfg["hpo_num_suggested_configs"] == 8, cfg

    # Untouched knobs keep their defaults.
    assert cfg["negative_trajectory_limit"] == 0, cfg
    print("PASS test_hpo_and_stagnation_knobs_roundtrip")


async def test_failed_attempts_archive_roundtrip():
    db, server = _fresh_modules()
    await db.init_db()
    from models import SwarmConfigUpdate
    key = await _admin_key(db)

    # Off by default.
    cfg = await server.get_swarm_config()
    assert cfg["failed_attempts_archive"] == 0, cfg

    await server.update_swarm_config(SwarmConfigUpdate(
        admin_key=key, failed_attempts_archive=1,
    ))
    cfg = await server.get_swarm_config()
    assert cfg["failed_attempts_archive"] == 1, cfg
    print("PASS test_failed_attempts_archive_roundtrip")


if __name__ == "__main__":
    asyncio.run(test_tracks_only_update_preserves_subconfig())
    asyncio.run(test_hpo_and_stagnation_knobs_roundtrip())
    asyncio.run(test_failed_attempts_archive_roundtrip())
    print("ALL PASS")
