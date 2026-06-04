"""Tests for GET /api/inspiration_matrix.

Runs standalone (`python test_inspiration_matrix.py` from the server dir) and is
also pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.

Regression under test: the matrix used to reconstruct the source trajectory via
an INNER JOIN onto the source agent's *current* trajectory_bests row. That row
is wiped when the source agent stagnates, so the join silently dropped every
event and the matrix went empty. We now read the trajectory captured at
hint-out time (experiments.inspiration_source_trajectory_id), with a LEFT JOIN
fallback for legacy rows.
"""

import asyncio
import os
import tempfile
import uuid

CHALLENGE = "vector_search"
TS = "2026-06-03T00:00:00Z"


def _fresh_modules():
    """Re-import db + server against a brand-new temp DB. Returns (db, server)."""
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    # Drop any cached imports so DB_PATH is recomputed from the new DATA_DIR.
    import sys
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _seed_agents(conn, *agents):
    for aid, name in agents:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            (aid, name, TS, TS),
        )


async def _seed_experiment(conn, *, agent_id, recv_traj, source_id,
                           source_traj, received_hint="inspiration"):
    await conn.execute(
        """INSERT INTO experiments
           (id, agent_id, challenge, score, trajectory_id, received_hint,
            inspiration_source_id, inspiration_source_trajectory_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), agent_id, CHALLENGE, 1.0, recv_traj, received_hint,
         source_id, source_traj, TS),
    )


async def _seed_trajectory_best(conn, *, agent_id, traj_id):
    await conn.execute(
        """INSERT INTO trajectory_bests
           (agent_id, challenge, experiment_id, algorithm_code, score,
            feasible, updated_at, trajectory_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, CHALLENGE, str(uuid.uuid4()), "code", 1.0, 1, TS, traj_id),
    )


def _cell(result, recv_traj, src_traj):
    """Count at matrix[recv][src], located by trajectory-id prefix in labels."""
    ids = [a["agent_id"] for a in result["agents"]]
    if recv_traj not in ids or src_traj not in ids:
        return None
    return result["matrix"][ids.index(recv_traj)][ids.index(src_traj)]


async def test_event_survives_wiped_source_best():
    """The previously-broken case: source agent's trajectory_bests is gone, but
    the event was recorded with its source trajectory and must still show."""
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        await _seed_agents(conn, ("recv", "Receiver"), ("src", "Source"))
        await _seed_experiment(conn, agent_id="recv", recv_traj="trajRECV",
                               source_id="src", source_traj="trajSRC")
        # Deliberately NO trajectory_bests row for "src" — it stagnated/reset.
        await conn.commit()

    result = await server.get_inspiration_matrix(CHALLENGE)
    assert _cell(result, "trajRECV", "trajSRC") == 1, result
    print("PASS test_event_survives_wiped_source_best")


async def test_legacy_row_uses_trajectory_bests_fallback():
    """Pre-migration row (NULL source trajectory) falls back to the source's
    current trajectory_bests trajectory_id via the LEFT JOIN."""
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        await _seed_agents(conn, ("recv", "Receiver"), ("src", "Source"))
        await _seed_experiment(conn, agent_id="recv", recv_traj="trajRECV",
                               source_id="src", source_traj=None)
        await _seed_trajectory_best(conn, agent_id="src", traj_id="trajLEGACY")
        await conn.commit()

    result = await server.get_inspiration_matrix(CHALLENGE)
    assert _cell(result, "trajRECV", "trajLEGACY") == 1, result
    print("PASS test_legacy_row_uses_trajectory_bests_fallback")


async def test_non_inspiration_hint_excluded():
    """tacit_knowledge events never appear in the inspiration matrix."""
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        await _seed_agents(conn, ("recv", "Receiver"), ("src", "Source"))
        await _seed_experiment(conn, agent_id="recv", recv_traj="trajRECV",
                               source_id="src", source_traj="trajSRC",
                               received_hint="tacit_knowledge")
        await conn.commit()

    result = await server.get_inspiration_matrix(CHALLENGE)
    assert result == {"agents": [], "matrix": []}, result
    print("PASS test_non_inspiration_hint_excluded")


async def test_empty_returns_empty_shape():
    db, server = _fresh_modules()
    await db.init_db()
    result = await server.get_inspiration_matrix(CHALLENGE)
    assert result == {"agents": [], "matrix": []}, result
    print("PASS test_empty_returns_empty_shape")


async def _main():
    await test_empty_returns_empty_shape()
    await test_non_inspiration_hint_excluded()
    await test_legacy_row_uses_trajectory_bests_fallback()
    await test_event_survives_wiped_source_best()
    print("\nAll inspiration_matrix tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
