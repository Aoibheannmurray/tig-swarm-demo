import aiosqlite
import base64
import gzip
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import os

from challenges import DEFAULT_CHALLENGE
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
    llm_type TEXT,
    -- Per-agent session token, generated at register. Required as
    -- X-Agent-Token on every non-register participant-write call.
    token TEXT,
    -- Model tier auto-classified at register (see server/tiers.py). Drives
    -- seeding only: 'standard' models get a working seed on a fresh
    -- trajectory; 'frontier' models keep the stub. Defaults to 'standard'.
    tier TEXT DEFAULT 'standard'
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
    role TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

CREATE TABLE IF NOT EXISTS trajectory_bests (
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    -- The experiment that produced this best. References experiments.id and
    -- propagates as-is through the inactive pool on adoption, so on a SHARED
    -- trajectory it may belong to a different agent than agent_id (whoever
    -- last actually improved the lineage). NULL when provenance is genuinely
    -- unknown: a floor inherited from a pre-experiment_id legacy inactive row,
    -- or an admin-seeded inactive entry that was never run. Never a fabricated
    -- id — an id here always resolves to a real experiments row.
    experiment_id TEXT,
    algorithm_code TEXT NOT NULL,
    kernel_code TEXT,
    score REAL NOT NULL,
    feasible INTEGER NOT NULL DEFAULT 1,
    -- Opaque per-challenge roll-up JSON. The server stores it verbatim and
    -- never inspects its keys; the dashboard pulls challenge-specific
    -- fields out of it (e.g. VRP reads num_vehicles / total_distance).
    challenge_metrics TEXT,
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
    kernel_code TEXT,
    score REAL NOT NULL,
    feasible INTEGER DEFAULT 1,
    -- See trajectory_bests.challenge_metrics — same opaque per-challenge dict.
    challenge_metrics TEXT,
    runtime_seconds REAL DEFAULT 0.0,
    notes TEXT DEFAULT '',
    solution_data TEXT,
    track_scores TEXT,
    delta_vs_best_pct REAL,
    delta_vs_trajectory_best_pct REAL,
    beats_trajectory_best INTEGER DEFAULT 0,
    trajectory_id TEXT,
    -- "tacit_knowledge" or "inspiration" when the agent fetched /api/state
    -- with that hint right before publishing this iteration; NULL otherwise.
    -- Lets the dashboard mark hint events on per-agent progress plots.
    received_hint TEXT,
    inspiration_source_id TEXT,
    -- Source agent's trajectory_id at the moment the inspiration hint was
    -- issued (copied from agent_challenge_state.pending_inspiration_source_
    -- trajectory). The inspiration matrix reads this directly so it no longer
    -- depends on the source's current trajectory_bests row surviving.
    inspiration_source_trajectory_id TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost REAL DEFAULT 0.0,
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
    kernel_code TEXT,
    score REAL,
    deposited_at TEXT NOT NULL,
    trajectory_id TEXT,
    program_id TEXT,
    -- The experiment that earned `score`, carried from the depositing agent's
    -- trajectory_bests row so an adopting agent inherits real provenance
    -- instead of a fabricated id. NULL for entries with no underlying run
    -- (admin-seeded) or deposited before this column existed.
    experiment_id TEXT,
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
    failed_attempts_count INTEGER DEFAULT 0,
    -- "tacit_knowledge" / "inspiration" / "failed_attempts" / NULL — the most
    -- recent hint the server gave this agent on this challenge. Set when
    -- /api/state issues the hint, cleared when the agent publishes the next
    -- iteration (whose experiments.received_hint absorbs the value).
    pending_hint TEXT,
    pending_inspiration_source TEXT,
    -- The source agent's trajectory_id captured at hint-out time. Recorded
    -- here (and copied onto experiments) so the inspiration matrix reads the
    -- exact source trajectory instead of reconstructing it from the source's
    -- *current* trajectory_bests row — which is wiped on stagnation reset.
    pending_inspiration_source_trajectory TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_estimated_cost REAL DEFAULT 0.0,
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
    timeout INTEGER NOT NULL DEFAULT 30,
    scoring_direction TEXT NOT NULL DEFAULT 'max',
    initial_algorithm_code TEXT NOT NULL DEFAULT '',
    initial_kernel_code TEXT NOT NULL DEFAULT '',
    strategy_tags TEXT NOT NULL DEFAULT '[]'
);

-- Pool of working starter algorithms handed to standard-tier / exploiter
-- agents on a fresh trajectory (instead of the bare stub). Two sources:
-- 'authored' (host-supplied at swarm create) and 'harvested' (a frontier
-- agent's first feasible result for a strategy_tag). The UNIQUE index below
-- is the entire size-control story: at most one seed per
-- (challenge, strategy_tag, source), so first-feasible-per-tag wins and the
-- pool can't grow unboundedly.
CREATE TABLE IF NOT EXISTS seed_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge TEXT NOT NULL,
    strategy_tag TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'authored',
    score REAL,
    feasible INTEGER NOT NULL DEFAULT 1,
    algorithm_code TEXT NOT NULL,
    kernel_code TEXT,
    origin_agent_id TEXT,
    created_at TEXT NOT NULL
);

