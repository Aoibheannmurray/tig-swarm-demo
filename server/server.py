import json
import asyncio
import hashlib
import logging
import random
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from models import (
    RegisterRequest, HeartbeatRequest, RenameRequest,
    IterationCreate, AdminBroadcast, AdminAuth, AdminResetChallenge,
    AdminRevoke, AdminSeedInactive, AdminSeedPool,
    MessageCreate,
    SwarmConfigUpdate,
    AgentResponse,
    IterationResponse, new_id, improvement_pct,
)
from names import generate_agent_name, load_used_names
from dedup import fingerprint
import db
import tiers
import ws_events
import api_models
import challenges
from challenges import DEFAULT_CHALLENGE

logger = logging.getLogger("swarm")


# ── Swarm-wide defaults ──
#
# Single source of truth for the integer thresholds the swarm tunes most
# often. Stored as strings in the `config` key/value table (set via the
# wizard's POST /api/swarm_config); these are the fall-throughs when a key
# is missing or unparseable. Add new tunables here so call sites stay
# consistent — never inline an `int(config.get(KEY, "N"))` again.
SWARM_DEFAULTS: dict[str, int] = {
    "inactive_minutes": 20,
    "stagnation_threshold": 2,
    "stagnation_limit": 5,
    "hypothesis_recall_threshold": 3,
}


def swarm_setting(config: dict, key: str) -> int:
    default = SWARM_DEFAULTS[key]
    raw = config.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ── Config resolution ──
#
# The server hosts every TIG challenge in parallel. Per-challenge config
# (tracks, timeout, scoring_direction, initial_algorithm_code) lives in the
# `challenge_configs` table; the singleton `config.active_challenge` row
# selects which one contributors auto-follow. Helpers below resolve a
# request's challenge and look up the right per-challenge config.
#
# `resolve_challenge` accepts an explicit value (typically from a request
# query param or body field) and falls back to the swarm's active challenge
# when none is provided.

# Cached configs — refreshed on admin config update.
_config_cache: dict | None = None
_challenge_config_cache: dict[str, dict] | None = None


async def get_config_cached() -> dict:
    global _config_cache
    if _config_cache is None:
        async with db.connect() as conn:
            _config_cache = await db.get_config(conn)
    return _config_cache


async def get_active_challenge() -> str:
    cfg = await get_config_cached()
    return cfg.get("active_challenge") or DEFAULT_CHALLENGE


async def resolve_challenge(challenge: str | None) -> str:
    """Pick the challenge a request applies to. Explicit value wins; otherwise
    fall back to the swarm's active challenge."""
    if challenge:
        return challenge
    return await get_active_challenge()


async def get_challenge_config_cached(challenge: str) -> dict:
    """Return per-challenge config (tracks, timeout, scoring_direction,
    initial_algorithm_code) with a small in-process cache. Cache is dropped
    whenever the global _config_cache is invalidated."""
    global _challenge_config_cache
    if _challenge_config_cache is None:
        _challenge_config_cache = {}
    if challenge in _challenge_config_cache:
        return _challenge_config_cache[challenge]
    async with db.connect() as conn:
        row = await db.get_challenge_config(conn, challenge)
    if row is None:
        # No row in challenge_configs yet — the wizard hasn't run for this
        # challenge. Mirror the schema/registry defaults so callers always
        # see a fully-populated dict.
        ch_def = challenges.CHALLENGES.get(challenge)
        cfg = {
            "challenge": challenge,
            "tracks": "{}",
            "timeout": ch_def.default_timeout if ch_def else 30,
            "scoring_direction": ch_def.scoring_direction if ch_def else "max",
            "initial_algorithm_code": "",
            "initial_kernel_code": "",
            "strategy_tags": "[]",
        }
    else:
        cfg = row
    _challenge_config_cache[challenge] = cfg
    return cfg


def _invalidate_caches() -> None:
    global _config_cache, _challenge_config_cache
    _config_cache = None
    _challenge_config_cache = None


async def load_initial_algorithm(challenge: str) -> tuple[str, str]:
    """Initial algorithm broadcast to every agent on a fresh trajectory for
    the given challenge: their first iteration on it, and again whenever a
    trajectory reset draws the "fresh start" slot from the per-challenge
    inactive pool. Returns (algorithm_code, kernel_code)."""
    cfg = await get_challenge_config_cached(challenge)
    return (
        cfg.get("initial_algorithm_code") or "",
        cfg.get("initial_kernel_code") or "",
    )


async def seed_for_agent(
    conn, agent_id: str, challenge: str, tier: str, role: str,
    *, direction: str, cutoff_ts: str,
) -> tuple[str, str, str]:
    """Pick the starting code for an agent on a fresh trajectory.

    Frontier explorers keep the bare stub (they bootstrap). Standard-tier OR
    exploiter agents get working code via a fallback chain:
      seed pool (diverse per-agent assignment) → best active peer → stub.

    Returns (algorithm_code, kernel_code, start) where `start` is one of
    'seed' | 'peer' | 'stub' for the dashboard.
    """
    needs_seed = (tier == "standard") or (role == "exploiter")
    if not needs_seed:
        code, kernel = await load_initial_algorithm(challenge)
        return code, kernel, "stub"

    seeds = await db.list_seeds(conn, challenge)
    if seeds:
        # Deterministic per-agent assignment spreads the population across the
        # available seeds (and is stable across this agent's resets).
        idx = int(hashlib.sha1(agent_id.encode()).hexdigest(), 16) % len(seeds)
        s = seeds[idx]
        return s["algorithm_code"], s.get("kernel_code") or "", "seed"

    # Empty seed pool → adopt the best active peer's current algorithm so the
    # agent exploits a real working lineage instead of idling on the stub.
    peers = await db.list_trajectory_bests(
        conn, challenge,
        exclude_agent_ids=[agent_id],
        direction=direction,
        active_only=True,
        inactive_cutoff=cutoff_ts,
    )
    if peers:
        best = peers[0]
        return best["algorithm_code"], best.get("kernel_code") or "", "peer"

    # True cold start: no seeds and no feasible peers yet.
    code, kernel = await load_initial_algorithm(challenge)
    return code, kernel, "stub"


async def get_direction(challenge: str | None = None) -> str:
    if challenge is None:
        challenge = await get_active_challenge()
    cfg = await get_challenge_config_cached(challenge)
    d = cfg.get("scoring_direction", "max")
    return "max" if d == "max" else "min"


def _per_challenge_tracks(cfg: dict) -> dict:
    raw = cfg.get("tracks") or "{}"
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}


def get_num_instances_for(cfg: dict, solution_data=None) -> int:
    """Authoritative count: the actual keys in the current best experiment's
    solution_data (one entry per benchmark instance). The per-challenge `tracks`
    dict is the fallback for the pre-first-experiment moment — sum the
    per-track instance counts (excluding the "seed" key)."""
    if solution_data:
        try:
            rd = json.loads(solution_data) if isinstance(solution_data, str) else solution_data
            if rd:
                return len(rd)
        except Exception:
            pass
    try:
        tracks = _per_challenge_tracks(cfg)
        total = sum(
            int(v) for k, v in tracks.items()
            if k != "seed" and isinstance(v, (int, float))
        )
        return total or 1
    except Exception:
        return 1


async def get_baseline_score(conn, challenge: str) -> float | None:
    """The baseline is the score of the very first feasible experiment
    published to the DB for this challenge. Scores are already per-instance
    averages (computed by benchmark.py), so no extra normalisation is
    needed. Returns None when nothing feasible has landed yet on this
    challenge."""
    cursor = await conn.execute(
        "SELECT score FROM experiments "
        "WHERE feasible = 1 AND challenge = ? "
        "ORDER BY created_at ASC LIMIT 1",
        (challenge,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return row["score"]


async def verify_admin(req: AdminAuth) -> None:
    config = await get_config_cached()
    expected = config.get("admin_key")
    if not expected or req.admin_key != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key")


def _derive_user_password(username: str, base_password: str) -> str:
    """Per-contributor password = sha256(username + ':' + base_password).

    The server stores only the base password (config.swarm_password); the
    host computes each contributor's derived password via
    `python setup.py invite <username>` and shares it with them out-of-band.
    Same shape `hashlib` digest used by the invite command, so the two
    must match exactly.
    """
    import hashlib
    return hashlib.sha256(f"{username}:{base_password}".encode()).hexdigest()


def _revoked_usernames(config: dict) -> set[str]:
    """Read the revoked-contributors set from config. Stored as a JSON
    array under `revoked_contributors`; absent / unparseable values are
    treated as an empty set so a bad write never locks everyone out."""
    raw = config.get("revoked_contributors")
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        return set()


async def verify_swarm_password(
    x_username: str | None = Header(default=None, alias="X-Username"),
    x_swarm_password: str | None = Header(default=None, alias="X-Swarm-Password"),
) -> str:
    """Gates /api/agents/register (the join endpoint). Returns the
    contributor's username so the handler can stamp it on the new agent.
    Subsequent writes use the per-agent token (see verify_agent_token).
    """
    if not x_username or not x_swarm_password:
        raise HTTPException(
            status_code=403,
            detail="Missing X-Username or X-Swarm-Password header",
        )
    config = await get_config_cached()
    base = config.get("swarm_password")
    if not base:
        raise HTTPException(status_code=403, detail="Swarm not configured")
    expected = _derive_user_password(x_username, base)
    if not secrets.compare_digest(x_swarm_password, expected):
        raise HTTPException(status_code=403, detail="Invalid credentials")
    if x_username in _revoked_usernames(config):
        raise HTTPException(status_code=403, detail="Contributor has been revoked")
    return x_username


async def verify_agent_token(
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
) -> str:
    """Look up the agent by token. Returns the agent_id so downstream
    handlers can use it; raises 403 if the token is missing or unknown.

    Issued at /api/agents/register and stored on the agents row. Tokens
    are not revocable today — deleting the agents row is the only way to
    invalidate one.
    """
    if not x_agent_token:
        raise HTTPException(status_code=403, detail="Missing agent token")
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT id FROM agents WHERE token = ?", (x_agent_token,),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Invalid agent token")
    return row["id"]


async def get_agent_name(conn, agent_id: str) -> str:
    cursor = await conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,))
    row = await cursor.fetchone()
    return row["name"] if row else "unknown"


# ── WebSocket manager ──

# Max time we'll wait for a single ws.send_json before considering that
# subscriber dead. Without this, asyncio.gather waits for every send to
# resolve — a single hung subscriber (network stall, paused tab on a slow
# connection) blocks broadcasts to every other dashboard. 2s is generous
# for a healthy connection but short enough that a stuck one doesn't dam
# the event stream during a busy publish burst.
_WS_SEND_TIMEOUT_S = 2.0


