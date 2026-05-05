import aiosqlite
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import os
# Use /data for Railway persistent volume, fallback to local for dev
_data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DB_PATH = _data_dir / "swarm.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    registered_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    status TEXT DEFAULT 'idle',
    experiments_completed INTEGER DEFAULT 0,
    best_score REAL,
    runs_since_improvement INTEGER DEFAULT 0,
    improvements INTEGER DEFAULT 0,
    best_ever_score REAL,
    num_trajectories INTEGER DEFAULT 0,
    tacit_knowledge_count INTEGER DEFAULT 0,
    inspiration_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    strategy_tag TEXT NOT NULL,
    status TEXT DEFAULT 'failed',
    fingerprint TEXT NOT NULL,
    parent_hypothesis_id TEXT,
    created_at TEXT NOT NULL,
    target_best_experiment_id TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS agent_bests (
    agent_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    algorithm_code TEXT NOT NULL,
    score REAL NOT NULL,
    feasible INTEGER NOT NULL DEFAULT 1,
    num_vehicles INTEGER DEFAULT 0,
    total_distance REAL DEFAULT 0.0,
    route_data TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    hypothesis_id TEXT,
    algorithm_code TEXT DEFAULT '',
    score REAL NOT NULL,
    feasible INTEGER DEFAULT 1,
    num_vehicles INTEGER DEFAULT 0,
    total_distance REAL DEFAULT 0.0,
    runtime_seconds REAL DEFAULT 0.0,
    notes TEXT DEFAULT '',
    route_data TEXT,
    delta_vs_best_pct REAL,
    delta_vs_own_best_pct REAL,
    beats_own_best INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    agent_name TEXT NOT NULL,
    content TEXT NOT NULL,
    msg_type TEXT DEFAULT 'agent',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS best_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    agent_id TEXT,
    agent_name TEXT NOT NULL,
    score REAL NOT NULL,
    route_data TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inactive_algorithms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    algorithm_code TEXT NOT NULL,
    score REAL,
    deposited_at TEXT NOT NULL,
    trajectory_id TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    current_score REAL,
    num_edits INTEGER DEFAULT 0,
    num_improvements INTEGER DEFAULT 0,
    momentum REAL DEFAULT 0.0,
    num_agents INTEGER DEFAULT 1,
    deactivated_at TEXT,
    edits_since_improvement INTEGER DEFAULT 0
);
"""

# Indexes are split out from the main schema so they can be applied after
# ALTER TABLE migrations in init_db, which keeps both fresh and upgraded
# databases working.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_exp_feasible_score ON experiments(feasible, score);
CREATE INDEX IF NOT EXISTS idx_exp_agent ON experiments(agent_id);
CREATE INDEX IF NOT EXISTS idx_hyp_status ON hypotheses(status);
CREATE INDEX IF NOT EXISTS idx_hyp_fingerprint ON hypotheses(fingerprint);
CREATE INDEX IF NOT EXISTS idx_agent_bests_score ON agent_bests(feasible, score);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_hyp_agent_target ON hypotheses(agent_id, target_best_experiment_id);
CREATE INDEX IF NOT EXISTS idx_hyp_program_id ON hypotheses(program_id);
"""