-- Server-stored per-contributor fleet config + tacit knowledge (P1 of
-- docs/server-first-onboarding-plan.md). `config_json` is the sanitized
-- fleet plan the hosted contributor console edits — the same agents-array
-- shape as fleet.config.json, secrets hard-rejected at the API layer.
-- `tacit_text` is the contributor's hosted tacit-knowledge notes. The
-- local runner fetches both in --join mode (P2).
-- The TIG mainnet algorithm's score ON THIS SWARM'S OWN INSTANCES, so the
-- dashboard can show members the bar they are trying to clear. One row per
-- challenge: the top-adoption mainnet algorithm, benchmarked unchanged.
--
-- Deliberately NOT measured during `setup.py create` — nobody should wait on a
-- benchmark to finish standing up a swarm. A row starts life 'pending' (a
-- single cheap INSERT) and is filled in afterwards by whichever comes first:
-- the host measuring it on demand, or an agent organically benchmarking the
-- mainnet seed, which the server recognises by `code_fingerprint`.
--
-- `config_fingerprint` is what makes the comparison honest. A score is only
-- meaningful against the exact instance set that produced it, so we record a
-- hash of the challenge's tracks + timeout at benchmark time; when the host
-- later edits either, the dashboard marks the baseline stale instead of
-- silently comparing against a different problem.
CREATE TABLE IF NOT EXISTS mainnet_baselines (
    challenge TEXT PRIMARY KEY,
    algo_name TEXT NOT NULL,
    adoption_pct REAL,
    -- NULL until measured; `status` says whether that's expected.
    score REAL,
    feasible INTEGER NOT NULL DEFAULT 1,
    -- 'pending'   — known algorithm, not benchmarked here yet
    -- 'ready'     — score is real and current
    -- 'unavailable' — no compatible mainnet algorithm for this challenge
    status TEXT NOT NULL DEFAULT 'pending',
    -- sha256 of the mainnet algorithm's code, so an agent that benchmarks the
    -- seed unchanged is recognised and its score adopted for free.
    code_fingerprint TEXT,
    config_fingerprint TEXT,
    measured_by TEXT,
    benchmarked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contributor_configs (
    username TEXT PRIMARY KEY,
    config_json TEXT,
    tacit_text TEXT,
    updated_at TEXT NOT NULL
);

-- Failed-attempts archive: LLM-authored artifacts only (structured
-- retrospectives written when a trajectory dies, plus the one-line "- LLM:"
-- tacit lessons). The lightweight per-iteration failure record is DERIVED
-- from experiments (beats_trajectory_best=0) at read time, never written
-- here — no code bodies either; experiment_id links back to experiments.
-- Gated by config.failed_attempts_archive; records are only ever served
-- back to the agent that wrote them (per-agent visibility).
CREATE TABLE IF NOT EXISTS failure_records (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    challenge TEXT NOT NULL,
    trajectory_id TEXT,
    experiment_id TEXT,
    kind TEXT NOT NULL DEFAULT 'retrospective',  -- 'retrospective' | 'lesson'
    approach_summary TEXT DEFAULT '',
    what_was_tried TEXT DEFAULT '',
    observed_outcome TEXT DEFAULT '',
    possible_reasons TEXT DEFAULT '',
    lesson TEXT DEFAULT '',
    best_score REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id)
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
CREATE INDEX IF NOT EXISTS idx_trajectory_bests_score ON trajectory_bests(feasible, score);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_hyp_agent_target ON hypotheses(agent_id, target_best_experiment_id);
CREATE INDEX IF NOT EXISTS idx_hyp_program_id ON hypotheses(program_id);
CREATE INDEX IF NOT EXISTS idx_trajectory_bests_challenge ON trajectory_bests(challenge, feasible, score);
CREATE INDEX IF NOT EXISTS idx_experiments_challenge ON experiments(challenge, agent_id);
CREATE INDEX IF NOT EXISTS idx_hyp_challenge_agent ON hypotheses(challenge, agent_id, target_best_experiment_id);
CREATE INDEX IF NOT EXISTS idx_inactive_challenge ON inactive_algorithms(challenge);
CREATE INDEX IF NOT EXISTS idx_trajectories_challenge ON trajectories(challenge);
CREATE INDEX IF NOT EXISTS idx_best_history_challenge ON best_history(challenge, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_challenge_created ON messages(challenge, created_at);
CREATE INDEX IF NOT EXISTS idx_acs_challenge ON agent_challenge_state(challenge);
CREATE INDEX IF NOT EXISTS idx_acs_active ON agent_challenge_state(challenge, last_active_at);
-- Covers get_baseline_score: WHERE feasible=1 AND challenge=? ORDER BY created_at ASC LIMIT 1.
-- Called from periodic_stats per-challenge, /api/state per fetch, /api/iterations per publish.
CREATE INDEX IF NOT EXISTS idx_exp_baseline ON experiments(challenge, feasible, created_at);
-- Lookup seeds for a challenge. Diversity is now driven by code-similarity
-- admission (server/seed_diversity.py), NOT a per-(tag, source) unique index —
-- the old idx_seed_pool_dedup is dropped in the init_db migrations.
CREATE INDEX IF NOT EXISTS idx_seed_pool_lookup ON seed_pool(challenge, strategy_tag);
CREATE INDEX IF NOT EXISTS idx_failure_records_agent ON failure_records(agent_id, challenge, created_at);
"""

DEFAULT_CONFIG = {
    # Global swarm config in the singleton key/value table. Per-challenge
    # config (tracks, timeout, scoring_direction, initial_algorithm_code)
    # lives in `challenge_configs`, not here.
    #
    # `active_challenge` is the swarm-wide challenge the owner has chosen;
    # contributors auto-follow it via `python setup.py sync`. Only the
    # owner (admin_key holder) can change it via POST /api/swarm_config.
    "active_challenge": DEFAULT_CHALLENGE,
    "swarm_name": "",
    "owner_name": "",
    "swarm_type": "cpu",
    "hypothesis_recall_threshold": "3",
    # Public URL of this swarm's hosted fleet runner (Tier 1), if the host
    # deployed one. Empty = no cloud-run option; the join page then only
    # offers the local runner. Set via POST /api/swarm_config (admin).
    "runner_url": "",
}


# ── Schema versioning ────────────────────────────────────────────────
#
# Every schema change is a numbered `Migration`, applied once and recorded in
# the `schema_version` table. Before this, init_db re-ran ~24 idempotent
# statements on every boot: workable, but with no ordering guarantee, no record
# of what had run, and no way to tell a fully-migrated DB from a half-migrated
# one. Ordering is load-bearing here — the trajectory_bests rebuild below has to
# run after the _add_column steps that create the columns it copies, or the
# first boot after an upgrade silently drops them.
#
# Adopting a pre-versioning database is safe: every migration is individually
# idempotent (that is exactly what made the old re-run-everything approach
# work), so an unstamped DB simply runs them all once and is stamped to head.
#
# Adding a migration: append one entry with the next version number. Never
# renumber, never edit a released migration in place — a deployed DB has
# already recorded it as applied and will not run it again. (The numbers
# below were assigned when this list was introduced, before any database
# had recorded them, which is the only moment renumbering is free.)

SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    """One numbered schema step.

    `own_connection` marks a migration that opens its own aiosqlite connection
    (the table rebuild does — it wraps BEGIN/DROP TABLE and must not nest
    inside init_db's transaction). The runner commits and hands it no db.
    """

    version: int
    name: str
    apply: Callable[..., Awaitable[None]]
    own_connection: bool = False


def _add_col_migration(version: int, table: str, column: str,
                       typedef: str) -> Migration:
    """Shorthand for the common case: one nullable/defaulted column."""
    return Migration(
        version, f"{table}.{column}",
        lambda db, t=table, c=column, d=typedef: _add_column(db, t, c, d),
    )


async def _add_column(db, table: str, column: str, typedef: str) -> None:
    # Idempotent ALTER for legacy DBs that predate columns now in SCHEMA.
    # Only swallow the duplicate-column error — anything else (locked DB,
    # type mismatch, etc.) must surface so init_db doesn't silently leave
    # the schema half-migrated.
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


# Owner-intended swarm config, injected as Railway service variables by
# `setup.py create` *before* the first deploy. The global scalars travel as
# plain vars; the per-challenge sub-configs (which include the initial
# algorithm/kernel code and so can be tens of KB) travel as one gzip+base64
# JSON blob to stay well within Railway's per-variable size limit.
_ENV_CONFIG_KEYS = (
    ("ACTIVE_CHALLENGE", "active_challenge"),
    ("SWARM_TYPE", "swarm_type"),
    ("SWARM_NAME", "swarm_name"),
    ("OWNER_NAME", "owner_name"),
    ("STAGNATION_THRESHOLD", "stagnation_threshold"),
    ("STAGNATION_LIMIT", "stagnation_limit"),
    ("HYPOTHESIS_RECALL_THRESHOLD", "hypothesis_recall_threshold"),
    ("HPO_FIRST_TUNE_IMPROVEMENTS", "hpo_first_tune_improvements"),
    ("HPO_MIN_IMPROVEMENTS", "hpo_min_improvements"),
    ("HPO_SEARCH_BUDGET", "hpo_search_budget"),
    ("HPO_NUM_SUGGESTED_CONFIGS", "hpo_num_suggested_configs"),
    ("FAILED_ATTEMPTS_ARCHIVE", "failed_attempts_archive"),
)


async def _apply_env_swarm_config(db: aiosqlite.Connection) -> None:
    """Apply the owner's intended swarm config from environment variables.

    `init_db` seeds the bare `DEFAULT_CONFIG` (active_challenge=satisfiability,
    swarm_type=cpu, no challenge_configs) with INSERT OR IGNORE and never
    overwrites it. Historically the *real* config was applied only by the
    create-time `POST /api/swarm_config`, which races the Railway rollout: the
    POST can land on a transient container during deploy and be discarded once
    the persistent /data volume's container becomes authoritative, stranding
    the swarm on the defaults forever (the volume keeps them across redeploys).

    Applying the config here — from vars `setup.py create` sets before the
    first deploy — makes the server come up correctly configured with no
    dependence on a POST landing. The caller runs this once per fresh DB
    (first-boot sentinel) so it authoritatively overrides the just-seeded
    DEFAULT_CONFIG without later clobbering owner runtime changes (e.g. a
    `setup.py switch`). Mirrors how ADMIN_KEY / SWARM_PASSWORD are injected,
    except those are safe to re-assert every boot and this is seed-once.
    """
    for env_name, cfg_key in _ENV_CONFIG_KEYS:
        val = os.environ.get(env_name)
        if val:
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (cfg_key, val),
            )

    blob = os.environ.get("SWARM_CHALLENGES_B64")
    if not blob:
        return
    challenges = json.loads(gzip.decompress(base64.b64decode(blob)).decode())
    for ch, sub in challenges.items():
        await upsert_challenge_config(
            db, ch,
            tracks=json.dumps(sub["tracks"]) if sub.get("tracks") is not None else None,
            timeout=sub.get("timeout"),
            scoring_direction=sub.get("scoring_direction"),
            initial_algorithm_code=sub.get("initial_algorithm_code"),
            initial_kernel_code=sub.get("initial_kernel_code"),
            strategy_tags=json.dumps(sub["strategy_tags"]) if sub.get("strategy_tags") is not None else None,
        )


async def _column_is_notnull(db, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    for row in await cursor.fetchall():
        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
        if row[1] == column:
            return bool(row[3])
    return False


# The ordered migration list. Comments preserved from the inline statements
# these replace — each explains why a column exists and how legacy rows read.
MIGRATIONS: tuple[Migration, ...] = (
    # Per-agent session token, generated at register. Used by every
    # non-register participant-write endpoint instead of the swarm password
    # (which is only consumed at register).
    _add_col_migration(1, "agents", "token", "TEXT"),
    # Contributor username stamped at register. Lets the dashboard group agents
    # by owner. Derived from the X-Username header at register time.
    _add_col_migration(2, "agents", "contributor_username", "TEXT"),
    # Auto-classified model tier (frontier/standard), drives seeding. Pre-tier
    # rows back-fill to 'standard'; read via COALESCE for safety.
    _add_col_migration(3, "agents", "tier", "TEXT DEFAULT 'standard'"),
    # Token accounting.
    _add_col_migration(4, "experiments", "input_tokens", "INTEGER DEFAULT 0"),
    _add_col_migration(5, "experiments", "output_tokens", "INTEGER DEFAULT 0"),
    _add_col_migration(6, "experiments", "estimated_cost", "REAL DEFAULT 0.0"),
    _add_col_migration(7, "agent_challenge_state", "total_input_tokens", "INTEGER DEFAULT 0"),
    _add_col_migration(8, "agent_challenge_state", "total_output_tokens", "INTEGER DEFAULT 0"),
    _add_col_migration(9, "agent_challenge_state", "total_estimated_cost", "REAL DEFAULT 0.0"),
    # Set to 1 the first time an agent publishes a benchmarked iteration on a
    # challenge. Stays 0 for an agent that never produced anything the
    # benchmark could run — a quiet "never benchmarked" signal (no feed noise).
    _add_col_migration(10, "agent_challenge_state", "ever_benchmarked", "INTEGER DEFAULT 0"),
    # Inspiration-source trajectory capture (see schema comments). Rows from
    # before this stay NULL; the inspiration matrix falls back to
    # reconstruction for those and uses the column for everything after.
    _add_col_migration(11, "experiments", "inspiration_source_trajectory_id", "TEXT"),
    _add_col_migration(12, "agent_challenge_state", "pending_inspiration_source_trajectory", "TEXT"),
    # Real experiment that earned a deposited best, carried through the
    # inactive pool so adoption inherits true provenance (see deposit_inactive
    # / the adoption branch in server.py). Older rows back-fill to NULL — their
    # originating experiment_id was never stored.
    _add_col_migration(13, "inactive_algorithms", "experiment_id", "TEXT"),
    # Winning hyperparameter config (JSON) for a trajectory best tuned by the
    # hyperparameter search. NULL for untuned bests. See
    # docs/hyperparameter-search.md.
    _add_col_migration(14, "trajectory_bests", "hyperparameters", "TEXT"),
    # The no-hyperparameters (default-config) score for this experiment. The
    # HPO gate's band is default-vs-default, so improvement scores are read
    # from here (COALESCE to `score` for untuned iterations).
    _add_col_migration(15, "experiments", "default_score", "REAL"),
    # Publishing agent's role ("explorer"/"exploiter") at iteration time, for
    # attribution. NULL for clients that don't send it.
    _add_col_migration(16, "hypotheses", "role", "TEXT"),
    # Multi-file algorithm bundle: a JSON {relpath: content} map (keys relative
    # to the algorithm dir, `mod.rs` is the entry). NULL for single-file rows,
    # where `algorithm_code` is the whole algorithm. When set it is the source
    # of truth; `algorithm_code` keeps the entry file for back-compat. Carried
    # through every place a stored algorithm can be handed to another agent.
    _add_col_migration(17, "experiments", "algorithm_files", "TEXT"),
    _add_col_migration(18, "trajectory_bests", "algorithm_files", "TEXT"),
    _add_col_migration(19, "seed_pool", "algorithm_files", "TEXT"),
    _add_col_migration(20, "inactive_algorithms", "algorithm_files", "TEXT"),
    # Per-iteration winning hyperparameter map (JSON) when this experiment was
    # tuned, else NULL. Lets the HPO gate ask "has this trajectory tuned
    # before?" (the first eligible candidate auto-fires; later ones respect the
    # improvement band).
    _add_col_migration(21, "experiments", "hyperparameters", "TEXT"),
    # Seed-pool diversity moved from a per-(tag, source) UNIQUE index to
    # code-similarity admission (server/seed_diversity.py). Drop the old unique
    # index so multiple seeds can share a strategy_tag and admission is decided
    # by similarity/LOC/cap, not first-feasible-per-tag.
    Migration(
        22, "drop idx_seed_pool_dedup",
        lambda db: db.execute("DROP INDEX IF EXISTS idx_seed_pool_dedup"),
    ),
    # (The authored-seed dedup that used to sit here is NOT a migration — it is
    # a recurring boot repair. See _collapse_duplicate_authored_seeds.)
    # Offered-count for the third stagnation hint type, "failed_attempts"
    # (mirrors tacit_knowledge_count / inspiration_count).
    _add_col_migration(23, "agent_challenge_state", "failed_attempts_count", "INTEGER DEFAULT 0"),
    # MUST stay after 14 and 18: the rebuild copies hyperparameters and
    # algorithm_files, and would drop them if it ran first.
    Migration(
        24, "trajectory_bests.experiment_id nullable",
        lambda: _relax_trajectory_bests_experiment_id(),
        own_connection=True,
    ),
)

# Columns asserted present after migrating. A half-applied schema otherwise
# surfaces as a 500 on the first query that touches the missing column, long
# after boot and far from the cause.
_EXPECTED_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (m.name.split(".")[0], m.name.split(".")[1])
    for m in MIGRATIONS if "." in m.name and not m.own_connection
)


def _validate_migrations() -> None:
    """Numbering must be dense and strictly increasing from 1 — a gap or a
    duplicate means a migration was renumbered or lost in a merge, and a
    deployed DB would silently skip or re-run one."""
    versions = [m.version for m in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(
            f"MIGRATIONS must be numbered 1..N with no gaps; got {versions}"
        )


_validate_migrations()


async def _apply_migrations(db) -> list[str]:
    """Run every migration this DB has not recorded, in order. Returns the
    names applied (empty on an up-to-date DB, which is the steady state)."""
    await db.executescript(SCHEMA_VERSION_TABLE)
    cursor = await db.execute("SELECT version FROM schema_version")
    done = {row[0] for row in await cursor.fetchall()}
    applied: list[str] = []
    for m in MIGRATIONS:
        if m.version in done or m.own_connection:
            continue
        await m.apply(db)
        await db.execute(
            "INSERT INTO schema_version (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (m.version, m.name, datetime.now(timezone.utc).isoformat()),
        )
        applied.append(f"{m.version}:{m.name}")
    await db.commit()
    return applied


async def _apply_own_connection_migrations() -> None:
    """Migrations that manage their own connection, run after init_db's block
    has closed so they never nest inside its transaction."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_VERSION_TABLE)
        cursor = await db.execute("SELECT version FROM schema_version")
        done = {row[0] for row in await cursor.fetchall()}
        for m in MIGRATIONS:
            if not m.own_connection or m.version in done:
                continue
            await m.apply()
            await db.execute(
                "INSERT INTO schema_version (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (m.version, m.name, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()


async def _collapse_duplicate_authored_seeds(db) -> None:
    """Enforce one authored seed per (challenge, strategy_tag), every boot.

    NOT a migration, deliberately. It began as a fix for the window where the
    unique index was gone but /api/admin/seed_pool still assumed the DB deduped
    — but because it ran on every boot it has been acting as a standing repair
    ever since, and server/test_seed_pool_upsert.py depends on that: it inserts
    duplicates into an already-migrated DB and expects the next init_db to
    collapse them. Numbering it as a once-only migration would silently retire
    a live safety net, so it stays a boot-time invariant.

    Authored seeds are host-owned; the newest row wins. Harvested seeds may
    legitimately share a tag (similarity admission) and are never touched.
    """
    await db.execute(
        "DELETE FROM seed_pool WHERE source = 'authored' AND id NOT IN ("
        "  SELECT MAX(id) FROM seed_pool WHERE source = 'authored' "
        "  GROUP BY challenge, strategy_tag)"
    )


async def _verify_schema(db) -> None:
    """Fail loudly on a half-migrated schema, at boot, naming the column."""
    missing: list[str] = []
    by_table: dict[str, set[str]] = {}
    for table, column in _EXPECTED_COLUMNS:
        if table not in by_table:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            by_table[table] = {row[1] for row in await cursor.fetchall()}
        if column not in by_table[table]:
            missing.append(f"{table}.{column}")
    if missing:
        raise RuntimeError(
            "schema is incomplete after migration — missing "
            + ", ".join(missing)
            + ". The database may be from a newer version, or a migration "
            "failed partway. Refusing to serve on a half-applied schema."
        )


async def _relax_trajectory_bests_experiment_id() -> None:
    """One-time table rebuild dropping the NOT NULL on
    trajectory_bests.experiment_id, so an adopted floor with no known source
    experiment can store NULL instead of a fabricated id. SQLite can't drop a
    column constraint in place, hence the copy-and-swap. Idempotent: a no-op
    once experiment_id is already nullable (fresh DBs created from SCHEMA, or
    a DB already migrated). Runs in its own connection AFTER the SCHEMA /
    SCHEMA_INDEXES pass so the source table definitely exists."""
    # Deliberately a separate aiosqlite connection from init_db's: this is
    # wrapped in BEGIN/COMMIT and a DROP TABLE, which we keep isolated.
    async with aiosqlite.connect(DB_PATH) as db:
        if not await _column_is_notnull(db, "trajectory_bests", "experiment_id"):
            return
        await db.executescript(
            """
            BEGIN;
            CREATE TABLE trajectory_bests_new (
                agent_id TEXT NOT NULL,
                challenge TEXT NOT NULL,
                experiment_id TEXT,
                algorithm_code TEXT NOT NULL,
                kernel_code TEXT,
                score REAL NOT NULL,
                feasible INTEGER NOT NULL DEFAULT 1,
                challenge_metrics TEXT,
                solution_data TEXT,
                track_scores TEXT,
                updated_at TEXT NOT NULL,
                trajectory_id TEXT,
                -- Added by _add_column earlier in init_db; the rebuild must
                -- carry them or the first boot after upgrade on a legacy DB
                -- drops them and every read of these columns 500s.
                hyperparameters TEXT,
                algorithm_files TEXT,
                PRIMARY KEY (agent_id, challenge),
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            );
            INSERT INTO trajectory_bests_new
                (agent_id, challenge, experiment_id, algorithm_code,
                 kernel_code, score, feasible, challenge_metrics,
                 solution_data, track_scores, updated_at, trajectory_id,
                 hyperparameters, algorithm_files)
                SELECT agent_id, challenge, experiment_id, algorithm_code,
                       kernel_code, score, feasible, challenge_metrics,
                       solution_data, track_scores, updated_at, trajectory_id,
                       hyperparameters, algorithm_files
                FROM trajectory_bests;
            DROP TABLE trajectory_bests;
            ALTER TABLE trajectory_bests_new RENAME TO trajectory_bests;
            CREATE INDEX IF NOT EXISTS idx_trajectory_bests_score
                ON trajectory_bests(feasible, score);
            CREATE INDEX IF NOT EXISTS idx_trajectory_bests_challenge
                ON trajectory_bests(challenge, feasible, score);
            COMMIT;
            """
        )
        await db.commit()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL mode is durable across connections — set once at startup, the
        # journal_mode persists in the DB file. Lets readers (dashboard WS,
        # /api/state) proceed concurrently with the single writer instead of
        # blocking on every commit, which matters as soon as the swarm has
        # >1 contributor.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await db.executescript(SCHEMA_INDEXES)
        await db.commit()

        # Numbered, recorded schema steps (see MIGRATIONS). Replaces the
        # ~24 statements that used to be re-run inline on every boot.
        applied = await _apply_migrations(db)
        if applied:
            # One line, not 24: a fresh DB applies them all and the names add
            # nothing. On an upgrade the range is the useful part.
            print(f"init_db: applied {len(applied)} migration(s) "
                  f"({applied[0]}..{applied[-1]})")
        await _verify_schema(db)
        # Boot-time invariants (not schema, not versioned) — see the docstring.
        await _collapse_duplicate_authored_seeds(db)
        await db.commit()
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
        # Swarm password: same priority as admin_key (env var wins, then
        # existing DB value, then a generated default on fresh DBs). Guards
        # the participant-write endpoints so a contributor needs URL + password
        # to register an agent or publish iterations.
        env_pw = os.environ.get("SWARM_PASSWORD")
        if env_pw:
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                ("swarm_password", env_pw),
            )
        else:
            await db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                ("swarm_password", secrets.token_urlsafe(16)),
            )
        await db.commit()

        # Seed the owner's intended swarm config from deploy-time env vars on
        # the FIRST boot of a fresh DB only (see `_apply_env_swarm_config`).
        # First-boot gating matters: the config keys here (active_challenge,
        # thresholds, …) are owner-tunable at runtime via POST /api/swarm_config
        # — e.g. `setup.py switch` flips active_challenge — and that runtime
        # state lives on the persistent /data volume. Re-applying env on every
        # boot would revert a switch on the next redeploy. A one-time sentinel
        # gives us "seed a fresh swarm correctly" without "clobber live state".
        # Defensive: a malformed var must never crash boot (an unreachable
        # server is an unfixable swarm) — log and carry on.
        cur = await db.execute(
            "SELECT 1 FROM config WHERE key = 'env_config_applied'"
        )
        if await cur.fetchone() is None:
            try:
                await _apply_env_swarm_config(db)
            except Exception as e:  # noqa: BLE001 — boot must survive any bad var
                print(f"init_db: could not apply env swarm config: {e!r}")
            await db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES ('env_config_applied', '1')"
            )
            await db.commit()

    # Migration 24 (trajectory_bests rebuild) opens its own connection, so it
    # runs after the block above has closed — see Migration.own_connection.
    await _apply_own_connection_migrations()


@asynccontextmanager
async def connect():
    """Context manager for DB connections — ensures cleanup on error.

    Per-connection PRAGMAs:
      - busy_timeout=5000: wait up to 5s for a contended write lock instead
        of failing immediately with SQLITE_BUSY. Under concurrent publish +
        periodic_stats load this avoids spurious 500s.
      - foreign_keys=ON: SQLite ships with FK enforcement OFF by default; the
        schema declares FKs (trajectory_bests.agent_id → agents.id, etc.) so we
        actually enforce them.
      - journal_mode=WAL is set once globally in init_db() — it's a database-
        level setting that persists in the file, not per-connection.
    """
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
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


# ── algorithm_files JSON codecs ──
#
# Multi-file algorithms are stored as one JSON {relpath: content} map in the
# `algorithm_files` column (experiments / trajectory_bests / seed_pool /
# inactive_algorithms all share the convention). Shared by server.py and
# trajectory_reset.py.


def files_json(files: dict | None) -> str | None:
    """JSON-encode a {relpath: content} files-map for storage, or None when it
    is empty/single-file (the entry lives in `algorithm_code`)."""
    return json.dumps(files) if files else None


def row_files(row) -> dict | None:
    """Decode a stored `algorithm_files` JSON column to a dict, or None."""
    raw = row.get("algorithm_files") if hasattr(row, "get") else None
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) and d else None


