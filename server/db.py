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
    status TEXT DEFAULT 'idle'
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    strategy_tag TEXT NOT NULL,
    status TEXT DEFAULT 'failed',
    fingerprint TEXT NOT NULL,
    parent_hypothesis_id TEXT,
    program_id TEXT,
    target_best_experiment_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS agent_bests (
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    algorithm_code TEXT NOT NULL,
    score REAL NOT NULL,
    feasible INTEGER NOT NULL DEFAULT 1,
    num_vehicles INTEGER DEFAULT 0,
    total_distance REAL DEFAULT 0.0,
    solution_data TEXT,
    track_scores TEXT,
    updated_at TEXT NOT NULL,
    trajectory_id TEXT,
    PRIMARY KEY (agent_id, challenge),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    hypothesis_id TEXT,
    algorithm_code TEXT DEFAULT '',
    score REAL NOT NULL,
    feasible INTEGER DEFAULT 1,
    num_vehicles INTEGER DEFAULT 0,
    total_distance REAL DEFAULT 0.0,
    runtime_seconds REAL DEFAULT 0.0,
    notes TEXT DEFAULT '',
    solution_data TEXT,
    track_scores TEXT,
    delta_vs_best_pct REAL,
    delta_vs_own_best_pct REAL,
    beats_own_best INTEGER DEFAULT 0,
    trajectory_id TEXT,
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
    challenge TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    content TEXT NOT NULL,
    msg_type TEXT DEFAULT 'agent',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS best_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    agent_id TEXT,
    challenge TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    score REAL NOT NULL,
    solution_data TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inactive_algorithms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    algorithm_code TEXT NOT NULL,
    score REAL,
    deposited_at TEXT NOT NULL,
    trajectory_id TEXT,
    program_id TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id TEXT PRIMARY KEY,
    challenge TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    current_score REAL,
    num_edits INTEGER DEFAULT 0,
    num_improvements INTEGER DEFAULT 0,
    momentum REAL DEFAULT 0.0,
    num_agents INTEGER DEFAULT 1,
    edits_since_improvement INTEGER DEFAULT 0,
    num_deactivations INTEGER DEFAULT 0,
    deactivated_at TEXT
);