DEFAULT_CONFIG = {
    # Swarm-wide configuration written by the setup wizard via
    # POST /api/swarm_config and read by every clone via GET /api/swarm_config.
    # The defaults below are pre-wizard placeholders — `python setup.py create`
    # overwrites every key. `tracks` is the dict shape that mirrors
    # datasets/<challenge>/test.json (instance counts per track key).
    # `initial_algorithm_code` is the source of `initial_algorithm.rs` from
    # the host's clone, broadcast as the starting code on every fresh
    # trajectory; empty until the wizard's first POST.
    "challenge": "vehicle_routing",
    "tracks": "{}",
    "timeout": "30",
    "scoring_direction": "max",
    "swarm_name": "",
    "owner_name": "",
    "initial_algorithm_code": "",
    "hypothesis_recall_threshold": "3",
}


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # 1) Tables first. All table DDL is IF NOT EXISTS so fresh and
        #    upgraded databases both work.
        await db.executescript(SCHEMA)
        # 2) Column migrations. ADD COLUMN fails if the column exists;
        #    that's expected on every subsequent run.
        try:
            await db.execute("ALTER TABLE experiments RENAME COLUMN algorithm_diff TO algorithm_code")
            await db.commit()
        except Exception:
            pass
        for stmt in (
            "ALTER TABLE agents ADD COLUMN runs_since_improvement INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN improvements INTEGER DEFAULT 0",
            "ALTER TABLE hypotheses ADD COLUMN target_best_experiment_id TEXT",
            "ALTER TABLE best_history ADD COLUMN agent_id TEXT",
            "ALTER TABLE experiments ADD COLUMN delta_vs_best_pct REAL",
            "ALTER TABLE experiments ADD COLUMN delta_vs_own_best_pct REAL",
            "ALTER TABLE experiments ADD COLUMN beats_own_best INTEGER DEFAULT 0",
            "ALTER TABLE experiments ADD COLUMN trajectory_id TEXT",
            "ALTER TABLE agent_bests ADD COLUMN trajectory_id TEXT",
            "ALTER TABLE agents ADD COLUMN current_trajectory_id TEXT",
            "ALTER TABLE inactive_algorithms ADD COLUMN trajectory_id TEXT",
            "ALTER TABLE trajectories ADD COLUMN edits_since_improvement INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN best_ever_score REAL",
            "ALTER TABLE agents ADD COLUMN num_trajectories INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN tacit_knowledge_count INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN inspiration_count INTEGER DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN current_program_id TEXT",
            "ALTER TABLE hypotheses ADD COLUMN program_id TEXT",
            "ALTER TABLE inactive_algorithms ADD COLUMN program_id TEXT",
        ):
            try:
                await db.execute(stmt)
                await db.commit()
            except Exception:
                pass
        # The current model has no "active" hypotheses: every attempt is
        # recorded as succeeded/failed once evaluated. Legacy statuses are
        # normalized to failed so old rows don't appear in a third state.
        await db.execute(
            "UPDATE hypotheses SET status = 'failed' "
            "WHERE status IN ('proposed', 'claimed', 'testing')"
        )
        await db.commit()
        # 3) Indexes last, *after* the migrations above — some of them
        #    reference columns that only exist post-migration.
        await db.executescript(SCHEMA_INDEXES)
        # 4) Backfill agent_bests from the existing experiments table on
        #    first upgrade. Without this, an existing deployment would see
        #    an empty agent_bests, collapse to cold start, and serve every
        #    agent the challenge seed until someone republishes. ON CONFLICT
        #    DO NOTHING makes this a no-op on subsequent boots.
        cursor = await db.execute(
            "SELECT value FROM config WHERE key = 'scoring_direction'"
        )
        row = await cursor.fetchone()
        backfill_order = "DESC" if (row and row[0] == "max") else "ASC"
        await db.execute(
            f"""INSERT INTO agent_bests
               (agent_id, experiment_id, algorithm_code, score, feasible,
                num_vehicles, total_distance, route_data, updated_at)
               SELECT agent_id, id, algorithm_code, score, 1,
                      num_vehicles, total_distance, route_data, created_at
               FROM (
                   SELECT e.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY e.agent_id
                              ORDER BY e.score {backfill_order}, e.created_at ASC
                          ) AS rn
                   FROM experiments e
                   WHERE e.feasible = 1
               )
               WHERE rn = 1
               ON CONFLICT(agent_id) DO NOTHING"""
        )
        await db.commit()
        for key, value in DEFAULT_CONFIG.items():
            await db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
        # Admin key resolution, in priority order:
        #   1. ADMIN_KEY env var — wins every boot, ideal for hosted deploys
        #      (Railway/Fly/etc) so the operator owns the key out-of-band.
        #   2. Existing value in the config table — preserves the key across
        #      restarts when no env var is set.
        #   3. A freshly-generated random key — only used on the very first
        #      boot of a fresh DB with no env override.
        env_key = os.environ.get("ADMIN_KEY")
        if env_key:
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                ("admin_key", env_key),
            )
        else:
            await db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                ("admin_key", secrets.token_urlsafe(16)),
            )
        await db.commit()