_TRAJECTORY_BESTS_COLS = (
    "agent_id, challenge, experiment_id as id, experiment_id, algorithm_code, "
    "kernel_code, algorithm_files, score, feasible, challenge_metrics, solution_data, "
    "track_scores, updated_at, trajectory_id, hyperparameters"
)


def score_epoch_key(challenge: str) -> str:
    """`config` key holding the cutoff for one challenge's leaderboard.

    Set by the admin "Reset leaderboard" action to the moment of the reset.
    Scores published before it stop counting as the global best, WITHOUT
    deleting the experiments themselves — the swarm's research history, the
    inspiration matrix and every trajectory chart still read those rows."""
    return f"score_epoch:{challenge}"


async def set_score_epoch(
    conn: aiosqlite.Connection, challenge: str, timestamp: str
) -> None:
    """Start a new scoring era for `challenge` as of `timestamp`."""
    await conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (score_epoch_key(challenge), timestamp),
    )


async def get_score_epoch(
    conn: aiosqlite.Connection, challenge: str
) -> str | None:
    cursor = await conn.execute(
        "SELECT value FROM config WHERE key = ?", (score_epoch_key(challenge),),
    )
    row = await cursor.fetchone()
    return row["value"] if row else None


async def get_global_best(
    conn: aiosqlite.Connection, challenge: str, *, direction: str
) -> dict | None:
    # Best-scoring feasible experiment for the challenge, across ALL
    # trajectories — active and inactive. Querying `experiments` (not
    # `trajectory_bests`) means the peak score from a now-deactivated trajectory
    # still counts: trajectory_bests is wiped per-agent on stagnation reset,
    # which would otherwise hide historical peaks once their trajectory
    # ended. Returned shape mirrors the prior trajectory_bests-based result so
    # callers don't need to change.
    #
    # The score epoch (set by POST /api/admin/reset_challenge) excludes rows
    # published before the last leaderboard reset. Without it a "reset" cleared
    # only the chart: this query still found the old peak in `experiments`, so
    # after a change that makes scores incomparable — new instance counts, a new
    # timeout — no legitimately lower score could ever become the new best.
    # Comparing ISO-8601 strings is a plain lexicographic compare, and the
    # COALESCE default of '' keeps every row when no epoch is set.
    order = _direction_order(direction)
    cursor = await conn.execute(
        "SELECT id as experiment_id, id, agent_id, challenge, "
        "algorithm_code, kernel_code, algorithm_files, score, feasible, challenge_metrics, "
        "solution_data, track_scores, created_at as updated_at, "
        "trajectory_id "
        "FROM experiments WHERE feasible = 1 AND challenge = ? "
        "  AND created_at > COALESCE("
        "        (SELECT value FROM config WHERE key = ?), '') "
        f"ORDER BY score {order} LIMIT 1",
        (challenge, score_epoch_key(challenge)),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_agent_tier(conn: aiosqlite.Connection, agent_id: str) -> str:
    """Auto-classified model tier for an agent. Legacy rows with NULL tier
    (predating the column) read as 'standard'."""
    cursor = await conn.execute(
        "SELECT COALESCE(tier, 'standard') AS tier FROM agents WHERE id = ?",
        (agent_id,),
    )
    row = await cursor.fetchone()
    return row["tier"] if row else "standard"


# ── Seed pool helpers ──


async def insert_seed(
    conn: aiosqlite.Connection,
    challenge: str,
    strategy_tag: str,
    algorithm_code: str,
    *,
    created_at: str,
    source: str = "authored",
    score: float | None = None,
    feasible: bool = True,
    kernel_code: str | None = None,
    origin_agent_id: str | None = None,
    algorithm_files: str | None = None,
) -> bool:
    """Insert a seed row. Plain insert — there is NO uniqueness constraint
    (the old per-(challenge, tag, source) UNIQUE index was dropped when pool
    diversity moved to similarity-based admission), so admission is the
    CALLER's job: harvested seeds go through seed_diversity.decide_admission,
    authored seeds through `upsert_authored_seed`. Returns True iff a row was
    added (kept for call-site compatibility; always True on success)."""
    cur = await conn.execute(
        "INSERT INTO seed_pool "
        "(challenge, strategy_tag, source, score, feasible, algorithm_code, "
        " kernel_code, algorithm_files, origin_agent_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (challenge, strategy_tag, source, score, 1 if feasible else 0,
         algorithm_code, kernel_code, algorithm_files, origin_agent_id, created_at),
    )
    return cur.rowcount > 0