class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, event):
        # Every event must be a typed Pydantic model from ws_events.py;
        # the union (`WSEvent`) is the wire-level contract.
        if not self.connections:
            return
        payload = event.model_dump(mode="json")
        # Per-send timeout: a TimeoutError from wait_for is captured by
        # return_exceptions=True alongside any other send failure, so the
        # below filter prunes hung subscribers exactly the same way it
        # prunes ones that closed cleanly.
        results = await asyncio.gather(
            *(
                asyncio.wait_for(ws.send_json(payload), timeout=_WS_SEND_TIMEOUT_S)
                for ws in self.connections
            ),
            return_exceptions=True,
        )
        self.connections = [
            ws for ws, result in zip(self.connections, results)
            if not isinstance(result, BaseException)
        ]


manager = ConnectionManager()


# ── App lifecycle ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    async with db.connect() as conn:
        names = await db.get_all_agent_names(conn)
    load_used_names(names)
    task = asyncio.create_task(periodic_stats())
    yield
    task.cancel()


app = FastAPI(title="Swarm Coordination Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static dashboard mounted after all routes (see bottom of file)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inactive_cutoff() -> str:
    cfg = _config_cache or {}
    minutes = swarm_setting(cfg, "inactive_minutes")
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# ── Periodic stats ──

async def periodic_stats():
    while True:
        await asyncio.sleep(10)
        try:
            cutoff_ts = inactive_cutoff()
            active_challenge = await get_active_challenge()
            async with db.connect() as conn:
                # Free up trajectories held by agents that have gone silent
                # past the inactive cutoff. Without this sweep, the
                # stagnation-reset path in /api/iterations is the only way
                # a trajectory ever leaves `active` — so a crashed or
                # disconnected agent's trajectory would stay flagged active
                # forever, and their best algorithm would never reach the
                # inactive pool that other agents adopt from.
                await db.deactivate_inactive_agent_trajectories(
                    conn, cutoff_ts, now(),
                )
                await conn.commit()
                total_agents = await db.get_agent_count(conn, active_only=False)

                # Batched per-challenge counters. Previously this loop fired
                # 5 separate COUNT queries per challenge (active, exp, hyp,
                # traj, agents_in_challenge) — at 8 challenges that's 40
                # roundtrips every 10s. At scale (~80 agents, 4-hour test)
                # those queries also start touching more rows. Collapsing
                # into 5 grouped queries (one per category, GROUP BY
                # challenge) keeps total roundtrips constant regardless of
                # how many challenges are configured.
                cur = await conn.execute(
                    "SELECT challenge, COUNT(*) as c FROM agent_challenge_state "
                    "WHERE last_active_at >= ? GROUP BY challenge",
                    (cutoff_ts,),
                )
                active_by_ch = {r["challenge"]: r["c"] for r in await cur.fetchall()}
                cur = await conn.execute(
                    "SELECT challenge, COUNT(*) as c FROM experiments GROUP BY challenge",
                )
                exp_by_ch = {r["challenge"]: r["c"] for r in await cur.fetchall()}
                cur = await conn.execute(
                    "SELECT challenge, COUNT(*) as c FROM hypotheses GROUP BY challenge",
                )
                hyp_by_ch = {r["challenge"]: r["c"] for r in await cur.fetchall()}
                cur = await conn.execute(
                    "SELECT challenge, COUNT(*) as c FROM trajectories GROUP BY challenge",
                )
                traj_by_ch = {r["challenge"]: r["c"] for r in await cur.fetchall()}
                # Distinct-agents-who-published per challenge — same source
                # of truth as get_challenge_total_agents (experiments table).
                cur = await conn.execute(
                    "SELECT challenge, COUNT(DISTINCT agent_id) as c FROM experiments GROUP BY challenge",
                )
                agents_in_ch = {r["challenge"]: r["c"] for r in await cur.fetchall()}

                per_challenge: dict[str, dict] = {}
                for ch in challenges.CHALLENGE_NAMES:
                    direction = await get_direction(ch)
                    cfg = await get_challenge_config_cached(ch)
                    best = await db.get_global_best(conn, ch, direction=direction)
                    baseline = await get_baseline_score(conn, ch)
                    best_solution_data = best["solution_data"] if best else None
                    num_instances = get_num_instances_for(cfg, best_solution_data)
                    best_score = best["score"] if best else None
                    imp = (
                        improvement_pct(baseline, best_score, direction)
                        if baseline is not None and best_score is not None
                        else 0
                    )
                    per_challenge[ch] = {
                        "active_agents": active_by_ch.get(ch, 0),
                        "best_score": best_score,
                        "baseline_score": baseline,
                        "num_instances": num_instances,
                        "improvement_pct": imp,
                        "total_experiments": exp_by_ch.get(ch, 0),
                        "hypotheses_count": hyp_by_ch.get(ch, 0),
                        "total_trajectories": traj_by_ch.get(ch, 0),
                        "total_agents_in_challenge": agents_in_ch.get(ch, 0),
                    }

            # `per_challenge` is the source of truth; the dashboard slices
            # it down to the viewed challenge before populating panels.
            await manager.broadcast(ws_events.StatsUpdate(
                active_challenge=active_challenge,
                per_challenge={ch: ws_events._StatsPerChallenge(**v) for ch, v in per_challenge.items()},
                total_agents=total_agents,
                timestamp=now(),
            ))
        except Exception:
            logger.exception("Error in periodic_stats")


# ── Agent endpoints ──

@app.post("/api/agents/register", response_model=AgentResponse)
async def register_agent(
    req: RegisterRequest,
    contributor_username: str = Depends(verify_swarm_password),
):
    agent_id = new_id()
    agent_token = secrets.token_urlsafe(24)
    timestamp = now()
    # Honour the contributor's chosen name when supplied AND not already
    # taken; fall back to the server's auto-generator otherwise. We can't
    # rely on the UNIQUE constraint to handle collisions because we want to
    # transparently degrade to a generated name rather than 409 the wizard.
    agent_name: str | None = None
    requested = (req.agent_name or "").strip()
    if requested:
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM agents WHERE name = ?", (requested,),
            )
            taken = await cur.fetchone()
        if not taken:
            agent_name = requested
    if agent_name is None:
        agent_name = generate_agent_name()
    llm_type = (req.llm_type or "").strip() or None
    # Auto-classify the model tier (frontier/standard) at register. Prefer the
    # structured provider/model when supplied; otherwise parse the llm_type
    # label the client already sends. Tier drives seeding only.
    if req.provider or req.model:
        tier = tiers.classify_tier(req.provider, req.model)
    else:
        tier = tiers.classify_tier_from_label(llm_type)

    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, status, llm_type, token, contributor_username, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, agent_name, timestamp, timestamp, "idle", llm_type, agent_token, contributor_username, tier),
        )
        config = await db.get_config(conn)
        # Persist a join event so the dashboard's live feed can replay it
        # on reload via /api/messages. The `challenge` column is NOT NULL,
        # so we record the active challenge at join time; clients querying
        # /api/messages get agent_joined rows back regardless of which
        # challenge they ask about (see list_messages).
        active_challenge = config.get("active_challenge") or DEFAULT_CHALLENGE
        await conn.execute(
            "INSERT INTO messages (id, agent_id, challenge, agent_name, content, msg_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id(), agent_id, active_challenge, agent_name,
             "joined the swarm", "agent_joined", timestamp),
        )
        await conn.commit()

    await manager.broadcast(ws_events.AgentJoined(
        agent_id=agent_id,
        agent_name=agent_name,
        timestamp=timestamp,
    ))

    active_challenge = config.get("active_challenge") or DEFAULT_CHALLENGE

    # `active_challenge` is the swarm-wide challenge the contributor should
    # auto-follow (set by the owner via `setup.py switch`). Per-track counts
    # / timeout live in /api/swarm_config — the agent polls that on every
    # iteration.
    swarm_type = config.get("swarm_type", "cpu")
    available = [
        ch for ch in challenges.CHALLENGE_NAMES
        if challenges.CHALLENGES[ch].is_gpu == (swarm_type == "gpu")
    ]

    return AgentResponse(
        agent_id=agent_id,
        agent_name=agent_name,
        agent_token=agent_token,
        registered_at=timestamp,
        config={
            "heartbeat_interval_seconds": 30,
            "active_challenge": active_challenge,
            "swarm_type": swarm_type,
            "available_challenges": available,
        },
    )


@app.post("/api/agents/{agent_id}/rename", dependencies=[Depends(verify_agent_token)])
async def rename_agent(agent_id: str, req: RenameRequest):
    """Update an existing agent's display name. `agents.name` is the
    single source of truth for an agent's name — leaderboard, messages
    GET, and every event broadcast resolve through it — so this is the
    only operation that affects what the dashboard shows for `agent_id`.

    Returns 404 if the agent doesn't exist, 409 if `agent_name` collides
    with another agent, 400 if blank. Idempotent when the new name
    equals the current one (no broadcast in that case)."""
    requested = (req.agent_name or "").strip()
    if not requested:
        raise HTTPException(status_code=400, detail="agent_name must be non-empty")

    timestamp = now()
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT name FROM agents WHERE id = ?", (agent_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        old_name = row["name"]
        if old_name == requested:
            return {"agent_id": agent_id, "agent_name": requested}

        cur = await conn.execute(
            "SELECT id FROM agents WHERE name = ? AND id != ?",
            (requested, agent_id),
        )
        if await cur.fetchone() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"agent_name {requested!r} is already taken",
            )

        await conn.execute(
            "UPDATE agents SET name = ? WHERE id = ?",
            (requested, agent_id),
        )
        await conn.commit()

    await manager.broadcast(ws_events.AgentRenamed(
        agent_id=agent_id,
        old_name=old_name,
        new_name=requested,
        timestamp=timestamp,
    ))
    return {"agent_id": agent_id, "agent_name": requested}


@app.post("/api/agents/{agent_id}/heartbeat", dependencies=[Depends(verify_agent_token)])
async def heartbeat(agent_id: str, req: HeartbeatRequest):
    timestamp = now()
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE agents SET last_heartbeat = ?, status = ? WHERE id = ?",
            (timestamp, req.status, agent_id),
        )
        # Also bump `last_active_at` on the agent's current challenge state
        # row. Without this, a long benchmark (multi-minute c3/GPU run) keeps
        # `last_heartbeat` fresh but `last_active_at` (only updated by
        # /api/state) goes stale — and periodic_stats's
        # `deactivate_inactive_agent_trajectories` then reaps an actively-
        # working agent's trajectory and clears their trajectory_bests row.
        #
        # "Current" challenge = the row with the most recent last_active_at
        # for this agent (i.e. whichever challenge their last /api/state was
        # for). If the agent has no acs rows yet (just registered, hasn't
        # fetched state), this is a no-op — fine, there's no trajectory to
        # keep alive at that stage.
        await conn.execute(
            "UPDATE agent_challenge_state SET last_active_at = ? "
            "WHERE agent_id = ? AND challenge = ("
            "  SELECT challenge FROM agent_challenge_state "
            "  WHERE agent_id = ? ORDER BY last_active_at DESC LIMIT 1"
            ")",
            (timestamp, agent_id, agent_id),
        )
        await conn.commit()
    return {"ack": True, "server_time": timestamp}