@asynccontextmanager
async def connect():
    """Context manager for DB connections — ensures cleanup on error."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def get_config(conn: aiosqlite.Connection) -> dict:
    cursor = await conn.execute("SELECT key, value FROM config")
    rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows}


def _direction_order(direction: str) -> str:
    # Min-direction challenges (VRP, JSP) want lower scores at the top of
    # the leaderboard; max-direction challenges (knapsack, SAT, energy)
    # want higher. Validated to a small set so callers can't slip raw SQL
    # through.
    return "DESC" if direction == "max" else "ASC"


def is_better(direction: str, candidate: float, prior: float) -> bool:
    return candidate > prior if direction == "max" else candidate < prior


async def get_global_best(
    conn: aiosqlite.Connection, direction: str = "min"
) -> dict | None:
    # Global best is the best-scoring `agent_bests` row — i.e. whichever
    # agent's branch currently holds the leading feasible score. `id` is
    # aliased to experiment_id so callers that expect the old experiments
    # shape (best["id"] meaning the experiment row) keep working.
    order = _direction_order(direction)
    cursor = await conn.execute(
        f"SELECT agent_id, experiment_id as id, experiment_id, algorithm_code, "
        f"       score, feasible, num_vehicles, total_distance, route_data, updated_at "
        f"FROM agent_bests WHERE feasible = 1 "
        f"ORDER BY score {order} LIMIT 1"
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_agent_best(
    conn: aiosqlite.Connection, agent_id: str
) -> dict | None:
    cursor = await conn.execute(
        "SELECT agent_id, experiment_id as id, experiment_id, algorithm_code, "
        "       score, feasible, num_vehicles, total_distance, route_data, updated_at "
        "FROM agent_bests WHERE agent_id = ?",
        (agent_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_agent_best(
    conn: aiosqlite.Connection,
    agent_id: str,
    experiment_id: str,
    algorithm_code: str,
    score: float,
    feasible: bool,
    num_vehicles: int,
    total_distance: float,
    route_data: str | None,
    updated_at: str,
    trajectory_id: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO agent_bests
           (agent_id, experiment_id, algorithm_code, score, feasible,
            num_vehicles, total_distance, route_data, updated_at, trajectory_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id) DO UPDATE SET
             experiment_id = excluded.experiment_id,
             algorithm_code = excluded.algorithm_code,
             score = excluded.score,
             feasible = excluded.feasible,
             num_vehicles = excluded.num_vehicles,
             total_distance = excluded.total_distance,
             route_data = excluded.route_data,
             updated_at = excluded.updated_at,
             trajectory_id = excluded.trajectory_id""",
        (agent_id, experiment_id, algorithm_code, score,
         1 if feasible else 0, num_vehicles, total_distance,
         route_data, updated_at, trajectory_id),
    )


async def list_agent_bests(
    conn: aiosqlite.Connection,
    exclude_agent_ids: list[str] | None = None,
    direction: str = "min",
) -> list[dict]:
    # All feasible agent-bests, optionally excluding specific agent ids.
    # Returned shape matches `get_global_best` so callers can treat the
    # rows interchangeably.
    exclude = exclude_agent_ids or []
    order = _direction_order(direction)
    if exclude:
        placeholders = ",".join("?" for _ in exclude)
        query = (
            "SELECT agent_id, experiment_id as id, experiment_id, algorithm_code, "
            "       score, feasible, num_vehicles, total_distance, route_data, updated_at "
            f"FROM agent_bests WHERE feasible = 1 AND agent_id NOT IN ({placeholders}) "
            f"ORDER BY score {order}"
        )
        cursor = await conn.execute(query, exclude)
    else:
        cursor = await conn.execute(
            f"SELECT agent_id, experiment_id as id, experiment_id, algorithm_code, "
            f"       score, feasible, num_vehicles, total_distance, route_data, updated_at "
            f"FROM agent_bests WHERE feasible = 1 ORDER BY score {order}"
        )
    return [dict(row) for row in await cursor.fetchall()]


async def get_agent_count(
    conn: aiosqlite.Connection,
    active_only: bool = False,
    inactive_cutoff: str | None = None,
) -> int:
    if active_only:
        if inactive_cutoff is None:
            raise ValueError("inactive_cutoff is required when active_only=True")
        cursor = await conn.execute(
            "SELECT COUNT(*) as c FROM agents WHERE last_heartbeat >= ?",
            (inactive_cutoff,),
        )
    else:
        cursor = await conn.execute("SELECT COUNT(*) as c FROM agents")
    return (await cursor.fetchone())["c"]


async def get_all_agent_names(conn: aiosqlite.Connection) -> set[str]:
    cursor = await conn.execute("SELECT name FROM agents")
    return {row["name"] for row in await cursor.fetchall()}


async def compute_leaderboard(
    conn: aiosqlite.Connection,
    inactive_cutoff: str | None = None,
    direction: str = "min",
) -> list[dict]:
    # All counters are stored directly on the agents table and updated
    # atomically by POST /api/iterations.  best_score comes from agent_bests.
    # `direction` flips the ORDER BY so max-direction challenges (knapsack,
    # SAT, energy) put higher scores at the top.
    order = _direction_order(direction)
    cursor = await conn.execute(
        f"""
        SELECT
            a.id   as agent_id,
            a.name as agent_name,
            a.experiments_completed as runs,
            a.improvements as improvements,
            a.runs_since_improvement as runs_since_improvement,
            a.last_heartbeat as last_heartbeat,
            a.best_ever_score as best_ever_score,
            a.num_trajectories as num_trajectories,
            a.tacit_knowledge_count as tacit_knowledge_count,
            a.inspiration_count as inspiration_count,
            ab.score as current_score
        FROM agents a
        LEFT JOIN agent_bests ab ON ab.agent_id = a.id AND ab.feasible = 1
        ORDER BY current_score IS NULL, current_score {order}, a.name ASC
        """
    )
    rows = await cursor.fetchall()
    return [
        {
            "rank": i + 1,
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "runs": row["runs"],
            "improvements": row["improvements"],
            "runs_since_improvement": row["runs_since_improvement"],
            "current_score": row["current_score"],
            "best_ever_score": row["best_ever_score"],
            "num_trajectories": row["num_trajectories"] or 0,
            "tacit_knowledge_count": row["tacit_knowledge_count"] or 0,
            "inspiration_count": row["inspiration_count"] or 0,
            "active": row["last_heartbeat"] >= inactive_cutoff if inactive_cutoff and row["last_heartbeat"] else False,
        }
        for i, row in enumerate(rows)
    ]