-- Per-(agent, challenge) state. One row per (agent_id, challenge) — created
-- lazily the first time an agent works on a given challenge. When the swarm
-- host switches the active challenge, agents resume from their existing row
-- for the new challenge (or get a fresh row if it's their first time).
CREATE TABLE IF NOT EXISTS agent_challenge_state (
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    current_trajectory_id TEXT,
    current_program_id TEXT,
    runs_since_improvement INTEGER DEFAULT 0,
    improvements INTEGER DEFAULT 0,
    experiments_completed INTEGER DEFAULT 0,
    best_ever_score REAL,
    num_trajectories INTEGER DEFAULT 0,
    tacit_knowledge_count INTEGER DEFAULT 0,
    inspiration_count INTEGER DEFAULT 0,
    last_active_at TEXT,
    PRIMARY KEY (agent_id, challenge),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

-- Per-challenge configuration. The owner can have all five rows populated
-- in parallel; `config.active_challenge` selects which one is currently
-- being worked on by the swarm. Switching the active challenge does NOT
-- touch this table.
CREATE TABLE IF NOT EXISTS challenge_configs (
    challenge TEXT PRIMARY KEY,
    tracks TEXT NOT NULL DEFAULT '{}',
    timeout INTEGER NOT NULL DEFAULT 5,
    scoring_direction TEXT NOT NULL DEFAULT 'max',
    initial_algorithm_code TEXT NOT NULL DEFAULT '',
    strategy_tags TEXT NOT NULL DEFAULT '[]'
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
CREATE INDEX IF NOT EXISTS idx_agent_bests_challenge ON agent_bests(challenge, feasible, score);
CREATE INDEX IF NOT EXISTS idx_experiments_challenge ON experiments(challenge, agent_id);
CREATE INDEX IF NOT EXISTS idx_hyp_challenge_agent ON hypotheses(challenge, agent_id, target_best_experiment_id);
CREATE INDEX IF NOT EXISTS idx_inactive_challenge ON inactive_algorithms(challenge);
CREATE INDEX IF NOT EXISTS idx_trajectories_challenge ON trajectories(challenge);
CREATE INDEX IF NOT EXISTS idx_best_history_challenge ON best_history(challenge, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_challenge_created ON messages(challenge, created_at);
CREATE INDEX IF NOT EXISTS idx_acs_challenge ON agent_challenge_state(challenge);
CREATE INDEX IF NOT EXISTS idx_acs_active ON agent_challenge_state(challenge, last_active_at);
"""

DEFAULT_CONFIG = {
    # Global swarm config in the singleton key/value table. Per-challenge
    # config (tracks, timeout, scoring_direction, initial_algorithm_code)
    # lives in `challenge_configs`, not here.
    #
    # `active_challenge` is the swarm-wide challenge the owner has chosen;
    # contributors auto-follow it via `python setup.py sync`. Only the
    # owner (admin_key holder) can change it via POST /api/swarm_config.
    "active_challenge": "vehicle_routing",
    "swarm_name": "",
    "owner_name": "",
    "hypothesis_recall_threshold": "3",
}


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Tables, then indexes. All DDL is IF NOT EXISTS, so calling
        # init_db() against an existing DB is a no-op — the schema only
        # ever moves forward via deliberate edits to SCHEMA / SCHEMA_INDEXES.
        # No in-place migrations are supported: a schema change implies
        # wiping the DB (or the persistent volume) before redeploy.
        await db.executescript(SCHEMA)
        await db.executescript(SCHEMA_INDEXES)
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
    conn: aiosqlite.Connection, challenge: str, direction: str = "min"
) -> dict | None:
    # Global best is the best-scoring `agent_bests` row — i.e. whichever
    # agent's branch currently holds the leading feasible score for the
    # given challenge. `id` is aliased to experiment_id so callers that
    # expect the old experiments shape keep working.
    order = _direction_order(direction)
    cursor = await conn.execute(
        f"SELECT agent_id, challenge, experiment_id as id, experiment_id, algorithm_code, "
        f"       score, feasible, num_vehicles, total_distance, solution_data, track_scores, updated_at "
        f"FROM agent_bests WHERE feasible = 1 AND challenge = ? "
        f"ORDER BY score {order} LIMIT 1",
        (challenge,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_agent_best(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> dict | None:
    cursor = await conn.execute(
        "SELECT agent_id, challenge, experiment_id as id, experiment_id, algorithm_code, "
        "       score, feasible, num_vehicles, total_distance, solution_data, track_scores, updated_at "
        "FROM agent_bests WHERE agent_id = ? AND challenge = ?",
        (agent_id, challenge),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_agent_best(
    conn: aiosqlite.Connection,
    agent_id: str,
    challenge: str,
    experiment_id: str,
    algorithm_code: str,
    score: float,
    feasible: bool,
    num_vehicles: int | None,
    total_distance: float | None,
    solution_data: str | None,
    updated_at: str,
    trajectory_id: str | None = None,
    track_scores: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO agent_bests
           (agent_id, challenge, experiment_id, algorithm_code, score, feasible,
            num_vehicles, total_distance, solution_data, track_scores, updated_at, trajectory_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id, challenge) DO UPDATE SET
             experiment_id = excluded.experiment_id,
             algorithm_code = excluded.algorithm_code,
             score = excluded.score,
             feasible = excluded.feasible,
             num_vehicles = excluded.num_vehicles,
             total_distance = excluded.total_distance,
             solution_data = excluded.solution_data,
             track_scores = excluded.track_scores,
             updated_at = excluded.updated_at,
             trajectory_id = excluded.trajectory_id""",
        (agent_id, challenge, experiment_id, algorithm_code, score,
         1 if feasible else 0, num_vehicles, total_distance,
         solution_data, track_scores, updated_at, trajectory_id),
    )


async def list_agent_bests(
    conn: aiosqlite.Connection,
    challenge: str,
    exclude_agent_ids: list[str] | None = None,
    direction: str = "min",
    active_only: bool = False,
    inactive_cutoff: str | None = None,
) -> list[dict]:
    # Feasible agent-bests for the given challenge, optionally excluding
    # specific agent ids. When active_only=True, only includes agents whose
    # agent_challenge_state(agent_id, challenge).last_active_at is recent —
    # this is the inspiration filter (don't pull inspiration from agents
    # not currently working on this challenge).
    exclude = exclude_agent_ids or []
    order = _direction_order(direction)
    where = ["ab.feasible = 1", "ab.challenge = ?"]
    params: list = [challenge]
    if exclude:
        placeholders = ",".join("?" for _ in exclude)
        where.append(f"ab.agent_id NOT IN ({placeholders})")
        params.extend(exclude)
    join_clause = ""
    if active_only and inactive_cutoff is not None:
        join_clause = (
            " JOIN agent_challenge_state acs "
            " ON acs.agent_id = ab.agent_id AND acs.challenge = ab.challenge "
        )
        where.append("acs.last_active_at >= ?")
        params.append(inactive_cutoff)
    query = (
        "SELECT ab.agent_id, ab.challenge, ab.experiment_id as id, ab.experiment_id, "
        "       ab.algorithm_code, ab.score, ab.feasible, ab.num_vehicles, "
        "       ab.total_distance, ab.solution_data, ab.updated_at "
        f"FROM agent_bests ab{join_clause} WHERE " + " AND ".join(where) +
        f" ORDER BY ab.score {order}"
    )
    cursor = await conn.execute(query, params)
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
    challenge: str,
    inactive_cutoff: str | None = None,
    direction: str = "min",
) -> list[dict]:
    # Per-challenge leaderboard. Only includes agents that have actually
    # PUBLISHED at least one iteration on this challenge. An agent that
    # only ever fetched /api/state for this challenge gets a row in
    # agent_challenge_state via ensure_agent_challenge_state, but with
    # zero experiments — those would otherwise show up as ghosts.
    order = _direction_order(direction)
    # CORRECTNESS INVARIANT: `active` is sourced from acs.last_active_at,
    # NOT from a.last_heartbeat. An agent currently working on VRP is alive
    # but is NOT "active on SAT" — its row in agent_challenge_state(*, sat)
    # may be missing or stale, and that's exactly what we want.
    cursor = await conn.execute(
        f"""
        SELECT
            a.id   as agent_id,
            a.name as agent_name,
            acs.experiments_completed as runs,
            acs.improvements as improvements,
            acs.runs_since_improvement as runs_since_improvement,
            acs.last_active_at as last_active_at,
            acs.best_ever_score as best_ever_score,
            acs.num_trajectories as num_trajectories,
            acs.tacit_knowledge_count as tacit_knowledge_count,
            acs.inspiration_count as inspiration_count,
            ab.score as current_score
        FROM agent_challenge_state acs
        JOIN agents a ON a.id = acs.agent_id
        LEFT JOIN agent_bests ab
            ON ab.agent_id = a.id AND ab.challenge = ? AND ab.feasible = 1
        WHERE acs.challenge = ?
          AND acs.experiments_completed > 0
        ORDER BY current_score IS NULL, current_score {order}, a.name ASC
        """,
        (challenge, challenge),
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
            "active": row["last_active_at"] >= inactive_cutoff if inactive_cutoff and row["last_active_at"] else False,
        }
        for i, row in enumerate(rows)
    ]


# ── agent_challenge_state helpers ──


async def get_agent_challenge_state(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> dict | None:
    cursor = await conn.execute(
        "SELECT * FROM agent_challenge_state WHERE agent_id = ? AND challenge = ?",
        (agent_id, challenge),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def ensure_agent_challenge_state(
    conn: aiosqlite.Connection, agent_id: str, challenge: str, last_active_at: str
) -> None:
    """Lazily insert a per-(agent, challenge) state row if missing, and
    bump last_active_at on every call. Called at the top of /api/state."""
    await conn.execute(
        """INSERT INTO agent_challenge_state
             (agent_id, challenge, last_active_at)
           VALUES (?, ?, ?)
           ON CONFLICT(agent_id, challenge) DO UPDATE SET
             last_active_at = excluded.last_active_at""",
        (agent_id, challenge, last_active_at),
    )


async def update_agent_challenge_state(
    conn: aiosqlite.Connection,
    agent_id: str,
    challenge: str,
    *,
    set_fields: dict,
) -> None:
    """Apply a SET-style update to the (agent_id, challenge) row.
    Caller passes a dict of column → value pairs; only those keys are
    written. Use this for atomic counter bumps and trajectory swaps."""
    if not set_fields:
        return
    cols = list(set_fields.keys())
    set_sql = ", ".join(f"{c} = ?" for c in cols)
    params = list(set_fields.values()) + [agent_id, challenge]
    await conn.execute(
        f"UPDATE agent_challenge_state SET {set_sql} "
        "WHERE agent_id = ? AND challenge = ?",
        params,
    )


async def increment_agent_challenge_counters(
    conn: aiosqlite.Connection,
    agent_id: str,
    challenge: str,
    *,
    runs: int = 0,
    improvements: int = 0,
    runs_since_improvement_reset: bool = False,
    runs_since_improvement_inc: int = 0,
    num_trajectories_inc: int = 0,
    tacit_knowledge_inc: int = 0,
    inspiration_inc: int = 0,
    best_ever_score: float | None = None,
    direction: str = "max",
) -> None:
    """Bump the counters on agent_challenge_state. Mirrors the legacy
    per-agents counter bumps, but scoped to (agent, challenge)."""
    rsi_clause = (
        "runs_since_improvement = 0"
        if runs_since_improvement_reset
        else f"runs_since_improvement = runs_since_improvement + {int(runs_since_improvement_inc)}"
    )
    best_clause = ""
    if best_ever_score is not None:
        cmp_op = ">" if direction == "max" else "<"
        best_clause = (
            f", best_ever_score = CASE "
            f"  WHEN best_ever_score IS NULL THEN ? "
            f"  WHEN ? {cmp_op} best_ever_score THEN ? "
            f"  ELSE best_ever_score END"
        )
    sql = f"""UPDATE agent_challenge_state SET
                experiments_completed = experiments_completed + ?,
                improvements = improvements + ?,
                {rsi_clause},
                num_trajectories = num_trajectories + ?,
                tacit_knowledge_count = tacit_knowledge_count + ?,
                inspiration_count = inspiration_count + ?
                {best_clause}
              WHERE agent_id = ? AND challenge = ?"""
    params: list = [runs, improvements, num_trajectories_inc,
                    tacit_knowledge_inc, inspiration_inc]
    if best_ever_score is not None:
        params.extend([best_ever_score, best_ever_score, best_ever_score])
    params.extend([agent_id, challenge])
    await conn.execute(sql, params)


# ── challenge_configs helpers ──


async def get_challenge_config(
    conn: aiosqlite.Connection, challenge: str
) -> dict | None:
    cursor = await conn.execute(
        "SELECT challenge, tracks, timeout, scoring_direction, initial_algorithm_code "
        "FROM challenge_configs WHERE challenge = ?",
        (challenge,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_challenge_configs(conn: aiosqlite.Connection) -> list[dict]:
    cursor = await conn.execute(
        "SELECT challenge, tracks, timeout, scoring_direction, initial_algorithm_code "
        "FROM challenge_configs ORDER BY challenge"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def upsert_challenge_config(
    conn: aiosqlite.Connection,
    challenge: str,
    *,
    tracks: str | None = None,
    timeout: int | None = None,
    scoring_direction: str | None = None,
    initial_algorithm_code: str | None = None,
    strategy_tags: str | None = None,
) -> None:
    """Partial upsert — only writes the fields the caller passes. Lets
    `POST /api/swarm_config` accept one challenge's sub-config at a time."""
    # Ensure row exists.
    await conn.execute(
        "INSERT OR IGNORE INTO challenge_configs (challenge) VALUES (?)",
        (challenge,),
    )
    sets = []
    params: list = []
    if tracks is not None:
        sets.append("tracks = ?")
        params.append(tracks)
    if timeout is not None:
        sets.append("timeout = ?")
        params.append(int(timeout))
    if scoring_direction is not None:
        sets.append("scoring_direction = ?")
        params.append(scoring_direction)
    if initial_algorithm_code is not None:
        sets.append("initial_algorithm_code = ?")
        params.append(initial_algorithm_code)
    if strategy_tags is not None:
        sets.append("strategy_tags = ?")
        params.append(strategy_tags)
    if not sets:
        return
    params.append(challenge)
    await conn.execute(
        f"UPDATE challenge_configs SET {', '.join(sets)} WHERE challenge = ?",
        params,
    )


# ── active_challenge helpers ──


async def get_active_challenge(conn: aiosqlite.Connection) -> str:
    """The swarm-wide challenge contributors auto-follow. Owner-set."""
    cursor = await conn.execute(
        "SELECT value FROM config WHERE key = 'active_challenge'"
    )
    row = await cursor.fetchone()
    return row["value"] if row else "vehicle_routing"


async def set_active_challenge(conn: aiosqlite.Connection, challenge: str) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        ("active_challenge", challenge),
    )


async def deposit_inactive(
    conn: aiosqlite.Connection,
    agent_id: str,
    challenge: str,
    algorithm_code: str,
    score: float | None,
    deposited_at: str,
    trajectory_id: str | None = None,
    program_id: str | None = None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO inactive_algorithms "
        "  (agent_id, challenge, algorithm_code, score, deposited_at, trajectory_id, program_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent_id, challenge, algorithm_code, score, deposited_at, trajectory_id, program_id),
    )
    return cursor.lastrowid


async def count_inactive(conn: aiosqlite.Connection, challenge: str) -> int:
    row = await (await conn.execute(
        "SELECT COUNT(*) as c FROM inactive_algorithms WHERE challenge = ?",
        (challenge,),
    )).fetchone()
    return row["c"]


async def pick_random_inactive(
    conn: aiosqlite.Connection, challenge: str
) -> dict | None:
    # CORRECTNESS INVARIANT: must filter by challenge so a stagnating agent's
    # "fresh start" cannot be handed code from a different challenge that
    # won't compile against its types.
    cursor = await conn.execute(
        "SELECT id, agent_id, challenge, algorithm_code, score, trajectory_id, program_id "
        "FROM inactive_algorithms WHERE challenge = ? ORDER BY RANDOM() LIMIT 1",
        (challenge,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def remove_inactive(conn: aiosqlite.Connection, inactive_id: int) -> None:
    await conn.execute(
        "DELETE FROM inactive_algorithms WHERE id = ?", (inactive_id,)
    )


async def mean_trajectory_deactivations(
    conn: aiosqlite.Connection, challenge: str
) -> float:
    row = await (await conn.execute(
        "SELECT AVG(num_deactivations) as avg_d FROM trajectories WHERE challenge = ?",
        (challenge,),
    )).fetchone()
    return row["avg_d"] if row and row["avg_d"] is not None else 0.0


async def get_inactive_with_deactivations(
    conn: aiosqlite.Connection, challenge: str
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT ia.id, ia.agent_id, ia.challenge, ia.algorithm_code, ia.score, "
        "  ia.trajectory_id, ia.program_id, "
        "  COALESCE(t.num_deactivations, 1) as num_deactivations "
        "FROM inactive_algorithms ia "
        "LEFT JOIN trajectories t ON ia.trajectory_id = t.id "
        "WHERE ia.challenge = ?",
        (challenge,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def clear_agent_best(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> None:
    await conn.execute(
        "DELETE FROM agent_bests WHERE agent_id = ? AND challenge = ?",
        (agent_id, challenge),
    )


# ── Trajectory helpers ──


async def create_trajectory(
    conn: aiosqlite.Connection,
    trajectory_id: str,
    challenge: str,
    started_at: str,
    current_score: float | None = None,
    num_agents: int = 1,
) -> None:
    await conn.execute(
        "INSERT INTO trajectories "
        "  (id, challenge, started_at, status, current_score, num_agents) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (trajectory_id, challenge, started_at, current_score, num_agents),
    )


async def deactivate_trajectory(
    conn: aiosqlite.Connection, trajectory_id: str, deactivated_at: str
) -> None:
    await conn.execute(
        "UPDATE trajectories SET status = 'inactive', deactivated_at = ?, "
        "num_deactivations = num_deactivations + 1 WHERE id = ?",
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


async def list_trajectories(
    conn: aiosqlite.Connection, challenge: str | None = None
) -> list[dict]:
    if challenge is None:
        cursor = await conn.execute(
            "SELECT * FROM trajectories ORDER BY started_at DESC"
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM trajectories WHERE challenge = ? ORDER BY started_at DESC",
            (challenge,),
        )
    return [dict(row) for row in await cursor.fetchall()]


async def get_trajectory_score_history(
    conn: aiosqlite.Connection,
    trajectory_id: str,
    challenge: str | None = None,
    direction: str = "max",
) -> list[dict]:
    if challenge is None:
        cursor = await conn.execute(
            "SELECT score, created_at FROM experiments "
            "WHERE trajectory_id = ? AND feasible = 1 "
            "ORDER BY created_at",
            (trajectory_id,),
        )
    else:
        cursor = await conn.execute(
            "SELECT score, created_at FROM experiments "
            "WHERE trajectory_id = ? AND challenge = ? AND feasible = 1 "
            "ORDER BY created_at",
            (trajectory_id, challenge),
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