# ── State endpoint ──


@app.get("/api/state")
async def get_state(
    agent_id: str | None = None,
    challenge: str | None = None,
    role: str | None = None,
):
    """Return current swarm state for the given challenge.

    When `agent_id` is supplied, the agent receives its own current best
    code for the requested challenge (or the per-challenge initial seed on
    first run). When stagnating past the `hypothesis_recall_threshold`,
    prior failed hypotheses for the current program are included with a
    directive to try something different. When stagnating past
    `stagnation_threshold`, a stagnation_hint field (50/50 "tacit_knowledge"
    or "inspiration") and inspiration_code are included — both filtered by
    the same challenge so per-challenge state stays disjoint. For GPU
    challenges, kernel code fields are included; for CPU challenges they
    are omitted.

    When `agent_id` is omitted, returns a global dashboard view (filtered
    by the requested or active challenge).

    `challenge` defaults to the swarm's `active_challenge` when omitted.
    """
    config = await get_config_cached()
    challenge = await resolve_challenge(challenge)
    direction = await get_direction(challenge)
    challenge_cfg = await get_challenge_config_cached(challenge)

    async with db.connect() as conn:
        global_best = await db.get_global_best(conn, challenge, direction=direction)
        baseline = await get_baseline_score(conn, challenge)
        cutoff_ts = inactive_cutoff()
        # active = agents recently active on THIS challenge
        cur = await conn.execute(
            "SELECT COUNT(*) as c FROM agent_challenge_state "
            "WHERE challenge = ? AND last_active_at >= ?",
            (challenge, cutoff_ts),
        )
        active = (await cur.fetchone())["c"]
        total_agents = await db.get_agent_count(conn, active_only=False)
        total_agents_in_challenge = await db.get_challenge_total_agents(
            conn, challenge,
        )
        cur = await conn.execute(
            "SELECT COUNT(*) as c FROM experiments WHERE challenge = ?",
            (challenge,),
        )
        total_exp = (await cur.fetchone())["c"]
        cur = await conn.execute(
            "SELECT COUNT(*) as c FROM hypotheses WHERE challenge = ?",
            (challenge,),
        )
        total_hyp = (await cur.fetchone())["c"]
        cur = await conn.execute(
            "SELECT COUNT(*) as c FROM trajectories WHERE challenge = ?",
            (challenge,),
        )
        total_traj = (await cur.fetchone())["c"]

        # ── Agent-specific view ──
        if agent_id is not None:
            ts_now = now()
            # Touch BOTH the global heartbeat AND the per-challenge
            # last_active_at so leaderboards and inspiration filters see
            # this agent as "currently working on `challenge`".
            await conn.execute(
                "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
                (ts_now, agent_id),
            )
            await db.ensure_agent_challenge_state(conn, agent_id, challenge, ts_now)
            await conn.commit()

            traj_best = await db.get_trajectory_best(conn, agent_id, challenge)
            acs = await db.get_agent_challenge_state(conn, agent_id, challenge)
            runs_since = acs["runs_since_improvement"] if acs else 0
            agent_tier = await db.get_agent_tier(conn, agent_id)
            # Role is contributor-owned, reported by the client each poll and
            # not persisted as authority. Normalize to the two known values;
            # anything unrecognized (or absent) is an explorer — today's
            # default behavior.
            agent_role = "exploiter" if (role or "").strip().lower() == "exploiter" else "explorer"

            # ── Trajectory reset on stagnation_limit ──
            trajectory_reset = None
            # How this iteration's starting code was chosen ('seed' | 'peer' |
            # 'stub'), whenever it came from seed_for_agent — on a fresh reset
            # OR a true cold start. Surfaced in state so the client can log
            # whether a standard-tier agent actually got a seed vs. the bare
            # stub. None when the agent continued its own existing best.
            seed_start = None
            stagnation_limit = swarm_setting(config, "stagnation_limit")
            if stagnation_limit > 0 and runs_since >= stagnation_limit and traj_best is not None:
                timestamp = now()
                # Deactivate the current trajectory.
                cur_traj_id = acs["current_trajectory_id"] if acs else None
                old_program_id = acs["current_program_id"] if acs else None
                if cur_traj_id:
                    await db.deactivate_trajectory(conn, cur_traj_id, timestamp)

                # Pick from the per-challenge inactive pool BEFORE depositing,
                # so the agent can't re-adopt its own just-deposited code.
                # CORRECTNESS INVARIANT: pick must be filtered by challenge —
                # otherwise a stagnating VRP agent could be handed SAT code.
                inactive_pool = await db.get_inactive_with_deactivations(conn, challenge)
                new_traj_id = None
                new_program_id = None

                # Fresh start if N^1.5 < D (number of trajectories^1.5 <
                # total deactivations). At equilibrium N^1.5 ≈ D, so the
                # trajectory count grows as total_work^(2/3) and mean
                # trajectory length (D/N) grows as total_work^(1/3) —
                # favors more diverse trajectories with shorter lives than
                # the prior N² rule.
                n_traj, total_deact = await db.trajectory_counts(conn, challenge)
                go_fresh = not inactive_pool or n_traj ** 1.5 < total_deact

                # Carries the adopted trajectory's peak score (None on a fresh
                # start) so we can seed it as the agent's personal best below.
                adopted_score = None
                if go_fresh:
                    new_code, new_kernel_code, _start = await seed_for_agent(
                        conn, agent_id, challenge, agent_tier, agent_role,
                        direction=direction, cutoff_ts=cutoff_ts,
                    )
                    new_program_id = new_id()
                    trajectory_reset = {"type": "fresh_start", "start": _start}
                    seed_start = _start
                else:
                    picked = random.choice(inactive_pool)
                    new_code = picked["algorithm_code"]
                    new_kernel_code = picked.get("kernel_code")
                    new_program_id = picked.get("program_id") or new_id()
                    adopted_score = picked.get("score")
                    await db.remove_inactive(conn, picked["id"])
                    trajectory_reset = {
                        "type": "adopted_inactive",
                        "prior_score": adopted_score,
                    }
                    if picked.get("trajectory_id"):
                        new_traj_id = picked["trajectory_id"]
                        await db.reactivate_trajectory(conn, new_traj_id)
                        await db.increment_trajectory_agents(conn, new_traj_id)

                # Now deposit the stagnated code into the per-challenge pool.
                await db.deposit_inactive(
                    conn, agent_id, challenge,
                    traj_best["algorithm_code"], traj_best["score"], timestamp,
                    trajectory_id=cur_traj_id, program_id=old_program_id,
                    kernel_code=traj_best.get("kernel_code"),
                )

                # Seed the agent's personal best with the adopted trajectory's
                # peak so it builds UP from that best instead of from zero.
                # Without this the floor is gone: the agent's first (possibly
                # worse) result becomes its new best and the trajectory drifts
                # below the peak it was handed. It also kept the adopted code
                # alive only until the next publish — if that publish failed,
                # the next state fell back to the stub (see the `else` branch
                # below). Fresh starts, and pool entries with no score, keep the
                # original clear-to-empty behaviour.
                if adopted_score is not None:
                    adopted_experiment_id = new_id()
                    await db.upsert_trajectory_best(
                        conn, agent_id=agent_id, challenge=challenge,
                        experiment_id=adopted_experiment_id,
                        algorithm_code=new_code, score=adopted_score,
                        feasible=True, challenge_metrics=None,
                        solution_data=None, updated_at=timestamp,
                        trajectory_id=new_traj_id, kernel_code=new_kernel_code,
                    )
                    traj_best = {
                        "algorithm_code": new_code,
                        "score": adopted_score,
                        "experiment_id": adopted_experiment_id,
                        "kernel_code": new_kernel_code,
                    }
                    current_trajectory_best = adopted_score
                    traj_best_experiment_id = adopted_experiment_id
                else:
                    await db.clear_trajectory_best(conn, agent_id, challenge)
                    traj_best = None
                    current_trajectory_best = None
                    traj_best_experiment_id = None
                await db.update_agent_challenge_state(
                    conn, agent_id, challenge,
                    set_fields={
                        "runs_since_improvement": 0,
                        "current_trajectory_id": new_traj_id,
                        "current_program_id": new_program_id,
                    },
                )
                await conn.commit()
                traj_best_code = new_code
                traj_best_kernel_code = new_kernel_code
                runs_since = 0
                agent_name = await get_agent_name(conn, agent_id)
                await manager.broadcast(ws_events.TrajectoryReset(
                    challenge=challenge,
                    agent_name=agent_name,
                    agent_id=agent_id,
                    reset_type=trajectory_reset["type"],
                    timestamp=timestamp,
                ))
                # Re-read acs so subsequent reads see the reset state.
                acs = await db.get_agent_challenge_state(conn, agent_id, challenge)
            else:
                if traj_best:
                    traj_best_code = traj_best["algorithm_code"]
                    traj_best_kernel_code = traj_best.get("kernel_code")
                else:
                    traj_best_code, traj_best_kernel_code, _start = await seed_for_agent(
                        conn, agent_id, challenge, agent_tier, agent_role,
                        direction=direction, cutoff_ts=cutoff_ts,
                    )
                    seed_start = _start
                current_trajectory_best = traj_best["score"] if traj_best else None
                traj_best_experiment_id = traj_best["experiment_id"] if traj_best else None

            # ── Program ID management (per-(agent, challenge)) ──
            current_program_id = (acs or {}).get("current_program_id") if acs else None
            if not current_program_id:
                current_program_id = new_id()
                await db.update_agent_challenge_state(
                    conn, agent_id, challenge,
                    set_fields={"current_program_id": current_program_id},
                )
                await conn.commit()

            # ── Prior hypotheses (program-scoped, shown only after threshold) ──
            hypothesis_recall_threshold = swarm_setting(config, "hypothesis_recall_threshold")
            prior_hypotheses: list[dict] = []
            hypothesis_recall_message: str | None = None
            if runs_since >= hypothesis_recall_threshold:
                cursor = await conn.execute(
                    """SELECT h.title, h.strategy_tag, h.description, e.score
                       FROM hypotheses h
                       LEFT JOIN experiments e ON e.hypothesis_id = h.id
                       WHERE h.program_id = ? AND h.challenge = ? AND h.status = 'failed'
                       ORDER BY h.created_at DESC LIMIT 20""",
                    (current_program_id, challenge),
                )
                prior_hypotheses = [dict(row) for row in await cursor.fetchall()]
                if prior_hypotheses:
                    hypothesis_recall_message = (
                        "The following strategies were tried on this program and "
                        "did not improve the score. Try something structurally "
                        "different from these approaches."
                    )

            # Inspiration on stagnation (only when not trajectory-resetting).
            # CORRECTNESS INVARIANT: only pull inspiration from agents
            # currently active on THIS challenge — not from agents whose
            # global heartbeat is recent but whose last_active_at on this
            # challenge is stale.
            inspiration_code = None
            inspiration_kernel_code = None
            inspiration_agent_name = None
            stagnation_hint = None
            n_stagnation = swarm_setting(config, "stagnation_threshold")
            if trajectory_reset is None and runs_since >= n_stagnation:
                stagnation_hint = random.choice(["tacit_knowledge", "inspiration"])
                if stagnation_hint == "tacit_knowledge":
                    await db.increment_agent_challenge_counters(
                        conn, agent_id, challenge,
                        tacit_knowledge_inc=1,
                        runs_since_improvement_inc=0,
                    )
                else:
                    await db.increment_agent_challenge_counters(
                        conn, agent_id, challenge,
                        inspiration_inc=1,
                        runs_since_improvement_inc=0,
                    )
                all_bests = await db.list_trajectory_bests(
                    conn, challenge,
                    exclude_agent_ids=[agent_id],
                    direction=direction,
                    active_only=True,
                    inactive_cutoff=cutoff_ts,
                )
                pending_source = None
                if all_bests:
                    chosen = random.choice(all_bests)
                    inspiration_code = chosen["algorithm_code"]
                    inspiration_kernel_code = chosen.get("kernel_code")
                    inspiration_agent_name = await get_agent_name(
                        conn, chosen["agent_id"]
                    )
                    if stagnation_hint == "inspiration":
                        pending_source = chosen["agent_id"]
                # Stash the hint (and inspiration source) so the next
                # iteration this agent publishes can be tagged with them.
                # /api/iterations reads + clears both atomically.
                await db.update_agent_challenge_state(
                    conn, agent_id, challenge,
                    set_fields={
                        "pending_hint": stagnation_hint,
                        "pending_inspiration_source": pending_source,
                    },
                )
                await conn.commit()

            best_solution_data = traj_best["solution_data"] if traj_best else None
            num_instances = get_num_instances_for(challenge_cfg, best_solution_data)
            leaderboard = await db.compute_leaderboard(
                conn, challenge, inactive_cutoff(), direction=direction,
            )
            global_best_score = global_best["score"] if global_best else None

            ch_def = challenges.CHALLENGES.get(challenge)
            is_gpu = ch_def.is_gpu if ch_def else False

            # Soft niching: suggest the least-covered strategy family to
            # explorers (a hint the client nudges toward; the agent may ignore
            # it). Exploiters make localized edits and don't pick a family.
            assigned_strategy_tag = None
            if agent_role == "explorer" and ch_def:
                assigned_strategy_tag = await db.least_covered_tag(
                    conn, challenge, list(ch_def.strategy_tags),
                )

            # Server's view of this agent's name — used by the loop client
            # to detect a local rename (swarm.config.json contributor_name
            # diverging from server's agents.name) and POST /rename.
            self_agent_name = await get_agent_name(conn, agent_id)
            resp = {
                "challenge": challenge,
                "is_gpu": is_gpu,
                "agent_name": self_agent_name,
                "best_score": global_best_score,
                "best_algorithm_code": traj_best_code,
                "best_experiment_id": traj_best_experiment_id,
                "current_trajectory_best": current_trajectory_best,
                "my_runs": (acs or {}).get("experiments_completed") if acs else 0,
                "my_improvements": (acs or {}).get("improvements") if acs else 0,
                "my_runs_since_improvement": runs_since,
                "num_instances": num_instances,
                "active_agents": active,
                "total_agents": total_agents,
                "total_experiments": total_exp,
                "hypotheses_count": total_hyp,
                "prior_hypotheses": prior_hypotheses,
                "hypothesis_recall_message": hypothesis_recall_message,
                "inspiration_code": inspiration_code,
                "inspiration_agent_name": inspiration_agent_name,
                "stagnation_hint": stagnation_hint,
                "trajectory_reset": trajectory_reset,
                "seed_start": seed_start,
                "leaderboard": leaderboard,
                "tier": agent_tier,
                "role": agent_role,
                "assigned_strategy_tag": assigned_strategy_tag,
            }
            if is_gpu:
                resp["best_kernel_code"] = traj_best_kernel_code or None
                resp["inspiration_kernel_code"] = inspiration_kernel_code or None
            return resp

        # ── Dashboard view (no agent_id) ──
        cursor = await conn.execute(
            """SELECT e.*, a.name as agent_name,
                       EXISTS(SELECT 1 FROM best_history bh
                              WHERE bh.experiment_id = e.id) as is_new_best
                FROM experiments e JOIN agents a ON a.id = e.agent_id
                WHERE e.challenge = ?
                ORDER BY e.created_at DESC LIMIT 20""",
            (challenge,),
        )
        recent_experiments = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """SELECT h.id, h.title, h.strategy_tag, h.description,
                      a.name as agent_name, h.agent_id, h.parent_hypothesis_id,
                      h.created_at
               FROM hypotheses h JOIN agents a ON a.id = h.agent_id
               WHERE h.challenge = ?
               ORDER BY h.created_at DESC LIMIT 30""",
            (challenge,),
        )
        recent_hypotheses = [dict(row) for row in await cursor.fetchall()]

        served = global_best
        best_solution_data = served["solution_data"] if served else None
        num_instances = get_num_instances_for(challenge_cfg, best_solution_data)
        leaderboard = await db.compute_leaderboard(
            conn, challenge, inactive_cutoff(), direction=direction,
        )

    global_best_score = global_best["score"] if global_best else None
    overall_imp = (
        improvement_pct(baseline, global_best_score, direction)
        if baseline is not None and global_best_score is not None
        else 0
    )

    _initial_algo = (None, None) if served else await load_initial_algorithm(challenge)
    return {
        "challenge": challenge,
        "baseline_score": baseline,
        "best_score": global_best_score,
        "improvement_pct": overall_imp,
        "best_algorithm_code": served["algorithm_code"] if served else _initial_algo[0],
        "best_kernel_code": (served.get("kernel_code") if served else _initial_algo[1]) or None,
        "best_experiment_id": served["id"] if served else None,
        "best_solution_data": json.loads(served["solution_data"]) if served and served["solution_data"] else None,
        "best_track_scores": (
            json.loads(served["track_scores"])
            if served and served.get("track_scores")
            else None
        ),
        "num_instances": num_instances,
        "active_agents": active,
        "total_agents": total_agents,
        "total_agents_in_challenge": total_agents_in_challenge,
        "total_experiments": total_exp,
        "hypotheses_count": total_hyp,
        "total_trajectories": total_traj,
        "recent_experiments": [
            {
                "id": e["id"],
                # Include agent_id so the dashboard can resolve each backfilled
                # experiment to the agent's palette color (getAgentColor is
                # keyed on agent_id). Without this, backfilled experiments
                # render with the event-type fallback color while live ones
                # use the agent's color — same agent, two colors.
                "agent_id": e["agent_id"],
                "agent_name": e["agent_name"],
                "score": e["score"],
                "feasible": bool(e["feasible"]),
                "is_new_best": bool(e["is_new_best"]),
                "improvement_pct": (
                    improvement_pct(baseline, e["score"], direction)
                    if baseline is not None
                    else 0
                ),
                "delta_vs_best_pct": e.get("delta_vs_best_pct"),
                "delta_vs_trajectory_best_pct": e.get("delta_vs_trajectory_best_pct"),
                "beats_trajectory_best": bool(e.get("beats_trajectory_best")),
                "created_at": e["created_at"],
                "notes": e["notes"],
            }
            for e in recent_experiments
        ],
        "recent_hypotheses": [
            {"id": h["id"], "title": h["title"], "strategy_tag": h["strategy_tag"],
             "agent_name": h["agent_name"], "description": h["description"],
             "parent_hypothesis_id": h.get("parent_hypothesis_id"),
             "agent_id": h.get("agent_id", ""),
             "created_at": h.get("created_at")}
            for h in recent_hypotheses
        ],
        "leaderboard": leaderboard,
    }