async def upsert_authored_seed(
    conn: aiosqlite.Connection,
    challenge: str,
    strategy_tag: str,
    algorithm_code: str,
    *,
    created_at: str,
    score: float | None = None,
    kernel_code: str | None = None,
    algorithm_files: str | None = None,
) -> str:
    """Deposit a HOST-AUTHORED seed, keyed by (challenge, strategy_tag).

    Authored seeds mirror files under initial_algorithms/<ch>/seeds/ — the
    host owns them, so a re-deposit must REPLACE the pool copy (a host who
    fixes a seed and re-runs `setup.py create` expects agents to draw the
    fixed version), and repeat deposits of identical content must be no-ops
    (idempotent create re-runs). Harvested seeds are untouched — they share
    tags freely under similarity-based admission.

    Returns 'inserted' | 'updated' | 'unchanged'. Legacy duplicate authored
    rows for the key (from the era when the endpoint lost its dedupe) are
    collapsed to the newest row as a side effect."""
    cursor = await conn.execute(
        "SELECT id, algorithm_code, kernel_code, algorithm_files FROM seed_pool "
        "WHERE challenge = ? AND strategy_tag = ? AND source = 'authored' "
        "ORDER BY id DESC",
        (challenge, strategy_tag),
    )
    rows = await cursor.fetchall()  # positional access: works with or without row_factory
    if len(rows) > 1:  # collapse legacy duplicates, keep the newest
        stale = [r[0] for r in rows[1:]]
        await conn.execute(
            f"DELETE FROM seed_pool WHERE id IN ({','.join('?' * len(stale))})",
            stale,
        )
    if not rows:
        await insert_seed(
            conn, challenge, strategy_tag, algorithm_code,
            created_at=created_at, source="authored", score=score,
            feasible=True, kernel_code=kernel_code, algorithm_files=algorithm_files,
        )
        return "inserted"
    newest_id, newest_code, newest_kernel, newest_files = (
        rows[0][0], rows[0][1], rows[0][2], rows[0][3])
    if (newest_code == algorithm_code
            and (newest_kernel or None) == (kernel_code or None)
            and (newest_files or None) == (algorithm_files or None)):
        return "unchanged"
    await conn.execute(
        "UPDATE seed_pool SET algorithm_code = ?, kernel_code = ?, "
        "algorithm_files = ?, score = ?, feasible = 1, created_at = ? WHERE id = ?",
        (algorithm_code, kernel_code, algorithm_files, score, created_at, newest_id),
    )
    return "updated"