async def deposit_inactive(
    conn: aiosqlite.Connection,
    agent_id: str,
    algorithm_code: str,
    score: float | None,
    deposited_at: str,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO inactive_algorithms (agent_id, algorithm_code, score, deposited_at) "
        "VALUES (?, ?, ?, ?)",
        (agent_id, algorithm_code, score, deposited_at),
    )
    return cursor.lastrowid


async def count_inactive(conn: aiosqlite.Connection) -> int:
    row = await (await conn.execute(
        "SELECT COUNT(*) as c FROM inactive_algorithms"
    )).fetchone()
    return row["c"]


async def pick_random_inactive(conn: aiosqlite.Connection) -> dict | None:
    cursor = await conn.execute(
        "SELECT id, agent_id, algorithm_code, score, trajectory_id, program_id "
        "FROM inactive_algorithms ORDER BY RANDOM() LIMIT 1"
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def remove_inactive(conn: aiosqlite.Connection, inactive_id: int) -> None:
    await conn.execute(
        "DELETE FROM inactive_algorithms WHERE id = ?", (inactive_id,)
    )


async def clear_agent_best(conn: aiosqlite.Connection, agent_id: str) -> None:
    await conn.execute(
        "DELETE FROM agent_bests WHERE agent_id = ?", (agent_id,)
    )


# ── Trajectory helpers ──


async def create_trajectory(
    conn: aiosqlite.Connection,
    trajectory_id: str,
    started_at: str,
    current_score: float | None = None,
    num_agents: int = 1,
) -> None:
    await conn.execute(
        "INSERT INTO trajectories (id, started_at, status, current_score, num_agents) "
        "VALUES (?, ?, 'active', ?, ?)",
        (trajectory_id, started_at, current_score, num_agents),
    )


async def deactivate_trajectory(
    conn: aiosqlite.Connection, trajectory_id: str, deactivated_at: str
) -> None:
    await conn.execute(
        "UPDATE trajectories SET status = 'inactive', deactivated_at = ? WHERE id = ?",
        (deactivated_at, trajectory_id),
    )


async def reactivate_trajectory(
    conn: aiosqlite.Connection, trajectory_id: str
) -> None:
    await conn.execute(
        "UPDATE trajectories SET status = 'active', deactivated_at = NULL WHERE id = ?",
        (trajectory_id,),
    )


async def update_trajectory_after_edit(
    conn: aiosqlite.Connection,
    trajectory_id: str,
    improved: bool,
    new_score: float | None = None,
) -> None:
    if improved and new_score is not None:
        await conn.execute(
            "UPDATE trajectories SET "
            "  num_edits = num_edits + 1, "
            "  num_improvements = num_improvements + 1, "
            "  momentum = momentum * 0.75 + 1, "
            "  current_score = ?, "
            "  edits_since_improvement = 0 "
            "WHERE id = ?",
            (new_score, trajectory_id),
        )
    else:
        await conn.execute(
            "UPDATE trajectories SET "
            "  num_edits = num_edits + 1, "
            "  momentum = momentum * 0.75, "
            "  edits_since_improvement = edits_since_improvement + 1 "
            "WHERE id = ?",
            (trajectory_id,),
        )


async def increment_trajectory_agents(
    conn: aiosqlite.Connection, trajectory_id: str
) -> None:
    await conn.execute(
        "UPDATE trajectories SET num_agents = num_agents + 1 WHERE id = ?",
        (trajectory_id,),
    )


async def list_trajectories(conn: aiosqlite.Connection) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM trajectories ORDER BY started_at DESC"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_trajectory_score_history(
    conn: aiosqlite.Connection,
    trajectory_id: str,
    direction: str = "max",
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT score, created_at FROM experiments "
        "WHERE trajectory_id = ? AND feasible = 1 "
        "ORDER BY created_at",
        (trajectory_id,),
    )
    rows = await cursor.fetchall()
    steps: list[dict] = []
    best: float | None = None
    for row in rows:
        score = row["score"]
        if best is None or is_better(direction, score, best):
            best = score
            steps.append({"score": score, "created_at": row["created_at"]})
    return steps