# ── Iteration endpoint (unified hypothesis + experiment) ──

@app.post("/api/iterations", response_model=IterationResponse, dependencies=[Depends(verify_agent_token)])
async def create_iteration(req: IterationCreate):
    challenge = await resolve_challenge(req.challenge)
    direction = await get_direction(challenge)
    challenge_cfg = await get_challenge_config_cached(challenge)
    exp_id = new_id()
    hyp_id = new_id()
    timestamp = now()
    solution_data_json = json.dumps(req.solution_data) if req.solution_data else None
    track_scores_json = json.dumps(req.track_scores) if req.track_scores else None
    challenge_metrics_json = (
        json.dumps(req.challenge_metrics) if req.challenge_metrics else None
    )
    fp = fingerprint(req.title, req.strategy_tag)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")

        # SQLite FKs aren't enforced (no PRAGMA), so /api/iterations would
        # otherwise accept any client-supplied agent_id and silently create
        # orphan rows in experiments/hypotheses/trajectory_bests/best_history.
        # Those rows then get dropped from the dashboard's INNER JOINs on
        # agents — leaderboard/recent_experiments go blank even though the
        # data is "there." Reject up front instead.
        cursor = await conn.execute(
            "SELECT 1 FROM agents WHERE id = ?", (req.agent_id,)
        )
        if await cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Agent {req.agent_id} is not registered. "
                    "Call POST /api/agents/register first."
                ),
            )

        await db.ensure_agent_challenge_state(conn, req.agent_id, challenge, timestamp)

        prev_best = await db.get_global_best(conn, challenge, direction=direction)
        prev_trajectory_best = await db.get_trajectory_best(conn, req.agent_id, challenge)
        baseline = await get_baseline_score(conn, challenge)

        is_new_best = prev_best is None or db.is_better(direction, req.score, prev_best["score"])
        beats_trajectory_best = (
            prev_trajectory_best is None
            or db.is_better(direction, req.score, prev_trajectory_best["score"])
        )

        target_best_experiment_id = (
            prev_trajectory_best["experiment_id"] if prev_trajectory_best else None
        )
        hyp_status = "succeeded" if beats_trajectory_best else "failed"

        # ── Program ID: tag hypothesis with current program (per-(agent, challenge)) ──
        acs = await db.get_agent_challenge_state(conn, req.agent_id, challenge)
        current_program_id = (acs or {}).get("current_program_id")
        if not current_program_id:
            current_program_id = new_id()
            await db.update_agent_challenge_state(
                conn, req.agent_id, challenge,
                set_fields={"current_program_id": current_program_id},
            )

        await conn.execute(
            """INSERT INTO hypotheses
               (id, agent_id, challenge, title, description, strategy_tag, status,
                fingerprint, target_best_experiment_id, program_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hyp_id, req.agent_id, challenge, req.title, req.description,
             req.strategy_tag, hyp_status, fp, target_best_experiment_id,
             current_program_id, timestamp),
        )

        delta_vs_best_pct: float | None = None
        if prev_best is not None and prev_best["score"] != 0:
            delta_vs_best_pct = round(
                improvement_pct(prev_best["score"], req.score, direction), 6
            )
        delta_vs_trajectory_best_pct: float | None = None
        if prev_trajectory_best is not None and prev_trajectory_best["score"] != 0:
            delta_vs_trajectory_best_pct = round(
                improvement_pct(prev_trajectory_best["score"], req.score, direction), 6
            )

        # ── Trajectory tracking (per-(agent, challenge)) ──
        trajectory_id = (acs or {}).get("current_trajectory_id")
        if not trajectory_id:
            trajectory_id = new_id()
            await db.create_trajectory(
                conn, trajectory_id, challenge, timestamp,
                current_score=req.score if beats_trajectory_best else None,
            )
            await db.update_agent_challenge_state(
                conn, req.agent_id, challenge,
                set_fields={"current_trajectory_id": trajectory_id},
            )
            await db.increment_agent_challenge_counters(
                conn, req.agent_id, challenge, num_trajectories_inc=1,
            )

        # Hint that drove this iteration (set on the prior /api/state call
        # when the agent was stagnating). We read + clear atomically so the
        # next iteration only carries a hint if the server hands one out
        # again.
        received_hint = (acs or {}).get("pending_hint")
        inspiration_source_id = (acs or {}).get("pending_inspiration_source")

        iter_input_tokens = req.input_tokens or 0
        iter_output_tokens = req.output_tokens or 0
        iter_estimated_cost = req.estimated_cost or 0.0

        await conn.execute(
            """INSERT INTO experiments
               (id, agent_id, challenge, hypothesis_id, algorithm_code, kernel_code,
                score, feasible,
                challenge_metrics, notes, solution_data, track_scores,
                delta_vs_best_pct, delta_vs_trajectory_best_pct, beats_trajectory_best,
                trajectory_id, received_hint, inspiration_source_id,
                input_tokens, output_tokens, estimated_cost, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exp_id, req.agent_id, challenge, hyp_id, req.algorithm_code, req.kernel_code,
             req.score,
             1 if req.feasible else 0, challenge_metrics_json,
             req.notes, solution_data_json, track_scores_json,
             delta_vs_best_pct, delta_vs_trajectory_best_pct,
             1 if beats_trajectory_best else 0,
             trajectory_id, received_hint, inspiration_source_id,
             iter_input_tokens, iter_output_tokens, iter_estimated_cost, timestamp),
        )

        if received_hint is not None:
            await db.update_agent_challenge_state(
                conn, req.agent_id, challenge,
                set_fields={
                    "pending_hint": None,
                    "pending_inspiration_source": None,
                },
            )

        await db.update_trajectory_after_edit(
            conn, trajectory_id, beats_trajectory_best,
            new_score=req.score if beats_trajectory_best else None,
        )

        # Personal best-ever is a record of what THIS agent actually achieved
        # with a mutation it made — it is independent of the trajectory floor.
        # It must update on every feasible publish (even one that scores below
        # an inherited trajectory best), and must never be raised by an
        # infeasible run or by the adopted floor an agent is handed on pickup.
        # increment_agent_challenge_counters applies a monotonic CASE guard, so
        # passing the score on every feasible run only ever ratchets it up.
        personal_best_candidate = req.score if req.feasible else None

        if beats_trajectory_best:
            new_program_id = new_id()
            await db.increment_agent_challenge_counters(
                conn, req.agent_id, challenge,
                runs=1,
                improvements=1,
                runs_since_improvement_reset=True,
                best_ever_score=personal_best_candidate,
                direction=direction,
                input_tokens=iter_input_tokens,
                output_tokens=iter_output_tokens,
                estimated_cost=iter_estimated_cost,
            )
            await db.update_agent_challenge_state(
                conn, req.agent_id, challenge,
                set_fields={"current_program_id": new_program_id},
            )
            await db.upsert_trajectory_best(
                conn, agent_id=req.agent_id, challenge=challenge,
                experiment_id=exp_id,
                algorithm_code=req.algorithm_code, score=req.score,
                feasible=req.feasible,
                challenge_metrics=challenge_metrics_json,
                solution_data=solution_data_json,
                updated_at=timestamp, trajectory_id=trajectory_id,
                track_scores=track_scores_json,
                kernel_code=req.kernel_code,
            )
        else:
            await db.increment_agent_challenge_counters(
                conn, req.agent_id, challenge,
                runs=1,
                runs_since_improvement_inc=1,
                best_ever_score=personal_best_candidate,
                direction=direction,
                input_tokens=iter_input_tokens,
                output_tokens=iter_output_tokens,
                estimated_cost=iter_estimated_cost,
            )

        agent_name = await get_agent_name(conn, req.agent_id)
        incremental_pct = delta_vs_best_pct if is_new_best else None

        if is_new_best:
            await conn.execute(
                """INSERT INTO best_history
                   (experiment_id, agent_id, challenge, agent_name, score, solution_data, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (exp_id, req.agent_id, challenge, agent_name, req.score, solution_data_json, timestamp),
            )

        # This agent got a result through the benchmark (feasible or not), so
        # it has "ever benchmarked" on this challenge. Agents that never reach
        # here keep ever_benchmarked=0 — a quiet signal, no feed message.
        await db.update_agent_challenge_state(
            conn, req.agent_id, challenge, set_fields={"ever_benchmarked": 1},
        )

        # Auto-harvest: a frontier agent's first feasible algorithm for a
        # strategy_tag becomes a seed for standard/exploiter agents. The
        # UNIQUE(challenge, strategy_tag, source) index + INSERT OR IGNORE make
        # first-feasible-per-tag win and bound the pool (≤ one seed per tag).
        if req.feasible and req.algorithm_code.strip():
            if (await db.get_agent_tier(conn, req.agent_id)) == "frontier":
                await db.insert_seed(
                    conn, challenge, req.strategy_tag, req.algorithm_code,
                    created_at=timestamp, source="harvested", score=req.score,
                    feasible=True, kernel_code=req.kernel_code,
                    origin_agent_id=req.agent_id,
                )

        await conn.commit()

        # Pull updated counters from the per-challenge row.
        acs = await db.get_agent_challenge_state(conn, req.agent_id, challenge)
        agent_info = {
            "experiments_completed": acs["experiments_completed"] if acs else 0,
            "improvements": acs["improvements"] if acs else 0,
            "runs_since_improvement": acs["runs_since_improvement"] if acs else 0,
        }
        leaderboard = await db.compute_leaderboard(
            conn, challenge, inactive_cutoff(), direction=direction,
        )
        rank = next(
            (e["rank"] for e in leaderboard if e["agent_id"] == req.agent_id),
            0,
        )

    effective_solution_data = req.solution_data or (
        prev_best["solution_data"] if prev_best else None
    )
    num_instances = get_num_instances_for(challenge_cfg, effective_solution_data)
    imp = improvement_pct(baseline, req.score, direction) if baseline is not None else 0.0

    await manager.broadcast(ws_events.ExperimentPublished(
        challenge=challenge,
        experiment_id=exp_id,
        agent_name=agent_name,
        agent_id=req.agent_id,
        score=req.score,
        feasible=req.feasible,
        improvement_pct=imp,
        delta_vs_best_pct=delta_vs_best_pct,
        beats_trajectory_best=beats_trajectory_best,
        delta_vs_trajectory_best_pct=delta_vs_trajectory_best_pct,
        num_instances=num_instances,
        is_new_best=is_new_best,
        hypothesis_id=hyp_id,
        strategy_tag=req.strategy_tag,
        title=req.title,
        notes=req.notes or "",
        track_scores=req.track_scores,
        timestamp=timestamp,
    ))

    if is_new_best:
        await manager.broadcast(ws_events.NewGlobalBest(
            challenge=challenge,
            experiment_id=exp_id,
            agent_name=agent_name,
            agent_id=req.agent_id,
            score=req.score,
            improvement_pct=imp,
            incremental_improvement_pct=incremental_pct,
            num_instances=num_instances,
            solution_data=req.solution_data,
            track_scores=req.track_scores,
            timestamp=timestamp,
        ))

    await manager.broadcast(ws_events.LeaderboardUpdate(
        challenge=challenge,
        entries=leaderboard,
        timestamp=timestamp,
    ))

    return IterationResponse(
        experiment_id=exp_id,
        hypothesis_id=hyp_id,
        is_new_best=is_new_best,
        beats_trajectory_best=beats_trajectory_best,
        rank=rank,
        runs=agent_info["experiments_completed"],
        improvements=agent_info["improvements"],
        runs_since_improvement=agent_info["runs_since_improvement"],
    )


# ── Leaderboard ──

@app.get("/api/leaderboard")
async def get_leaderboard(challenge: str | None = None):
    challenge = await resolve_challenge(challenge)
    direction = await get_direction(challenge)
    async with db.connect() as conn:
        leaderboard = await db.compute_leaderboard(
            conn, challenge, inactive_cutoff(), direction=direction,
        )
    return {"challenge": challenge, "updated_at": now(), "entries": leaderboard}


# ── Messages (chat feed) ──

@app.post("/api/messages", dependencies=[Depends(verify_agent_token)])
async def create_message(req: MessageCreate):
    challenge = await resolve_challenge(req.challenge)
    msg_id = new_id()
    timestamp = now()
    async with db.connect() as conn:
        # Same reasoning as /api/iterations — without this check the chat
        # feed can attribute messages to an agent_id that the leaderboard
        # has no row for, making the dashboard look inconsistent.
        cursor = await conn.execute(
            "SELECT name FROM agents WHERE id = ?", (req.agent_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Agent {req.agent_id} is not registered. "
                    "Call POST /api/agents/register first."
                ),
            )
        # `req.agent_name` is intentionally ignored — `agents.name` is the
        # single source of truth. Clients that want to change the display
        # name must POST /api/agents/{id}/rename first.
        agent_name = row["name"]
        await conn.execute(
            "INSERT INTO messages (id, agent_id, challenge, agent_name, content, msg_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, req.agent_id, challenge, agent_name, req.content, req.msg_type, timestamp),
        )
        await conn.commit()

    await manager.broadcast(ws_events.ChatMessage(
        challenge=challenge,
        message_id=msg_id,
        agent_name=agent_name,
        agent_id=req.agent_id,
        content=req.content,
        msg_type=req.msg_type,
        timestamp=timestamp,
    ))

    return {"message_id": msg_id, "timestamp": timestamp, "challenge": challenge}


@app.get("/api/messages")
async def list_messages(
    limit: int = 50, challenge: str | None = None, before: str | None = None,
):
    """Chat messages for the requested challenge, plus agent_joined events
    regardless of challenge (joins are swarm-wide). `agent_name` is JOINed
    from `agents` so retired snapshot data in `messages.agent_name` is
    never returned — current name only.

    `before` is an optional `created_at` cursor (ISO string): when set, only
    messages strictly older than it are returned. The dashboard feed uses
    this to page backwards ("load older") through history that has scrolled
    off its in-memory buffer."""
    challenge = await resolve_challenge(challenge)
    limit = max(1, min(limit, 200))
    where = "(m.challenge = ? OR m.msg_type = 'agent_joined')"
    params: list = [challenge]
    if before:
        where += " AND m.created_at < ?"
        params.append(before)
    params.append(limit)
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT m.id, m.agent_id, m.challenge, "
            "       COALESCE(a.name, m.agent_name) AS agent_name, "
            "       m.content, m.msg_type, m.created_at "
            "FROM messages m "
            "LEFT JOIN agents a ON a.id = m.agent_id "
            f"WHERE {where} "
            "ORDER BY m.created_at DESC LIMIT ?",
            params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return rows


@app.get("/api/hypotheses")
async def list_hypotheses(
    limit: int = 30, challenge: str | None = None, before: str | None = None,
):
    """Recent hypotheses for the challenge, newest-first. Mirrors the
    `recent_hypotheses` block of /api/state but is paginated via an optional
    `before` (created_at) cursor so the Ideas research feed can page backwards
    through history that scrolled off its in-memory buffer."""
    challenge = await resolve_challenge(challenge)
    limit = max(1, min(limit, 200))
    where = "h.challenge = ?"
    params: list = [challenge]
    if before:
        where += " AND h.created_at < ?"
        params.append(before)
    params.append(limit)
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT h.id, h.title, h.strategy_tag, h.description, "
            "       a.name as agent_name, h.agent_id, h.parent_hypothesis_id, "
            "       h.created_at "
            "FROM hypotheses h JOIN agents a ON a.id = h.agent_id "
            f"WHERE {where} "
            "ORDER BY h.created_at DESC LIMIT ?",
            params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return rows



# ── Diversity ──

@app.get("/api/diversity", response_model=api_models.DiversityResponse)
async def get_diversity(challenge: str | None = None):
    """Pairwise code-diversity matrix over **trajectories** (active +
    inactive), not over current agents.

    Each cell compares the algorithm code that defines a trajectory:
      - Active trajectories → the latest feasible experiment on that
        trajectory (i.e. the trajectory_bests row whose trajectory_id matches).
      - Inactive trajectories → the algorithm_code stored in
        inactive_algorithms when the trajectory was deposited.

    Response shape: `{"trajectories": [...], "matrix": [[...]]}`. Each
    trajectory entry has `trajectory_id` and a human-readable
    `display_name` like "traj abcdef · alice".
    """
    challenge = await resolve_challenge(challenge)
    direction = await get_direction(challenge)
    order = db._direction_order(direction)

    async with db.connect() as conn:
        # Active trajectories: pick the latest feasible code via trajectory_bests.
        # We take the highest-scoring trajectory_bests row per trajectory_id when
        # there are multiple — this matches the trajectory's `current_score`
        # surfaced elsewhere.
        active_cur = await conn.execute(
            f"""SELECT t.id AS trajectory_id, t.started_at,
                       ab.algorithm_code, ab.agent_id, a.name AS agent_name
                  FROM trajectories t
                  JOIN trajectory_bests ab
                    ON ab.trajectory_id = t.id
                   AND ab.challenge = t.challenge
                   AND ab.feasible = 1
                  JOIN agents a ON a.id = ab.agent_id
                 WHERE t.challenge = ? AND t.status = 'active'
                 ORDER BY ab.score {order}""",
            (challenge,),
        )
        active_rows = [dict(r) for r in await active_cur.fetchall()]

        # Inactive trajectories: use the deposited algorithm code, picking
        # the most recent deposit when a trajectory has been deposited
        # multiple times (rare but possible after re-deactivation).
        inactive_cur = await conn.execute(
            """SELECT ia.trajectory_id, ia.algorithm_code, ia.agent_id,
                      a.name AS agent_name, ia.deposited_at
                 FROM inactive_algorithms ia
                 LEFT JOIN agents a ON a.id = ia.agent_id
                WHERE ia.challenge = ?
                ORDER BY ia.deposited_at DESC""",
            (challenge,),
        )
        inactive_raw = [dict(r) for r in await inactive_cur.fetchall()]

    # Dedupe both lists by trajectory_id (one entry per trajectory). Active
    # is dedup'd because a trajectory could in theory have multiple
    # trajectory_bests rows after adoption; inactive is dedup'd to keep the
    # most recent deposit when re-deactivation happened.
    seen: set[str] = set()
    entries: list[dict] = []
    for r in active_rows:
        tid = r["trajectory_id"]
        if not tid or tid in seen:
            continue
        seen.add(tid)
        entries.append({
            "trajectory_id": tid,
            "display_name": _traj_label(tid, r.get("agent_name"), "active"),
            "algorithm_code": r["algorithm_code"] or "",
        })
    for r in inactive_raw:
        tid = r["trajectory_id"]
        if not tid or tid in seen:
            continue
        seen.add(tid)
        entries.append({
            "trajectory_id": tid,
            "display_name": _traj_label(tid, r.get("agent_name"), "inactive"),
            "algorithm_code": r["algorithm_code"] or "",
        })

    if not entries:
        return {"trajectories": [], "matrix": []}

    line_sets = []
    for e in entries:
        lines = set(e["algorithm_code"].splitlines())
        lines.discard("")
        line_sets.append(lines)

    n = len(entries)
    all_others = [
        set().union(*(line_sets[k] for k in range(n) if k != i))
        for i in range(n)
    ]

    matrix = []
    for i in range(n):
        total = len(line_sets[i]) or 1
        row = []
        for j in range(n):
            if i == j:
                unique = line_sets[i] - all_others[i]
                row.append(round(len(unique) / total, 3))
            else:
                shared = line_sets[i] & line_sets[j]
                row.append(round(len(shared) / total, 3))
        matrix.append(row)

    trajectories = [
        {"trajectory_id": e["trajectory_id"], "display_name": e["display_name"]}
        for e in entries
    ]
    return {"trajectories": trajectories, "matrix": matrix}


def _traj_label(traj_id: str, agent_name: str | None, status: str) -> str:
    """Compact label for a trajectory in the diversity matrix headers.

    Format: ``<6-char traj-id> · <agent-name>`` — short enough to fit in the
    diversity panel's column / row chips, with a trailing tag when inactive
    so projected swarms can see at a glance which trajectories are still
    being worked on."""
    head = traj_id[:6] if traj_id else "?"
    tail = agent_name or "?"
    suffix = "" if status == "active" else " (inactive)"
    return f"{head} · {tail}{suffix}"


@app.get("/api/inspiration_matrix")
async def get_inspiration_matrix(challenge: str | None = None):
    """NxN matrix of inspiration counts between **trajectories**.

    matrix[i][j] = number of times trajectory i received inspiration from
    trajectory j.  The receiver trajectory comes from the experiment's own
    trajectory_id; the source trajectory is looked up via the source agent's
    trajectory_bests row (their trajectory at inspiration time is approximated by
    their current trajectory — exact enough for the dashboard view).
    """
    challenge = await resolve_challenge(challenge)
    async with db.connect() as conn:
        cursor = await conn.execute(
            """SELECT e.trajectory_id        AS recv_traj,
                      ab_src.trajectory_id   AS src_traj,
                      a_recv.name            AS recv_agent,
                      a_src.name             AS src_agent,
                      COUNT(*)               AS cnt
               FROM experiments e
               JOIN agents a_recv ON a_recv.id = e.agent_id
               JOIN agents a_src  ON a_src.id  = e.inspiration_source_id
               JOIN trajectory_bests ab_src
                 ON ab_src.agent_id  = e.inspiration_source_id
                AND ab_src.challenge = e.challenge
              WHERE e.challenge = ?
                AND e.received_hint = 'inspiration'
                AND e.inspiration_source_id IS NOT NULL
                AND e.trajectory_id IS NOT NULL
              GROUP BY e.trajectory_id, ab_src.trajectory_id""",
            (challenge,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    traj_ids: list[str] = []
    traj_labels: dict[str, str] = {}
    seen: set[str] = set()
    for r in rows:
        for tid, aname in [(r["recv_traj"], r["recv_agent"]),
                           (r["src_traj"], r["src_agent"])]:
            if tid and tid not in seen:
                seen.add(tid)
                traj_ids.append(tid)
                traj_labels[tid] = _traj_label(tid, aname, "active")

    if not traj_ids:
        return {"agents": [], "matrix": []}

    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        rt, st = r["recv_traj"], r["src_traj"]
        if rt and st:
            counts[(rt, st)] = r["cnt"]

    n = len(traj_ids)
    matrix = []
    for i in range(n):
        row = []
        for j in range(n):
            row.append(counts.get((traj_ids[i], traj_ids[j]), 0))
        matrix.append(row)

    agents = [
        {"agent_id": tid, "agent_name": traj_labels[tid]}
        for tid in traj_ids
    ]
    return {"agents": agents, "matrix": matrix}


# ── Replay ──

@app.get("/api/replay", response_model=list[api_models.ReplayRow])
async def get_replay(challenge: str | None = None, compact: int = 0):
    """Best-history replay for a challenge.

    `compact=1` omits the per-row `solution_data` field so callers that
    only need score/agent/timestamp (the chart panel's score-history
    feed) don't pay for 100 KB+ of viz payload they'd just discard.
    The visualization panels continue to use the default full payload.

    Schema: see ``server/api_models.py:ReplayRow``. The compact variant
    leaves ``solution_data=None`` in the response — same model shape so
    callers don't have to branch on the ``compact`` query param.
    """
    challenge = await resolve_challenge(challenge)
    async with db.connect() as conn:
        # JOIN agents so the response always carries the agent's current
        # name; the bh.agent_name snapshot is only used as a fallback when
        # the agent row has been deleted (shouldn't happen in practice).
        cursor = await conn.execute(
            "SELECT bh.experiment_id, bh.agent_id, "
            "       COALESCE(a.name, bh.agent_name) AS agent_name, "
            "       bh.score, bh.solution_data, bh.created_at "
            "FROM best_history bh "
            "LEFT JOIN agents a ON a.id = bh.agent_id "
            "WHERE bh.challenge = ? ORDER BY bh.created_at ASC",
            (challenge,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    if compact:
        return [
            api_models.ReplayRow(
                experiment_id=r["experiment_id"],
                agent_id=r.get("agent_id"),
                agent_name=r["agent_name"],
                score=r["score"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    return [
        api_models.ReplayRow(
            experiment_id=r["experiment_id"],
            agent_id=r.get("agent_id"),
            agent_name=r["agent_name"],
            score=r["score"],
            solution_data=json.loads(r["solution_data"]) if r["solution_data"] else None,
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get("/api/top_scores")
async def get_top_scores(limit: int = 20, challenge: str | None = None):
    # Top-N feasible iterations for the given challenge, joined to the
    # proposing hypothesis for its strategy tag + title.
    challenge = await resolve_challenge(challenge)
    direction = await get_direction(challenge)
    order = db._direction_order(direction)
    limit = max(1, min(limit, 100))
    async with db.connect() as conn:
        cursor = await conn.execute(
            f"""SELECT e.id AS experiment_id, e.score, e.created_at,
                      e.agent_id, a.name AS agent_name,
                      h.strategy_tag, h.title
               FROM experiments e
               LEFT JOIN hypotheses h ON h.id = e.hypothesis_id
               LEFT JOIN agents a ON a.id = e.agent_id
               WHERE e.feasible = 1 AND e.challenge = ?
               ORDER BY e.score {order}
               LIMIT ?""",
            (challenge, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return {"challenge": challenge, "entries": rows, "limit": limit}


@app.get("/api/agent_experiments")
async def get_agent_experiments(
    agent_id: str,
    challenge: str | None = None,
    include_code: bool = False,
):
    # Per-agent full attempt history for the personal progress chart, scoped
    # to the requested challenge (defaults to active). Returns every experiment
    # (improvement or not, feasible or not) so the dashboard can render a
    # step plot of the agent's whole journey on this challenge.
    # Pass include_code=true to also return algorithm_code per experiment.
    challenge = await resolve_challenge(challenge)
    async with db.connect() as conn:
        ag = await conn.execute(
            "SELECT id, name, registered_at FROM agents WHERE id = ?",
            (agent_id,),
        )
        agent_row = await ag.fetchone()
        if agent_row is None:
            return {"agent_id": agent_id, "agent_name": None,
                    "registered_at": None, "challenge": challenge, "experiments": []}

        code_col = ", e.algorithm_code" if include_code else ""
        cursor = await conn.execute(
            f"""SELECT e.id, e.score, e.feasible, e.beats_trajectory_best, e.notes,
                      e.created_at, e.trajectory_id, e.received_hint,
                      t.status AS trajectory_status,
                      h.title, h.description, h.strategy_tag
                      {code_col}
               FROM experiments e
               LEFT JOIN hypotheses h ON h.id = e.hypothesis_id
               LEFT JOIN trajectories t ON t.id = e.trajectory_id
               WHERE e.agent_id = ? AND e.challenge = ?
               ORDER BY e.created_at ASC""",
            (agent_id, challenge),
        )
        rows = await cursor.fetchall()

    # Augment each row with `trajectory_deactivated`: True when this is the
    # last experiment the agent ran on a trajectory that subsequently became
    # inactive. The dashboard uses this to mark the deactivation point on
    # the per-agent benchmark plot.
    rows_list = [dict(r) for r in rows]
    last_idx_by_traj: dict[str, int] = {}
    for i, r in enumerate(rows_list):
        tid = r.get("trajectory_id")
        if tid:
            last_idx_by_traj[tid] = i
    for i, r in enumerate(rows_list):
        tid = r.get("trajectory_id")
        is_last_for_traj = bool(tid) and last_idx_by_traj.get(tid) == i
        r["trajectory_deactivated"] = bool(
            is_last_for_traj and (r.get("trajectory_status") == "inactive")
        )

    def _row_dict(r):
        d = {
            "id": r["id"],
            "score": r["score"],
            "feasible": bool(r["feasible"]),
            "beats_trajectory_best": bool(r["beats_trajectory_best"]) if r["beats_trajectory_best"] is not None else False,
            "notes": r["notes"],
            "title": r["title"],
            "description": r["description"],
            "strategy_tag": r["strategy_tag"],
            "trajectory_id": r.get("trajectory_id"),
            "received_hint": r.get("received_hint"),
            "trajectory_deactivated": bool(r.get("trajectory_deactivated")),
            "created_at": r["created_at"],
        }
        if include_code:
            d["algorithm_code"] = r["algorithm_code"]
        return d

    return {
        "agent_id": agent_id,
        "challenge": challenge,
        "agent_name": agent_row["name"],
        "registered_at": agent_row["registered_at"],
        "experiments": [_row_dict(r) for r in rows_list],
    }


# ── Trajectories ──

@app.get("/api/trajectories")
async def get_trajectories(challenge: str | None = None):
    challenge = await resolve_challenge(challenge)
    direction = await get_direction(challenge)
    async with db.connect() as conn:
        trajectories = await db.list_trajectories(conn, challenge=challenge)
        result = []
        for t in trajectories:
            history = await db.get_trajectory_score_history(
                conn, t["id"], challenge=challenge, direction=direction,
            )
            # `unique_agents` is the authoritative count of distinct agents
            # that have published an experiment on this trajectory (computed
            # by list_trajectories via DISTINCT on experiments.agent_id).
            # `num_agents` on the row is only ever bumped on creation /
            # adoption, so it under-counts in practice. Surface the
            # authoritative value as `num_agents` on the wire so the
            # dashboard's existing column wiring keeps working.
            unique_agents = t.get("unique_agents")
            if unique_agents is None or unique_agents == 0:
                unique_agents = t.get("num_agents") or 0
            result.append({
                "id": t["id"],
                "started_at": t["started_at"],
                "status": t["status"],
                "current_score": t["current_score"],
                "num_edits": t["num_edits"],
                "num_improvements": t["num_improvements"],
                "momentum": round(t["momentum"], 4) if t["momentum"] else 0,
                "num_agents": unique_agents,
                "num_deactivations": t.get("num_deactivations") or 0,
                "edits_since_improvement": t["edits_since_improvement"] or 0,
                "deactivated_at": t["deactivated_at"],
                "score_history": history,
            })
        active = sum(1 for t in result if t["status"] == "active")
        inactive = sum(1 for t in result if t["status"] == "inactive")
    return {
        "challenge": challenge,
        "total": len(result),
        "active": active,
        "inactive": inactive,
        "trajectories": result,
    }


@app.get("/api/trajectory_experiments")
async def get_trajectory_experiments(
    challenge: str | None = None,
    trajectory_id: str | None = None,
    include_code: bool = False,
):
    """All experiments grouped by trajectory.

    Optionally filter to a single trajectory via ?trajectory_id=...
    Pass include_code=true to return algorithm_code per experiment.
    """
    challenge = await resolve_challenge(challenge)
    async with db.connect() as conn:
        traj_filter = ""
        params: list = [challenge]
        if trajectory_id:
            traj_filter = "AND e.trajectory_id = ?"
            params.append(trajectory_id)

        code_col = ", e.algorithm_code" if include_code else ""
        cursor = await conn.execute(
            f"""SELECT e.id, e.trajectory_id, e.agent_id, a.name AS agent_name,
                       e.score, e.feasible, e.beats_trajectory_best, e.notes,
                       e.created_at, h.title, h.description, h.strategy_tag
                       {code_col}
                FROM experiments e
                LEFT JOIN hypotheses h ON h.id = e.hypothesis_id
                LEFT JOIN agents a ON a.id = e.agent_id
                WHERE e.challenge = ? {traj_filter}
                ORDER BY e.trajectory_id, e.created_at ASC""",
            params,
        )
        rows = await cursor.fetchall()

    grouped: dict[str, list] = {}
    for r in rows:
        tid = r["trajectory_id"] or "unknown"
        d = {
            "id": r["id"],
            "agent_id": r["agent_id"],
            "agent_name": r["agent_name"],
            "score": r["score"],
            "feasible": bool(r["feasible"]),
            "beats_trajectory_best": bool(r["beats_trajectory_best"]) if r["beats_trajectory_best"] is not None else False,
            "notes": r["notes"],
            "title": r["title"],
            "description": r["description"],
            "strategy_tag": r["strategy_tag"],
            "created_at": r["created_at"],
        }
        if include_code:
            d["algorithm_code"] = r["algorithm_code"]
        grouped.setdefault(tid, []).append(d)

    return {"challenge": challenge, "trajectories": grouped}


# ── Admin endpoints ──

@app.post("/api/admin/broadcast")
async def admin_broadcast(req: AdminBroadcast):
    await verify_admin(req)
    await manager.broadcast(ws_events.AdminBroadcastEvt(
        message=req.message,
        priority=req.priority,
        timestamp=now(),
    ))
    return {"sent": True}


@app.post("/api/admin/reset_challenge")
async def admin_reset_challenge(req: AdminResetChallenge):
    """Per-challenge leaderboard reset. Drops `trajectory_bests` + `best_history`
    for the named challenge so the next feasible publish becomes the new
    global best. Preserves `experiments`, `hypotheses`, and `trajectories`
    so the swarm's research history isn't erased.

    Use case: a wire-format change (e.g. the route_data → solution_data
    rename + trailing-slash fix) leaves all prior best_history rows with
    NULL solution_data, so the dashboard's gantt / route panels render
    blank. Resetting the leaderboard lets fresh publishes — which now
    carry solution_data correctly — repopulate the visualisation.
    """
    await verify_admin(req)
    challenge = req.challenge
    async with db.connect() as conn:
        cur = await conn.execute(
            "DELETE FROM best_history WHERE challenge = ?", (challenge,),
        )
        best_history_deleted = cur.rowcount
        cur = await conn.execute(
            "DELETE FROM trajectory_bests WHERE challenge = ?", (challenge,),
        )
        trajectory_bests_deleted = cur.rowcount
        await conn.commit()
    await manager.broadcast(ws_events.ResetEvt(
        challenge=challenge,
        timestamp=now(),
    ))
    return {
        "reset": True,
        "challenge": challenge,
        "best_history_deleted": best_history_deleted,
        "trajectory_bests_deleted": trajectory_bests_deleted,
    }


SEED_INACTIVE_SUPPORTED = ("knapsack", "satisfiability")


@app.post("/api/admin/seed_inactive")
async def admin_seed_inactive(req: AdminSeedInactive):
    """Insert an externally-sourced algorithm into the inactive_algorithms
    pool. The next stagnated agent on this challenge that does NOT qualify
    for a fresh start (i.e. inactive pool is non-empty AND n_trajectories^1.5
    >= total_deactivations) picks it up via the existing `adopted_inactive`
    branch in server.py — at which point it is removed from the pool
    (consume-once semantics).

    Restricted to challenges whose mainnet algorithm format matches the
    swarm's single-file expectation. The host-side wizard enforces the
    same set; this is defense-in-depth so a stray curl can't seed an
    unsupported challenge with a payload that would break adoption."""
    await verify_admin(req)
    if req.challenge not in SEED_INACTIVE_SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"seed_inactive is supported for {list(SEED_INACTIVE_SUPPORTED)} "
                f"only (got {req.challenge!r})"
            ),
        )
    if not req.algorithm_code.strip():
        raise HTTPException(status_code=400, detail="algorithm_code is empty")
    timestamp = now()
    async with db.connect() as conn:
        agent_id = await db.ensure_synthetic_agent(
            conn, req.source_label, timestamp,
        )
        inactive_id = await db.deposit_inactive(
            conn, agent_id, req.challenge,
            req.algorithm_code, None, timestamp,
            kernel_code=req.kernel_code,
        )
        await conn.commit()
    return {
        "seeded": True,
        "challenge": req.challenge,
        "inactive_id": inactive_id,
        "source": req.source_label,
    }


@app.post("/api/admin/seed_pool")
async def admin_seed_pool(req: AdminSeedPool):
    """Deposit a host-authored seed algorithm into `seed_pool`. Deduped by
    (challenge, strategy_tag, source='authored'); a repeat for the same tag is
    silently ignored (idempotent re-runs of `setup.py create`)."""
    await verify_admin(req)
    if not req.algorithm_code.strip():
        raise HTTPException(status_code=400, detail="algorithm_code is empty")
    timestamp = now()
    async with db.connect() as conn:
        added = await db.insert_seed(
            conn, req.challenge, req.strategy_tag, req.algorithm_code,
            created_at=timestamp, source="authored", score=req.score,
            feasible=True, kernel_code=req.kernel_code,
        )
        await conn.commit()
    return {
        "seeded": added,
        "challenge": req.challenge,
        "strategy_tag": req.strategy_tag,
    }


@app.post("/api/admin/revoke")
async def admin_revoke(req: AdminRevoke):
    """Revoke a contributor by username.

    Two effects, applied in the same transaction:
      1. Adds the username to `config.revoked_contributors` so
         `verify_swarm_password` rejects future /api/agents/register calls
         under that name (even with a still-valid derived password hash).
      2. Clears the per-agent `token` (and stamps `status='revoked'`) on
         every agent row whose `contributor_username` matches, so existing
         workers fail `verify_agent_token` on their next write call.
         Agent rows themselves are preserved — dashboard history is
         intact; only the auth handle is cut.

    Idempotent: re-revoking the same username adds nothing and just
    re-counts how many of their agents are currently still token-bearing.
    """
    global _config_cache
    await verify_admin(req)
    username = (req.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username must be non-empty")
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT value FROM config WHERE key = 'revoked_contributors'"
        )
        row = await cur.fetchone()
        try:
            revoked = set(json.loads(row["value"])) if row and row["value"] else set()
        except (ValueError, TypeError):
            revoked = set()
        revoked.add(username)
        await conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("revoked_contributors", json.dumps(sorted(revoked))),
        )
        cur = await conn.execute(
            "UPDATE agents SET token = NULL, status = 'revoked' "
            "WHERE contributor_username = ? AND token IS NOT NULL",
            (username,),
        )
        agents_invalidated = cur.rowcount
        await conn.commit()
    _config_cache = None  # invalidate so the next register sees the new revoked set
    return {
        "revoked": username,
        "agents_invalidated": agents_invalidated,
    }


@app.post("/api/admin/contributors")
async def admin_contributors(req: AdminAuth):
    """Return one row per contributor known to this swarm.

    Sources merged:
      - `agents` table grouped by `contributor_username` (registered names).
      - `config.revoked_contributors` (names that were revoked but may have
        had no surviving agents).

    Fields per row:
      - username
      - agent_count: total agents the contributor ever registered
      - agents_active: agents with last_heartbeat within the inactive window
      - agents_invalidated: agents whose token was cleared (revoke side-effect)
      - last_heartbeat: most recent heartbeat across all their agents (or null)
      - revoked: true if the username is in config.revoked_contributors
    """
    await verify_admin(req)
    cutoff = inactive_cutoff()
    config = await get_config_cached()
    revoked = _revoked_usernames(config)
    rows: dict[str, dict] = {}
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT contributor_username AS username, "
            "       COUNT(*) AS agent_count, "
            "       SUM(CASE WHEN last_heartbeat >= ? THEN 1 ELSE 0 END) AS agents_active, "
            "       SUM(CASE WHEN token IS NULL THEN 1 ELSE 0 END) AS agents_invalidated, "
            "       MAX(last_heartbeat) AS last_heartbeat "
            "FROM agents WHERE contributor_username IS NOT NULL "
            "GROUP BY contributor_username",
            (cutoff,),
        )
        for r in await cursor.fetchall():
            username = r["username"]
            rows[username] = {
                "username": username,
                "agent_count": r["agent_count"] or 0,
                "agents_active": r["agents_active"] or 0,
                "agents_invalidated": r["agents_invalidated"] or 0,
                "last_heartbeat": r["last_heartbeat"],
                "revoked": username in revoked,
            }
    # Surface revoked names with no surviving agent rows so they're still
    # auditable (and so a host can't be surprised by a "missing" name).
    for username in revoked:
        if username not in rows:
            rows[username] = {
                "username": username,
                "agent_count": 0,
                "agents_active": 0,
                "agents_invalidated": 0,
                "last_heartbeat": None,
                "revoked": True,
            }
    # Sort: active contributors first (by most recent heartbeat), then
    # never-heartbeated, then revoked-with-no-agents at the bottom.
    def _sort_key(row: dict) -> tuple:
        return (
            row["revoked"] and row["agent_count"] == 0,
            row["last_heartbeat"] is None,
            -(row["agents_active"] or 0),
            row["last_heartbeat"] or "",
        )
    contributors = sorted(rows.values(), key=_sort_key)
    return {"contributors": contributors, "inactive_cutoff": cutoff}


@app.post("/api/admin/config")
async def admin_config(req: AdminAuth, key: str = "", value: str = ""):
    global _config_cache
    await verify_admin(req)
    if key and value:
        async with db.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
            await conn.commit()
        _config_cache = None  # invalidate cache
    return {"updated": True}


# ── Swarm config (read by every clone, written by the setup wizard) ──

@app.get("/api/swarm_config")
async def get_swarm_config():
    """Return the swarm-wide settings every clone needs to run.

    `active_challenge` is the challenge contributors auto-follow (set by
    the owner via POST /api/swarm_config). `available_challenges` is the
    per-challenge sub-config map (tracks, timeout, scoring_direction,
    initial_algorithm_code) — the agent looks up its active sub-config in
    here on every iteration.
    """
    config = await get_config_cached()
    active_challenge = config.get("active_challenge") or DEFAULT_CHALLENGE

    # Per-challenge sub-configs.
    available: dict[str, dict] = {}
    async with db.connect() as conn:
        rows = await db.list_challenge_configs(conn)
    for row in rows:
        try:
            tracks = json.loads(row.get("tracks") or "{}")
        except Exception:
            tracks = {}
        try:
            strategy_tags = json.loads(row.get("strategy_tags") or "[]")
        except Exception:
            strategy_tags = []
        ch_name = row["challenge"]
        ch_def = challenges.CHALLENGES.get(ch_name)
        available[ch_name] = {
            "tracks": tracks,
            "timeout": row.get("timeout") or (ch_def.default_timeout if ch_def else 30),
            "scoring_direction": row.get("scoring_direction") or (
                ch_def.scoring_direction if ch_def else "max"
            ),
            # Flag-only: don't ship the algorithm body in this response (it
            # can be large and is fetched separately from /api/initial_algorithm).
            "has_initial_algorithm": bool(row.get("initial_algorithm_code")),
            "has_initial_kernel_code": bool(row.get("initial_kernel_code")),
            "is_gpu": ch_def.is_gpu if ch_def else False,
            "strategy_tags": strategy_tags,
        }

    return {
        "active_challenge": active_challenge,
        "available_challenges": available,
        # Global keys.
        "swarm_name": config.get("swarm_name", ""),
        "owner_name": config.get("owner_name", ""),
        "swarm_type": config.get("swarm_type", "cpu"),
        "stagnation_threshold": swarm_setting(config, "stagnation_threshold"),
        "stagnation_limit": swarm_setting(config, "stagnation_limit"),
        "hypothesis_recall_threshold": swarm_setting(
            config, "hypothesis_recall_threshold",
        ),
    }


@app.get("/api/initial_algorithm")
async def get_initial_algorithm(challenge: str | None = None):
    """Return the per-challenge initial algorithm code. Used by agents on
    their first iteration and by the wizard for round-trip verification."""
    challenge = await resolve_challenge(challenge)
    cfg = await get_challenge_config_cached(challenge)
    return {
        "challenge": challenge,
        "algorithm_code": cfg.get("initial_algorithm_code", "") or "",
        "kernel_code": cfg.get("initial_kernel_code", "") or "",
    }


@app.post("/api/swarm_config")
async def update_swarm_config(req: SwarmConfigUpdate):
    """Owner-only endpoint to update swarm-wide configuration.

    Pass `active_challenge` to flip the swarm's active challenge, and/or
    `challenges` to merge per-challenge sub-configs (partial updates
    supported — only the keys passed get written). Global keys
    (swarm_name, stagnation thresholds) update independently.

    Gated by admin_key — same secret used for /api/admin/broadcast.
    """
    await verify_admin(req)

    challenges_payload: dict[str, dict] = {}
    if req.challenges:
        for ch, sub in req.challenges.items():
            d = sub.dict() if hasattr(sub, "dict") else sub
            challenges_payload[ch] = d

    async with db.connect() as conn:
        for ch, sub in challenges_payload.items():
            await db.upsert_challenge_config(
                conn, ch,
                tracks=json.dumps(sub["tracks"]) if sub.get("tracks") is not None else None,
                timeout=sub.get("timeout"),
                scoring_direction=sub.get("scoring_direction"),
                initial_algorithm_code=sub.get("initial_algorithm_code"),
                initial_kernel_code=sub.get("initial_kernel_code"),
                strategy_tags=json.dumps(sub["strategy_tags"]) if sub.get("strategy_tags") is not None else None,
            )
        if req.active_challenge:
            await db.set_active_challenge(conn, req.active_challenge)
        for key, value in (
            ("swarm_name", req.swarm_name),
            ("owner_name", req.owner_name),
            ("swarm_type", req.swarm_type),
            ("stagnation_threshold", str(req.stagnation_threshold) if req.stagnation_threshold is not None else None),
            ("stagnation_limit", str(req.stagnation_limit) if req.stagnation_limit is not None else None),
            ("hypothesis_recall_threshold", str(req.hypothesis_recall_threshold) if req.hypothesis_recall_threshold is not None else None),
        ):
            if value is not None:
                await conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (key, value),
                )
        await conn.commit()

    _invalidate_caches()

    config_after = await get_swarm_config()
    active_sub = config_after["available_challenges"].get(
        config_after["active_challenge"], {}
    )

    # Tell connected dashboards to refetch swarm_config so labels and the
    # active visualization swap to the new challenge without a page reload.
    await manager.broadcast(ws_events.SwarmConfigUpdated(
        active_challenge=config_after["active_challenge"],
        available_challenges=config_after["available_challenges"],
        scoring_direction=active_sub.get("scoring_direction", "max"),
        swarm_name=config_after["swarm_name"],
        timestamp=now(),
    ))
    return {"updated": True, **config_after}


# ── WebSocket ──

@app.websocket("/ws/dashboard")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Health ──

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": now()}


# ── Serve dashboard static files (must be last, catches all unmatched routes) ──
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