async def list_seeds(conn: aiosqlite.Connection, challenge: str) -> list[dict]:
    """All feasible seeds for a challenge, stable order (by tag then id) so
    a per-agent hash assignment is deterministic across calls."""
    cursor = await conn.execute(
        "SELECT id, strategy_tag, source, score, feasible, algorithm_code, "
        "kernel_code, algorithm_files FROM seed_pool WHERE challenge = ? AND feasible = 1 "
        "ORDER BY strategy_tag, id",
        (challenge,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def evict_seed(conn: aiosqlite.Connection, seed_id: int) -> None:
    """Remove one seed by id (used by similarity-based redundancy eviction)."""
    await conn.execute("DELETE FROM seed_pool WHERE id = ?", (seed_id,))


async def least_covered_tag(
    conn: aiosqlite.Connection, challenge: str, candidate_tags: list[str],
) -> str | None:
    """The candidate strategy_tag with the fewest hypotheses tried so far on
    this challenge (untried tags count as 0). Powers the soft niching
    suggestion for explorers. Ties break by candidate order. Returns None if
    no candidates are given."""
    if not candidate_tags:
        return None
    cursor = await conn.execute(
        "SELECT strategy_tag, COUNT(*) AS c FROM hypotheses "
        "WHERE challenge = ? GROUP BY strategy_tag",
        (challenge,),
    )
    counts = {r["strategy_tag"]: r["c"] for r in await cursor.fetchall()}
    return min(candidate_tags, key=lambda t: counts.get(t, 0))


async def get_trajectory_best(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> dict | None:
    cursor = await conn.execute(
        f"SELECT {_TRAJECTORY_BESTS_COLS} FROM trajectory_bests "
        "WHERE agent_id = ? AND challenge = ?",
        (agent_id, challenge),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_recent_improvement_scores(
    conn: aiosqlite.Connection, trajectory_id: str | None, limit: int
) -> list[float]:
    """The last `limit` feasible improvement scores on a trajectory, ascending.

    An "improvement" is an experiment that beat the trajectory best when it ran
    (`beats_trajectory_best = 1`), so the scores are monotonically increasing.
    Keyed by `trajectory_id`, which is preserved when a trajectory is adopted
    out of the inactive pool — so an adopted trajectory inherits its real
    improvement history (and survives process restarts). Returns [] when the
    trajectory is unknown or has no recorded improvements.

    Powers the hyperparameter-search gate (see docs/hyperparameter-search.md):
    the count is `len(...)`, the band floor is `result[-min_improvements]`, and
    the parent (band ceiling) is `result[-1]` — after the first tune the gate
    fires only when floor < candidate < parent (direction-aware).

    The scores are each improvement's *default* (no-hyperparameters) score, so the
    band is default-vs-default: an ancestor that tuned never raises the bar for its
    descendants. Falls back to the published `score` wherever `default_score`
    is absent — the common ongoing case is an untuned row, where the two are
    equal by definition.
    Note these default scores are not strictly monotonic (only the published
    scores are), but the band only needs the value from `min_improvements` ago.
    """
    if not trajectory_id:
        return []
    cursor = await conn.execute(
        """SELECT COALESCE(default_score, score) FROM experiments
           WHERE trajectory_id = ? AND beats_trajectory_best = 1 AND feasible = 1
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (trajectory_id, limit),
    )
    rows = await cursor.fetchall()
    # Fetched newest-first; reverse to oldest-first so [-min_improvements] is the
    # band floor and the list reads as the recent improvement sequence.
    return [float(r[0]) for r in reversed(rows)]


async def trajectory_has_tuned(
    conn: aiosqlite.Connection, trajectory_id: str | None
) -> bool:
    """True if any experiment on this trajectory was hyperparameter-tuned (has a
    non-NULL `hyperparameters` map). The HPO gate auto-fires the FIRST time a
    mature trajectory is eligible (this returns False), then defers to the
    improvement band thereafter. Keyed by trajectory_id so it survives adoption
    and process restarts. See docs/hyperparameter-search.md."""
    if not trajectory_id:
        return False
    cursor = await conn.execute(
        "SELECT 1 FROM experiments "
        "WHERE trajectory_id = ? AND hyperparameters IS NOT NULL LIMIT 1",
        (trajectory_id,),
    )
    return (await cursor.fetchone()) is not None


async def upsert_trajectory_best(
    conn: aiosqlite.Connection,
    agent_id: str,
    challenge: str,
    experiment_id: str,
    algorithm_code: str,
    score: float,
    feasible: bool,
    challenge_metrics: str | None,
    solution_data: str | None,
    updated_at: str,
    trajectory_id: str | None = None,
    track_scores: str | None = None,
    kernel_code: str | None = None,
    hyperparameters: str | None = None,
    algorithm_files: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO trajectory_bests
           (agent_id, challenge, experiment_id, algorithm_code, kernel_code, algorithm_files,
            score, feasible,
            challenge_metrics, solution_data, track_scores, updated_at, trajectory_id,
            hyperparameters)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id, challenge) DO UPDATE SET
             experiment_id = excluded.experiment_id,
             algorithm_code = excluded.algorithm_code,
             kernel_code = excluded.kernel_code,
             algorithm_files = excluded.algorithm_files,
             score = excluded.score,
             feasible = excluded.feasible,
             challenge_metrics = excluded.challenge_metrics,
             solution_data = excluded.solution_data,
             track_scores = excluded.track_scores,
             updated_at = excluded.updated_at,
             trajectory_id = excluded.trajectory_id,
             hyperparameters = excluded.hyperparameters""",
        (agent_id, challenge, experiment_id, algorithm_code, kernel_code, algorithm_files,
         score, 1 if feasible else 0, challenge_metrics,
         solution_data, track_scores, updated_at, trajectory_id,
         hyperparameters),
    )


async def list_trajectory_bests(
    conn: aiosqlite.Connection,
    challenge: str,
    *,
    direction: str,
    exclude_agent_ids: list[str] | None = None,
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
        "       ab.algorithm_code, ab.kernel_code, ab.score, ab.feasible, "
        "       ab.challenge_metrics, ab.solution_data, ab.updated_at "
        f"FROM trajectory_bests ab{join_clause} WHERE " + " AND ".join(where) +
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
    *,
    direction: str,
) -> list[dict]:
    # Per-challenge leaderboard. Only includes agents that have actually
    # PUBLISHED at least one iteration on this challenge. An agent that
    # only ever fetched /api/state for this challenge gets a row in
    # agent_challenge_state via ensure_agent_challenge_state, but with
    # zero experiments — those would otherwise show up as ghosts.
    #
    # tacit/inspiration counts are derived from experiments.received_hint
    # (hints actually CONSUMED by a published iteration), not from the
    # acs.tacit_knowledge_count / acs.inspiration_count columns. Those
    # columns are bumped at hint-OFFER time on every /api/state fetch
    # while stagnated, so a client stuck in a fetch→fail→retry loop
    # inflates them without ever running an iteration (observed: 703
    # offers vs 6 consumed). They remain as raw offer telemetry only.
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
            a.llm_type as llm_type,
            acs.experiments_completed as runs,
            acs.improvements as improvements,
            acs.runs_since_improvement as runs_since_improvement,
            acs.last_active_at as last_active_at,
            acs.best_ever_score as best_ever_score,
            acs.num_trajectories as num_trajectories,
            COALESCE(hints.tacit_knowledge_count, 0) as tacit_knowledge_count,
            COALESCE(hints.inspiration_count, 0) as inspiration_count,
            COALESCE(hints.failed_attempts_count, 0) as failed_attempts_count,
            acs.total_input_tokens as total_input_tokens,
            acs.total_output_tokens as total_output_tokens,
            acs.total_estimated_cost as total_estimated_cost,
            ab.score as current_score
        FROM agent_challenge_state acs
        JOIN agents a ON a.id = acs.agent_id
        LEFT JOIN trajectory_bests ab
            ON ab.agent_id = a.id AND ab.challenge = ? AND ab.feasible = 1
        LEFT JOIN (
            SELECT agent_id,
                   SUM(CASE WHEN received_hint = 'tacit_knowledge' THEN 1 ELSE 0 END)
                       AS tacit_knowledge_count,
                   SUM(CASE WHEN received_hint = 'inspiration' THEN 1 ELSE 0 END)
                       AS inspiration_count,
                   SUM(CASE WHEN received_hint = 'failed_attempts' THEN 1 ELSE 0 END)
                       AS failed_attempts_count
            FROM experiments
            WHERE challenge = ?
            GROUP BY agent_id
        ) hints ON hints.agent_id = a.id
        WHERE acs.challenge = ?
          AND acs.experiments_completed > 0
        -- Sort by best-ever score (not current_score from trajectory_bests):
        -- trajectory_bests is cleared when a trajectory stagnates or the
        -- inactivity sweep fires, so current_score goes NULL for agents
        -- whose trajectory has ended even though their historical peak
        -- is still meaningful. best_ever_score on acs is monotonic — it
        -- captures the agent's highest score across every trajectory
        -- they've ever held on this challenge.
        ORDER BY best_ever_score IS NULL, best_ever_score {order}, a.name ASC
        """,
        (challenge, challenge, challenge),
    )
    rows = await cursor.fetchall()
    return [
        {
            "rank": i + 1,
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "llm_type": row["llm_type"] or "",
            "runs": row["runs"],
            "improvements": row["improvements"],
            "runs_since_improvement": row["runs_since_improvement"],
            "current_score": row["current_score"],
            "best_ever_score": row["best_ever_score"],
            "num_trajectories": row["num_trajectories"] or 0,
            "tacit_knowledge_count": row["tacit_knowledge_count"] or 0,
            "inspiration_count": row["inspiration_count"] or 0,
            "failed_attempts_count": row["failed_attempts_count"] or 0,
            "total_tokens": (row["total_input_tokens"] or 0) + (row["total_output_tokens"] or 0),
            "estimated_cost_usd": round(row["total_estimated_cost"] or 0, 4),
            "active": row["last_active_at"] >= inactive_cutoff if inactive_cutoff and row["last_active_at"] else False,
        }
        for i, row in enumerate(rows)
    ]


async def get_challenge_total_agents(
    conn: aiosqlite.Connection, challenge: str
) -> int:
    """Count of distinct agents that have actually PUBLISHED at least one
    experiment on this challenge.

    Was previously sourced from agent_challenge_state, which is created
    lazily on the first /api/state hit — so any agent that registered,
    fetched state once, then died (LLM API error before its first publish,
    bad config, crashed worker) showed up in this count forever. That
    diverged from compute_leaderboard, which already filters to "agents
    that have published at least once" — meaning the dashboard's AGENTS
    counter could read higher than the rows in its own leaderboard.

    Sourcing from experiments fixes that divergence: an agent only counts
    once it has done useful work."""
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT agent_id) as c FROM experiments WHERE challenge = ?",
        (challenge,),
    )
    row = await cur.fetchone()
    return row["c"] if row else 0


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
    failed_attempts_inc: int = 0,
    best_ever_score: float | None = None,
    direction: str = "max",
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost: float = 0.0,
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
                inspiration_count = inspiration_count + ?,
                failed_attempts_count = failed_attempts_count + ?,
                total_input_tokens = total_input_tokens + ?,
                total_output_tokens = total_output_tokens + ?,
                total_estimated_cost = total_estimated_cost + ?
                {best_clause}
              WHERE agent_id = ? AND challenge = ?"""
    params: list = [runs, improvements, num_trajectories_inc,
                    tacit_knowledge_inc, inspiration_inc, failed_attempts_inc,
                    input_tokens, output_tokens, estimated_cost]
    if best_ever_score is not None:
        params.extend([best_ever_score, best_ever_score, best_ever_score])
    params.extend([agent_id, challenge])
    await conn.execute(sql, params)


# ── failure_records helpers ──


async def insert_failure_record(
    conn: aiosqlite.Connection,
    *,
    record_id: str,
    agent_id: str,
    challenge: str,
    trajectory_id: str | None,
    experiment_id: str | None,
    kind: str,
    approach_summary: str,
    what_was_tried: str,
    observed_outcome: str,
    possible_reasons: str,
    lesson: str,
    best_score: float | None,
    created_at: str,
    max_per_agent: int,
) -> None:
    """Insert an LLM-authored failure artifact and prune retention in the
    same transaction: only the newest `max_per_agent` rows survive per
    (agent, challenge)."""
    await conn.execute(
        """INSERT INTO failure_records
             (id, agent_id, challenge, trajectory_id, experiment_id, kind,
              approach_summary, what_was_tried, observed_outcome,
              possible_reasons, lesson, best_score, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (record_id, agent_id, challenge, trajectory_id, experiment_id, kind,
         approach_summary, what_was_tried, observed_outcome,
         possible_reasons, lesson, best_score, created_at),
    )
    await conn.execute(
        """DELETE FROM failure_records
            WHERE agent_id = ? AND challenge = ?
              AND id NOT IN (SELECT id FROM failure_records
                              WHERE agent_id = ? AND challenge = ?
                              ORDER BY created_at DESC, id DESC LIMIT ?)""",
        (agent_id, challenge, agent_id, challenge, max(1, int(max_per_agent))),
    )


async def list_failure_records(
    conn: aiosqlite.Connection, agent_id: str, challenge: str, limit: int
) -> list[dict]:
    """Newest-first LLM-authored failure artifacts for ONE agent. The
    agent_id filter is the per-agent-visibility guarantee — records are
    never served across agents."""
    cursor = await conn.execute(
        """SELECT id, trajectory_id, experiment_id, kind, approach_summary,
                  what_was_tried, observed_outcome, possible_reasons,
                  lesson, best_score, created_at
             FROM failure_records
            WHERE agent_id = ? AND challenge = ?
            ORDER BY created_at DESC, id DESC LIMIT ?""",
        (agent_id, challenge, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_rejected_experiments(
    conn: aiosqlite.Connection, agent_id: str, challenge: str, limit: int
) -> list[dict]:
    """Derived lightweight failure records: the agent's own most recent
    non-improving iterations, joined to their hypothesis. No new storage —
    experiments already keeps everything."""
    cursor = await conn.execute(
        """SELECT e.id, e.score, e.feasible, e.delta_vs_trajectory_best_pct,
                  e.trajectory_id, e.created_at, e.notes,
                  h.title, h.strategy_tag, h.description
             FROM experiments e
             LEFT JOIN hypotheses h ON h.id = e.hypothesis_id
            WHERE e.agent_id = ? AND e.challenge = ?
              AND e.beats_trajectory_best = 0
            ORDER BY e.created_at DESC LIMIT ?""",
        (agent_id, challenge, limit),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    for r in rows:
        r["description"] = (r.get("description") or "")[:500]
        r["notes"] = (r.get("notes") or "")[:500]
    return rows


async def has_failure_material(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> bool:
    """True when the failed_attempts hint has something to serve for this
    agent: an archived LLM record OR a rejected iteration to derive a
    lightweight record from. Two cheap indexed EXISTS."""
    cursor = await conn.execute(
        """SELECT EXISTS(SELECT 1 FROM failure_records
                          WHERE agent_id = ? AND challenge = ?)
                OR EXISTS(SELECT 1 FROM experiments
                           WHERE agent_id = ? AND challenge = ?
                             AND beats_trajectory_best = 0) AS present""",
        (agent_id, challenge, agent_id, challenge),
    )
    row = await cursor.fetchone()
    return bool(row["present"]) if row else False


# ── challenge_configs helpers ──


_CHALLENGE_CONFIG_COLS = (
    "challenge, tracks, timeout, scoring_direction, "
    "initial_algorithm_code, initial_kernel_code, strategy_tags"
)


async def get_challenge_config(
    conn: aiosqlite.Connection, challenge: str
) -> dict | None:
    cursor = await conn.execute(
        f"SELECT {_CHALLENGE_CONFIG_COLS} "
        "FROM challenge_configs WHERE challenge = ?",
        (challenge,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# ── Mainnet baseline ────────────────────────────────────────────────


def config_fingerprint(tracks: object, timeout: object) -> str:
    """Identity of the instance set a score was earned on.

    Two scores are only comparable if they solved the same problems, so the
    dashboard needs to know when the host has edited tracks or the solver
    timeout since the baseline was measured."""
    payload = json.dumps({"tracks": tracks, "timeout": timeout}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def code_fingerprint(algorithm_code: str) -> str:
    """Identity of an algorithm's source, so a score published for the
    unmodified mainnet algorithm can be recognised wherever it comes from.

    Line endings are normalised first. The code makes a round trip the server
    does not control — deposited here, written to an agent's worktree (where
    challenge_files._safe_write rewrites CRLF to LF), benchmarked, read back
    and published — so raw bytes are not preserved end to end. A mainnet
    algorithm arrives base64-decoded from GitHub and may well carry CRLF, in
    which case a byte-exact hash can never match what comes back."""
    normalised = algorithm_code.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


async def get_mainnet_baseline(
    conn: aiosqlite.Connection, challenge: str,
) -> dict | None:
    cursor = await conn.execute(
        "SELECT * FROM mainnet_baselines WHERE challenge = ?", (challenge,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def record_mainnet_algorithm(
    conn: aiosqlite.Connection,
    challenge: str,
    algo_name: str,
    *,
    created_at: str,
    adoption_pct: float | None = None,
    code_fingerprint_: str | None = None,
) -> str:
    """Note WHICH mainnet algorithm this challenge is measured against, without
    measuring it. One INSERT — safe to call from anything on the setup path,
    because it adds no benchmark and no network round trip of its own.

    Returns 'inserted' | 'updated' | 'unchanged'. A new algorithm (mainnet's
    top-adoption entry changed) resets the row to pending: the old score
    belongs to different code."""
    existing = await get_mainnet_baseline(conn, challenge)
    if existing and existing["algo_name"] == algo_name and (
        code_fingerprint_ is None or existing["code_fingerprint"] == code_fingerprint_
    ):
        return "unchanged"
    if existing:
        await conn.execute(
            "UPDATE mainnet_baselines SET algo_name = ?, adoption_pct = ?, "
            "code_fingerprint = ?, score = NULL, status = 'pending', "
            "config_fingerprint = NULL, measured_by = NULL, benchmarked_at = NULL "
            "WHERE challenge = ?",
            (algo_name, adoption_pct, code_fingerprint_, challenge),
        )
        return "updated"
    await conn.execute(
        "INSERT INTO mainnet_baselines (challenge, algo_name, adoption_pct, "
        "code_fingerprint, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
        (challenge, algo_name, adoption_pct, code_fingerprint_, created_at),
    )
    return "inserted"


# States in which a published run of the mainnet code is still wanted. Shared
# by the claim, the adoption preference, and the capture — they have to agree,
# or the flow stalls at whichever one disagrees (it has, once already).
MAINNET_UNMEASURED = ("pending", "requested", "measuring")


async def claim_mainnet_measurement(
    conn: aiosqlite.Connection, challenge: str, agent_id: str, *,
    now_ts: str, stale_before: str,
) -> bool:
    """Reserve the measurement for exactly one agent. Returns whether we got it.

    Without a claim, every agent polling for state would be handed the same
    forced reset and they would all abandon their trajectories to benchmark
    the same algorithm. `stale_before` re-arms a claim whose agent died mid-run
    so one crash can't park the measurement forever."""
    cursor = await conn.execute(
        "UPDATE mainnet_baselines SET status = 'measuring', measured_by = ?, "
        "benchmarked_at = ? "
        "WHERE challenge = ? AND ("
        "  status = 'requested'"
        "  OR (status = 'measuring' AND (benchmarked_at IS NULL "
        "                                OR benchmarked_at < ?))"
        ")",
        (f"agent:{agent_id}", now_ts, challenge, stale_before),
    )
    return cursor.rowcount > 0


async def set_mainnet_baseline_score(
    conn: aiosqlite.Connection,
    challenge: str,
    score: float,
    *,
    feasible: bool,
    benchmarked_at: str,
    measured_by: str,
    config_fingerprint_: str | None = None,
) -> bool:
    """Fill in a measured score. Returns False when no row exists — the caller
    must have recorded WHICH algorithm it measured first, so a score can never
    be attributed to an unknown one."""
    cursor = await conn.execute(
        "UPDATE mainnet_baselines SET score = ?, feasible = ?, status = 'ready', "
        "config_fingerprint = ?, measured_by = ?, benchmarked_at = ? "
        "WHERE challenge = ?",
        (score, 1 if feasible else 0, config_fingerprint_, measured_by,
         benchmarked_at, challenge),
    )
    return cursor.rowcount > 0


async def mark_mainnet_baseline_unavailable(
    conn: aiosqlite.Connection, challenge: str, *, created_at: str,
) -> None:
    """No compatible mainnet algorithm exists for this challenge. Recorded so
    the dashboard can say so once instead of showing a permanent 'pending'."""
    await conn.execute(
        "INSERT INTO mainnet_baselines (challenge, algo_name, status, created_at) "
        "VALUES (?, '', 'unavailable', ?) "
        "ON CONFLICT(challenge) DO UPDATE SET status = 'unavailable'",
        (challenge, created_at),
    )


async def list_challenge_configs(conn: aiosqlite.Connection) -> list[dict]:
    cursor = await conn.execute(
        f"SELECT {_CHALLENGE_CONFIG_COLS} "
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
    initial_kernel_code: str | None = None,
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
    if initial_kernel_code is not None:
        sets.append("initial_kernel_code = ?")
        params.append(initial_kernel_code)
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


async def set_active_challenge(conn: aiosqlite.Connection, challenge: str) -> None:
    """Swarm-wide active challenge. Owner-set via POST /api/swarm_config.
    Reads go through `get_config_cached` in server.py, not a dedicated
    helper, so contributors hit the cache on every /api/state call."""
    await conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        ("active_challenge", challenge),
    )


async def ensure_synthetic_agent(
    conn: aiosqlite.Connection, name: str, timestamp: str,
) -> str:
    """Look up (or create) a synthetic agent row keyed by `name`. Used for
    pool entries that need an `agent_id` FK but don't originate from a real
    swarm contributor (e.g. seeds from the TIG mainnet).

    The synthetic row sets `last_heartbeat` to the creation time and never
    updates it, so it naturally falls outside `active_only=True` queries
    (leaderboards, inspiration) and won't compete with real agents.
    """
    cursor = await conn.execute(
        "SELECT id FROM agents WHERE name = ?", (name,),
    )
    row = await cursor.fetchone()
    if row:
        return row["id"]
    import uuid
    agent_id = uuid.uuid4().hex[:12]
    await conn.execute(
        "INSERT INTO agents "
        "  (id, name, registered_at, last_heartbeat, status, llm_type) "
        "VALUES (?, ?, ?, ?, 'idle', ?)",
        (agent_id, name, timestamp, timestamp, "tig-mainnet-seed"),
    )
    return agent_id


async def deposit_inactive(
    conn: aiosqlite.Connection,
    agent_id: str,
    challenge: str,
    algorithm_code: str,
    score: float | None,
    deposited_at: str,
    trajectory_id: str | None = None,
    program_id: str | None = None,
    kernel_code: str | None = None,
    experiment_id: str | None = None,
    algorithm_files: str | None = None,
) -> int:
    # Keep negative-scoring attempts out of the inactive trajectory pool.
    # The pool is what other agents adopt from on a fresh reset, so a
    # negative deposit hands known-bad code to whoever draws it — polluting
    # the pool regardless of how many edits the trajectory accumulated
    # (an iterated line that never climbed above zero is still a dead end).
    # Centralised here so BOTH deposit paths (online stagnation reset and
    # offline-agent cleanup) are covered. Returns -1 to signal the deposit
    # was skipped; callers ignore the row id.
    #
    # Note: on challenges whose feasible scores are themselves negative (e.g.
    # neuralnet divergence), this also drops feasible-but-negative results —
    # accepted tradeoff per the chosen `score < 0` definition of "bad".
    if score is not None and score < 0:
        return -1
    cursor = await conn.execute(
        "INSERT INTO inactive_algorithms "
        "  (agent_id, challenge, algorithm_code, kernel_code, algorithm_files, score, deposited_at, trajectory_id, program_id, experiment_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, challenge, algorithm_code, kernel_code, algorithm_files, score, deposited_at, trajectory_id, program_id, experiment_id),
    )
    return cursor.lastrowid


async def remove_inactive(conn: aiosqlite.Connection, inactive_id: int) -> None:
    await conn.execute(
        "DELETE FROM inactive_algorithms WHERE id = ?", (inactive_id,)
    )


async def clear_inactive_pool(
    conn: aiosqlite.Connection, challenge: str, keep_agent_id: str | None = None
) -> int:
    """Delete all inactive-pool entries for `challenge`, optionally keeping those
    attributed to `keep_agent_id` (e.g. a preserved seed source). Returns the
    number of rows removed."""
    if keep_agent_id:
        cur = await conn.execute(
            "DELETE FROM inactive_algorithms WHERE challenge = ? AND agent_id != ?",
            (challenge, keep_agent_id),
        )
    else:
        cur = await conn.execute(
            "DELETE FROM inactive_algorithms WHERE challenge = ?", (challenge,),
        )
    return cur.rowcount


async def has_inactive_with_code(
    conn: aiosqlite.Connection, agent_id: str, challenge: str, algorithm_code: str,
) -> bool:
    """Is this EXACT algorithm already sitting unconsumed in the pool for
    `agent_id`?

    The mainnet seeder needs this rather than a bare "does any row exist"
    count. It records a fingerprint of the code it fetched, and the capture
    and adoption paths both match on that hash — so what matters is not
    whether the source has *a* seed here, but whether the seed it has is the
    one the fingerprint describes. Two different reshapes of the same mainnet
    algorithm (host-side challenge_files vs server-side mainnet_seed, kept
    only in "rough sync") hash differently and would otherwise leave the
    baseline permanently unmeasurable."""
    cursor = await conn.execute(
        "SELECT algorithm_code FROM inactive_algorithms "
        "WHERE agent_id = ? AND challenge = ?",
        (agent_id, challenge),
    )
    target = code_fingerprint(algorithm_code)
    for row in await cursor.fetchall():
        if code_fingerprint(row["algorithm_code"] or "") == target:
            return True
    return False


async def count_inactive_from_agent(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> int:
    """Number of unconsumed inactive-pool rows on `challenge` attributed to
    `agent_id`. Used by the admin seeder's idempotency guard: a synthetic
    source agent (e.g. tig-foundation) that still has an unconsumed seed for
    this challenge shouldn't be re-seeded, so a repeated `setup.py create`
    doesn't pile up duplicate mainnet seeds in the pool. (Consume-once
    semantics mean a genuinely-adopted seed leaves no row, so the next create
    correctly re-seeds.)"""
    row = await (await conn.execute(
        "SELECT COUNT(*) AS c FROM inactive_algorithms "
        "WHERE agent_id = ? AND challenge = ?",
        (agent_id, challenge),
    )).fetchone()
    return row["c"]


async def trajectory_counts(
    conn: aiosqlite.Connection, challenge: str
) -> tuple[int, int]:
    """Return (n_trajectories, total_deactivations) for a challenge."""
    row = await (await conn.execute(
        "SELECT COUNT(*) as n, COALESCE(SUM(num_deactivations), 0) as total_d "
        "FROM trajectories WHERE challenge = ?",
        (challenge,),
    )).fetchone()
    return (row["n"], row["total_d"]) if row else (0, 0)


async def get_inactive_with_deactivations(
    conn: aiosqlite.Connection, challenge: str
) -> list[dict]:
    cursor = await conn.execute(
        "SELECT ia.id, ia.agent_id, ia.challenge, ia.algorithm_code, ia.kernel_code, "
        "  ia.algorithm_files, ia.score, "
        "  ia.trajectory_id, ia.program_id, ia.experiment_id, "
        "  COALESCE(t.num_deactivations, 1) as num_deactivations "
        "FROM inactive_algorithms ia "
        "LEFT JOIN trajectories t ON ia.trajectory_id = t.id "
        "WHERE ia.challenge = ?",
        (challenge,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def clear_trajectory_best(
    conn: aiosqlite.Connection, agent_id: str, challenge: str
) -> None:
    await conn.execute(
        "DELETE FROM trajectory_bests WHERE agent_id = ? AND challenge = ?",
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


async def get_trajectory(
    conn: aiosqlite.Connection, trajectory_id: str
) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM trajectories WHERE id = ?", (trajectory_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def deactivate_trajectory(
    conn: aiosqlite.Connection, trajectory_id: str, deactivated_at: str
) -> None:
    # `AND status = 'active'` makes this idempotent — callers that aren't
    # certain whether the trajectory has already been deactivated (e.g.
    # the inactivity sweep below) can re-fire without double-bumping
    # num_deactivations.
    await conn.execute(
        "UPDATE trajectories SET status = 'inactive', deactivated_at = ?, "
        "num_deactivations = num_deactivations + 1 "
        "WHERE id = ? AND status = 'active'",
        (deactivated_at, trajectory_id),
    )


async def deactivate_inactive_agent_trajectories(
    conn: aiosqlite.Connection, cutoff_ts: str, timestamp: str
) -> int:
    """Free up trajectories owned by agents who've gone silent.

    An agent that crashes or disconnects never hits the stagnation-reset
    path in `publish_iteration`, so their trajectory stays flagged
    `active` and their best algorithm is locked inside `trajectory_bests` —
    invisible to the per-challenge inactive pool that other agents draw
    inspiration from. This sweep handles that: for each (agent,
    challenge) whose `last_active_at` is older than `cutoff_ts` but
    still holds a `current_trajectory_id`, we:

      1. Mark the trajectory inactive (unless another agent that IS still
         live claims it as their current — shared trajectories shouldn't
         deactivate while anyone's still working on them).
      2. Deposit the agent's best into `inactive_algorithms` so it can be
         adopted from the pool.
      3. Clear their `trajectory_bests` row and null the trajectory pointer on
         their `agent_challenge_state` row, so a returning agent starts
         fresh on next /api/state.

    Returns the number of (agent, challenge) pairs processed.
    """
    cursor = await conn.execute(
        "SELECT agent_id, challenge, current_trajectory_id, current_program_id "
        "FROM agent_challenge_state "
        "WHERE last_active_at < ? AND current_trajectory_id IS NOT NULL",
        (cutoff_ts,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    processed = 0
    for row in rows:
        agent_id = row["agent_id"]
        challenge = row["challenge"]
        traj_id = row["current_trajectory_id"]
        program_id = row["current_program_id"]

        # Compare-and-swap: atomically clear the trajectory pointer ONLY if
        # the agent is still inactive AND still owns the same trajectory we
        # saw in the snapshot. Without this guard, a heartbeat or /api/state
        # call landing between the snapshot SELECT and this UPDATE would
        # have its trajectory clipped despite the agent being live again.
        # rowcount == 0 means the agent slipped out from under us; skip
        # all side-effects for this row.
        guard = await conn.execute(
            "UPDATE agent_challenge_state "
            "SET current_trajectory_id = NULL, current_program_id = NULL "
            "WHERE agent_id = ? AND challenge = ? "
            "  AND last_active_at < ? AND current_trajectory_id = ?",
            (agent_id, challenge, cutoff_ts, traj_id),
        )
        if (guard.rowcount or 0) == 0:
            continue
        processed += 1

        other_live = await (await conn.execute(
            "SELECT 1 FROM agent_challenge_state "
            "WHERE current_trajectory_id = ? AND last_active_at >= ? "
            "  AND NOT (agent_id = ? AND challenge = ?) LIMIT 1",
            (traj_id, cutoff_ts, agent_id, challenge),
        )).fetchone()
        if other_live is None:
            await deactivate_trajectory(conn, traj_id, timestamp)

        best = await get_trajectory_best(conn, agent_id, challenge)
        # Mirror the online stagnation-reset gate (server.py): only deposit a
        # feasible best. An infeasible entry would hand broken code to whoever
        # adopts it and spread the infeasible-floor trap. trajectory_bests is
        # normally feasible-only, but legacy rows / the adopted floor mean we
        # can't assume it. deposit_inactive applies the extra negative-score
        # guard on top.
        if best is not None:
            if best.get("feasible"):
                await deposit_inactive(
                    conn, agent_id, challenge,
                    best["algorithm_code"], best["score"], timestamp,
                    trajectory_id=traj_id, program_id=program_id,
                    kernel_code=best.get("kernel_code"),
                    experiment_id=best.get("experiment_id"),
                )
            await clear_trajectory_best(conn, agent_id, challenge)
    return processed


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
    """Trajectory rows enriched with the actual count of distinct agents that
    have published an experiment on each one. This `unique_agents` column is
    what the dashboard table renders — `num_agents` on the row is only ever
    bumped on creation / adoption, so it under-counts when the same active
    trajectory has been worked on by several agents."""
    if challenge is None:
        cursor = await conn.execute(
            """SELECT t.*,
                      (SELECT COUNT(DISTINCT e.agent_id)
                         FROM experiments e
                        WHERE e.trajectory_id = t.id) AS unique_agents
                 FROM trajectories t ORDER BY t.started_at DESC"""
        )
    else:
        cursor = await conn.execute(
            """SELECT t.*,
                      (SELECT COUNT(DISTINCT e.agent_id)
                         FROM experiments e
                        WHERE e.trajectory_id = t.id) AS unique_agents
                 FROM trajectories t
                 WHERE t.challenge = ?
                 ORDER BY t.started_at DESC""",
            (challenge,),
        )
    return [dict(row) for row in await cursor.fetchall()]


async def get_trajectory_score_history(
    conn: aiosqlite.Connection,
    trajectory_id: str,
    *,
    direction: str,
    challenge: str | None = None,
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


# ── Contributor configs (server-first onboarding P1) ──


async def get_contributor_config(
    conn: aiosqlite.Connection, username: str,
) -> dict | None:
    """The contributor's stored fleet config row, or None before first save.
    `config_json` is returned as stored (a JSON string or NULL) — the API
    layer owns encoding/decoding and validation."""
    cursor = await conn.execute(
        "SELECT config_json, tacit_text, updated_at "
        "FROM contributor_configs WHERE username = ?",
        (username,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def set_contributor_config(
    conn: aiosqlite.Connection, username: str,
    config_json: str | None, tacit_text: str | None, updated_at: str,
) -> None:
    """Full-row upsert. Partial-update semantics (PUT with only `config` or
    only `tacit`) are the caller's job: read the existing row, merge, write."""
    await conn.execute(
        "INSERT OR REPLACE INTO contributor_configs "
        "(username, config_json, tacit_text, updated_at) VALUES (?, ?, ?, ?)",
        (username, config_json, tacit_text, updated_at),
    )
