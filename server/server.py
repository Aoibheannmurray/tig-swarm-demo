import json
import asyncio
import contextvars
import functools
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from models import (
    RegisterRequest, HeartbeatRequest, RenameRequest,
    IterationCreate, AdminBroadcast, AdminAuth, AdminResetChallenge,
    AdminRevoke, AdminSeedInactive, AdminSeedPool, AdminClearInactive,
    AdminSeedsQuery,
    ContributorConfigPut, MAX_CONTRIB_CONFIG_LEN,
    MessageCreate,
    SwarmConfigUpdate,
    AgentResponse,
    IterationResponse, new_id, improvement_pct,
)
from names import generate_agent_name, load_used_names, reserve_name
from dedup import fingerprint
import db
import tiers
import seed_diversity
import ws_events
import api_models
import challenges
from challenges import DEFAULT_CHALLENGE
from trajectory_reset import maybe_reset_trajectory

logger = logging.getLogger("swarm")


# ── Swarm-wide defaults ──
#
# Single source of truth for the integer thresholds the swarm tunes most
# often. Stored as strings in the `config` key/value table (set via the
# wizard's POST /api/swarm_config); these are the fall-throughs when a key
# is missing or unparseable. Add new tunables here so call sites stay
# consistent — never inline an `int(config.get(KEY, "N"))` again.
SWARM_DEFAULTS: dict[str, int] = {
    # 60, not 20: agentic / C3-benchmark iterations legitimately run 45+ min
    # of wall-clock per cycle. A shorter window lets the periodic_stats sweep
    # reap an actively-working agent's trajectory mid-iteration, so every
    # publish lands on a fresh trajectory and trivially "beats" an empty best.
    "inactive_minutes": 60,
    "stagnation_threshold": 2,
    "stagnation_limit": 4,
    # Kill-switch for trajectories that never turn positive: when > 0, a
    # trajectory whose best is still not better than 0 (= the baseline)
    # after this many edits is reset on the next state poll, exactly like a
    # stagnation_limit trip. Catches lines stagnation_limit can't: small
    # improvements below zero reset runs_since_improvement every time, yet
    # the pool refuses negative deposits, so such a line can grind forever
    # and never yield anything adoptable. 0 (default) disables. Leave it
    # disabled on challenges whose feasible scores are inherently negative
    # (e.g. neuralnet_optimizer) — there "positive" is unreachable and this
    # would cull every trajectory.
    "negative_trajectory_limit": 0,
    "hypothesis_recall_threshold": 3,
    # Seed-pool diversity (server-side; see server/seed_diversity.py).
    # K: max seeds kept per challenge. max_loc: simplicity ceiling — algorithms
    # above this many source lines are never harvested as seeds.
    "seed_pool_size": 10,
    "seed_max_loc": 200,
    # Hyperparameter-optimization gate + search. The contributor's driver reads
    # these from the pushed swarm config (see scripts/run_loop.py _hpo_gate_open
    # / _maybe_run_hpo). first_tune_improvements: per-trajectory improvements a
    # trajectory needs before its FIRST tune (the band check is waived that
    # once). min_improvements: improvements needed for later tunes; also sets
    # the tune-band width. search_budget (N): configs evaluated per tune
    # (default {} + suggested + random). num_suggested_configs: max
    # LLM-suggested configs folded into N.
    "hpo_first_tune_improvements": 10,
    "hpo_min_improvements": 4,
    "hpo_search_budget": 13,
    "hpo_num_suggested_configs": 5,
}

# Float-valued swarm tunables (swarm_setting only returns ints).
SWARM_FLOAT_DEFAULTS: dict[str, float] = {
    # Novelty gate: a candidate seed is admitted only if its similarity to every
    # existing seed is below this. Higher => more permissive (more seeds).
    "seed_similarity_threshold": 0.6,
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


def swarm_setting_float(config: dict, key: str) -> float:
    default = SWARM_FLOAT_DEFAULTS[key]
    raw = config.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
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
    """Pick the challenge a request applies to. An explicit value wins but
    must name a known challenge — 400 otherwise, so garbage query params
    can't grow the per-challenge config cache or seed junk
    agent_challenge_state rows. Falls back to the swarm's active challenge
    when none is provided."""
    if challenge:
        if not challenges.is_known_challenge(challenge):
            raise HTTPException(
                status_code=400, detail=f"Unknown challenge {challenge!r}",
            )
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


# The JSON codecs for the `algorithm_files` column live in db.py (they're
# shared with trajectory_reset.py). Private aliases kept — call sites
# throughout this module and the self-running tests use server._files_json /
# server._row_files.
_files_json = db.files_json
_row_files = db.row_files


async def seed_for_agent(
    conn, agent_id: str, challenge: str, tier: str, role: str,
    *, direction: str, cutoff_ts: str, seeded: bool | None = None,
) -> tuple[str, str, dict | None, str]:
    """Pick the starting code for an agent on a fresh trajectory.

    On CPU challenges frontier explorers keep the bare stub (they bootstrap),
    while standard-tier OR exploiter agents get working code. On GPU challenges
    *every* agent — frontier explorers included — gets working code, because
    bootstrapping a compiling CUDA/kernel algorithm from the stub is hard even
    for frontier models (temporary policy; see `is_gpu` below). Either way the
    working-code path is a fallback chain:
      seed pool (diverse per-agent assignment) → best active peer → stub.

    `seeded` is the contributor-owned per-agent override (`seeded_start` in
    fleet.config.json, reported on each /api/state poll like `role`): True
    forces the working-code chain, False forces the stub, None (absent)
    keeps the tier/role/GPU policy above.

    Returns (algorithm_code, kernel_code, algorithm_files, start) where
    `algorithm_files` is the multi-file map (or None for single-file) and
    `start` is one of 'seed' | 'peer' | 'stub' for the dashboard.
    """
    # For now, GPU challenges seed every model regardless of tier/role —
    # frontier models rarely produce a compiling kernel from the bare stub, so
    # handing them a working seed gets the whole fleet off the ground faster.
    ch_def = challenges.CHALLENGES.get(challenge)
    is_gpu = ch_def.is_gpu if ch_def else False
    if seeded is None:
        needs_seed = is_gpu or (tier == "standard") or (role == "exploiter")
    else:
        needs_seed = seeded
    if not needs_seed:
        code, kernel = await load_initial_algorithm(challenge)
        return code, kernel, None, "stub"

    seeds = await db.list_seeds(conn, challenge)
    if seeds:
        # Random per-trajectory draw: each fresh start re-rolls the launch point
        # so the population spreads across the (small, curated) seed pool.
        s = random.choice(seeds)
        return s["algorithm_code"], s.get("kernel_code") or "", _row_files(s), "seed"

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
        return best["algorithm_code"], best.get("kernel_code") or "", _row_files(best), "peer"

    # True cold start: no seeds and no feasible peers yet.
    code, kernel = await load_initial_algorithm(challenge)
    return code, kernel, None, "stub"


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
    supplied = req.admin_key or ""
    ip = _client_ip.get()
    # Constant-time compare (encode to bytes so a non-ASCII input can't raise
    # inside compare_digest) FIRST — a correct key always succeeds and is never
    # throttled. Only wrong keys count toward the per-IP brute-force limit.
    if expected and secrets.compare_digest(supplied.encode(), expected.encode()):
        _clear_auth_failure(f"admin:{ip}")
        return
    if _note_auth_failure(f"admin:{ip}") > _AUTH_FAIL_LIMIT:
        raise HTTPException(status_code=429, detail="Too many attempts; slow down")
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
    # Per-IP throttle on wrong passwords. Safe for fleets: every legitimate
    # agent presents the correct derived password, so it passes here and is
    # never throttled even when a whole fleet registers from one IP at once.
    ip = _client_ip.get()
    if not secrets.compare_digest(x_swarm_password.encode(), expected.encode()):
        if _note_auth_failure(f"swarm:{ip}") > _AUTH_FAIL_LIMIT:
            raise HTTPException(status_code=429, detail="Too many attempts; slow down")
        raise HTTPException(status_code=403, detail="Invalid credentials")
    _clear_auth_failure(f"swarm:{ip}")
    if x_username in _revoked_usernames(config):
        raise HTTPException(status_code=403, detail="Contributor has been revoked")
    return x_username


async def _agent_id_for_token(token: str) -> str | None:
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT id FROM agents WHERE token = ?", (token,),
        )
        row = await cursor.fetchone()
    return row["id"] if row else None


async def verify_agent_token(
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
) -> str:
    """Look up the agent by token. Returns the agent_id so downstream
    handlers can check it against the agent the request claims to act as;
    raises 403 if the token is missing or unknown.

    Issued at /api/agents/register and stored on the agents row. Revoking a
    contributor clears the token (see /api/admin/revoke)."""
    if not x_agent_token:
        raise HTTPException(status_code=403, detail="Missing agent token")
    agent_id = await _agent_id_for_token(x_agent_token)
    if agent_id is None:
        raise HTTPException(status_code=403, detail="Invalid agent token")
    return agent_id


async def optional_agent_token(
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
) -> str | None:
    """Like verify_agent_token, but for endpoints that are public without an
    agent_id (GET /api/state): a missing or unknown token resolves to None
    instead of 403. The handler enforces the token↔agent match only when the
    request names an agent."""
    if not x_agent_token:
        return None
    return await _agent_id_for_token(x_agent_token)


def require_token_matches(token_agent_id: str | None, agent_id: str | None) -> None:
    """403 unless the token resolved to exactly the agent the request acts as."""
    if token_agent_id is None or token_agent_id != agent_id:
        raise HTTPException(
            status_code=403, detail="Agent token does not match agent_id",
        )


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

# How many recent trajectory improvement scores to return in /api/state for the
# hyperparameter-search gate. Must comfortably exceed any host-set
# hpo_min_improvements (the band floor is result[-min_improvements]).
_IMPROVEMENT_HISTORY_LIMIT = 16


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
        # Snapshot the list: concurrent broadcasts/connects mutate it while
        # we await, so sends and pruning must both work off this copy.
        targets = list(self.connections)
        # Per-send timeout: a TimeoutError from wait_for is captured by
        # return_exceptions=True alongside any other send failure, so the
        # below pruning drops hung subscribers exactly the same way it
        # drops ones that closed cleanly.
        results = await asyncio.gather(
            *(
                asyncio.wait_for(ws.send_json(payload), timeout=_WS_SEND_TIMEOUT_S)
                for ws in targets
            ),
            return_exceptions=True,
        )
        # Remove failed sockets from the LIVE list individually — rebuilding
        # self.connections from the snapshot would evict subscribers that
        # connected mid-broadcast. disconnect() tolerates sockets a
        # concurrent broadcast already removed.
        for ws, result in zip(targets, results):
            if isinstance(result, BaseException):
                self.disconnect(ws)


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

# Client IP for the current request, captured by middleware so the auth helpers
# (verify_admin / verify_swarm_password) can throttle failed attempts per source
# without threading Request through every endpoint. X-Forwarded-For is only
# honored when TRUSTED_PROXY=1 says a reverse proxy fronts the server and owns
# the header (Railway always does — server/entrypoint.sh sets the var). Without
# a proxy the header is client-controlled, so trusting it would let an attacker
# pick a fresh throttle bucket per request; use the socket peer instead.
_client_ip: contextvars.ContextVar[str] = contextvars.ContextVar("client_ip", default="")

_TRUST_PROXY = os.environ.get("TRUSTED_PROXY", "") == "1"


@app.middleware("http")
async def _capture_client_ip(request: Request, call_next):
    peer = request.client.host if request.client else ""
    ip = peer
    if _TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        ip = (xff.split(",")[0].strip() if xff else "") or peer
    _client_ip.set(ip)
    return await call_next(request)


# ── Brute-force throttle on failed auth (defense-in-depth) ──
#
# admin_key and the base swarm_password are 128-bit secrets (secrets.token_urlsafe(16)),
# so online brute force is already infeasible; this bounds it further and slows
# credential stuffing. It is DoS-safe BY CONSTRUCTION: a request bearing the
# CORRECT secret always passes (the constant-time compare runs first and returns
# before any throttle check), so a flood of wrong guesses — even sharing the
# legitimate user's proxy IP — can never lock the real admin/contributor out.
_AUTH_FAIL_WINDOW_S = 60.0
_AUTH_FAIL_LIMIT = 20
_auth_failures: dict[str, list[float]] = {}


def _note_auth_failure(key: str) -> int:
    """Record a failed attempt for `key` (e.g. "admin:1.2.3.4") and return the
    number of failures still inside the sliding window."""
    mono = time.monotonic()
    recent = [t for t in _auth_failures.get(key, []) if mono - t < _AUTH_FAIL_WINDOW_S]
    recent.append(mono)
    _auth_failures[key] = recent
    # Opportunistic cleanup so the map can't grow unbounded across many source IPs.
    if len(_auth_failures) > 2048:
        for k in [k for k, v in _auth_failures.items()
                  if all(mono - t >= _AUTH_FAIL_WINDOW_S for t in v)]:
            _auth_failures.pop(k, None)
    return len(recent)


def _clear_auth_failure(key: str) -> None:
    _auth_failures.pop(key, None)


# Static dashboard mounted after all routes (see bottom of file)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def inactive_cutoff() -> str:
    # Await the config rather than peeking at _config_cache directly: the
    # cache may be cold (first periodic_stats sweep after boot, or right
    # after an admin invalidation), and a cold read would silently fall back
    # to the default inactive_minutes instead of the host-tuned value.
    cfg = await get_config_cached()
    minutes = swarm_setting(cfg, "inactive_minutes")
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# ── Shared per-challenge stats helpers ──
#
# Used by BOTH the periodic_stats broadcast and the /api/state views so the
# dashboard's counters and the state endpoint can never drift apart.


async def _challenge_counters(
    conn, cutoff_ts: str, challenge: str | None = None,
) -> dict[str, dict]:
    """Per-challenge dashboard counters, batched.

    Batched per-challenge counters. Previously the periodic_stats loop fired
    5 separate COUNT queries per challenge (active, exp, hyp, traj,
    agents_in_challenge) — at 8 challenges that's 40 roundtrips every 10s.
    At scale (~80 agents, 4-hour test) those queries also start touching
    more rows. Collapsing into 5 grouped queries (one per category, GROUP BY
    challenge) keeps total roundtrips constant regardless of how many
    challenges are configured.

    Pass `challenge` to restrict all 5 queries to one challenge (the
    /api/state views). Challenges with no rows anywhere are absent from the
    result — callers .get() with zero defaults.
    """
    ch_and = " AND challenge = ?" if challenge else ""
    ch_where = " WHERE challenge = ?" if challenge else ""
    ch_params: tuple = (challenge,) if challenge else ()

    async def _by_ch(sql: str, params: tuple) -> dict[str, int]:
        cur = await conn.execute(sql, params)
        return {r["challenge"]: r["c"] for r in await cur.fetchall()}

    active_by_ch = await _by_ch(
        "SELECT challenge, COUNT(*) as c FROM agent_challenge_state "
        f"WHERE last_active_at >= ?{ch_and} GROUP BY challenge",
        (cutoff_ts, *ch_params),
    )
    exp_by_ch = await _by_ch(
        f"SELECT challenge, COUNT(*) as c FROM experiments{ch_where} GROUP BY challenge",
        ch_params,
    )
    hyp_by_ch = await _by_ch(
        f"SELECT challenge, COUNT(*) as c FROM hypotheses{ch_where} GROUP BY challenge",
        ch_params,
    )
    traj_by_ch = await _by_ch(
        f"SELECT challenge, COUNT(*) as c FROM trajectories{ch_where} GROUP BY challenge",
        ch_params,
    )
    # Distinct-agents-who-published per challenge — same source
    # of truth as db.get_challenge_total_agents (experiments table).
    agents_in_ch = await _by_ch(
        "SELECT challenge, COUNT(DISTINCT agent_id) as c "
        f"FROM experiments{ch_where} GROUP BY challenge",
        ch_params,
    )

    return {
        ch: {
            "active_agents": active_by_ch.get(ch, 0),
            "total_experiments": exp_by_ch.get(ch, 0),
            "hypotheses_count": hyp_by_ch.get(ch, 0),
            "total_trajectories": traj_by_ch.get(ch, 0),
            "total_agents_in_challenge": agents_in_ch.get(ch, 0),
        }
        for ch in (
            set(active_by_ch) | set(exp_by_ch) | set(hyp_by_ch)
            | set(traj_by_ch) | set(agents_in_ch)
        )
    }


async def _challenge_best_stats(
    conn, challenge: str, direction: str, challenge_cfg: dict,
) -> tuple[dict | None, float | None, int, float]:
    """Global best + baseline for a challenge, with the derived
    num_instances / improvement_pct the dashboard shows. Returns
    (best_row, baseline, num_instances, improvement_pct)."""
    best = await db.get_global_best(conn, challenge, direction=direction)
    baseline = await get_baseline_score(conn, challenge)
    best_solution_data = best["solution_data"] if best else None
    num_instances = get_num_instances_for(challenge_cfg, best_solution_data)
    best_score = best["score"] if best else None
    imp = (
        improvement_pct(baseline, best_score, direction)
        if baseline is not None and best_score is not None
        else 0
    )
    return best, baseline, num_instances, imp


# ── Periodic stats ──

async def periodic_stats():
    while True:
        await asyncio.sleep(10)
        try:
            cutoff_ts = await inactive_cutoff()
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

                counters = await _challenge_counters(conn, cutoff_ts)

                per_challenge: dict[str, dict] = {}
                for ch in challenges.CHALLENGE_NAMES:
                    direction = await get_direction(ch)
                    cfg = await get_challenge_config_cached(ch)
                    best, baseline, num_instances, imp = (
                        await _challenge_best_stats(conn, ch, direction, cfg)
                    )
                    c = counters.get(ch, {})
                    per_challenge[ch] = {
                        "active_agents": c.get("active_agents", 0),
                        "best_score": best["score"] if best else None,
                        "baseline_score": baseline,
                        "num_instances": num_instances,
                        "improvement_pct": imp,
                        "total_experiments": c.get("total_experiments", 0),
                        "hypotheses_count": c.get("hypotheses_count", 0),
                        "total_trajectories": c.get("total_trajectories", 0),
                        "total_agents_in_challenge": c.get("total_agents_in_challenge", 0),
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
    requested = (req.agent_name or "").strip()
    llm_type = (req.llm_type or "").strip() or None
    # Auto-classify the model tier (frontier/standard) at register. Prefer the
    # structured provider/model when supplied; otherwise parse the llm_type
    # label the client already sends. Tier drives seeding only.
    if req.provider or req.model:
        tier = tiers.classify_tier(req.provider, req.model)
    else:
        tier = tiers.classify_tier_from_label(llm_type)

    def _insert_agent(conn, name):
        return conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, status, llm_type, token, contributor_username, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, name, timestamp, timestamp, "idle", llm_type, agent_token, contributor_username, tier),
        )

    async with db.connect() as conn:
        # Honour the contributor's chosen name when supplied AND not already
        # taken; fall back to the server's auto-generator otherwise (degrade
        # transparently rather than 409 the wizard). Check + INSERT run in ONE
        # immediate transaction so two concurrent registrations of the same
        # name can't both pass the check and then race the UNIQUE(name)
        # constraint into an unhandled 500.
        await conn.execute("BEGIN IMMEDIATE")
        agent_name: str | None = None
        if requested:
            cur = await conn.execute(
                "SELECT 1 FROM agents WHERE name = ?", (requested,),
            )
            if await cur.fetchone() is None:
                agent_name = requested
        if agent_name is None:
            agent_name = generate_agent_name()
        try:
            await _insert_agent(conn, agent_name)
        except sqlite3.IntegrityError:
            # Lost a cross-process race, or the generator replayed a name
            # taken after boot — fall back to a fresh generated name.
            agent_name = generate_agent_name()
            await _insert_agent(conn, agent_name)
        # Keep the in-process generator's used-set in sync with names accepted
        # after boot, so generate_agent_name can never replay them.
        reserve_name(agent_name)
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


@app.get("/api/contributor/me")
async def contributor_me(
    contributor_username: str = Depends(verify_swarm_password),
):
    """Validate a contributor credential pair and describe the swarm.

    The first `/api/contributor/*` endpoint (see
    docs/server-first-onboarding-plan.md): the hosted /join page calls it to
    turn a pasted/clicked invite into "✓ valid invite for <name> — this swarm
    is optimizing <challenge>". Auth (and its rate limiting) is exactly the
    register path's verify_swarm_password, so a revoked or mistyped invite
    fails here the same way registration would.
    """
    config = await get_config_cached()
    return {
        "username": contributor_username,
        "swarm_name": config.get("swarm_name") or "",
        "swarm_type": config.get("swarm_type", "cpu"),
        "active_challenge": config.get("active_challenge") or DEFAULT_CHALLENGE,
        # Public URL of the hosted fleet runner, when the host deployed one —
        # lets the join page offer the zero-install "run in the cloud" tier.
        "runner_url": config.get("runner_url") or "",
    }


# ── Contributor fleet config (server-first onboarding P1) ──
#
# The hosted contributor console authors a fleet plan here; the local runner
# fetches it in --join mode (P2). Stored configs are sanitized fleet.config
# material: whitelisted keys only, raw secrets hard-rejected — LLM keys are
# referenced by env-var NAME (`api_key_env`), never by value.

# Per-agent keys a stored config may carry. Mirrors the fleet-entry fields
# scripts/run_fleet.py materializes into worktrees (_AGENT_CONFIG_KEYS + the
# entry's `name`), MINUS anything secret or locally-owned: `c3_api_key` (raw
# secret), `agent_id`/`agent_name` (runner-persisted identity).
_CONTRIB_AGENT_KEYS = frozenset({
    "name", "provider", "model", "api_base", "api_key_env",
    "compute", "hardware", "c3_hardware", "c3_time", "c3_cloud_provider",
    "c3_no_build", "c3_max_parallel_jobs",
    "log_prompts", "detailed_prompts", "tacit_write", "role", "edit_mode",
    "hpo_min_improvements", "hpo_first_tune_improvements",
    "hpo_num_suggested_configs", "hpo_search_budget", "hpo_seed",
    "cleaner_trigger_chars", "cleaner_target_pct", "cleaner_score_delta_pct",
    "cleaner_cooldown_iters",
})
# Top-level keys: the agents array + the fleet-wide default knobs run_fleet
# inherits into every agent. Credentials (server_url/username/swarm_password)
# are deliberately absent — the runner already has them; storing them here
# would just duplicate secrets.
_CONTRIB_TOP_KEYS = frozenset({
    "agents",
    "hpo_min_improvements", "hpo_first_tune_improvements",
    "hpo_num_suggested_configs", "hpo_search_budget", "hpo_seed",
    "cleaner_trigger_chars", "cleaner_target_pct", "cleaner_score_delta_pct",
    "cleaner_cooldown_iters",
    "c3_max_parallel_jobs",
})
_CONTRIB_MAX_AGENTS = 32
# Agent names become worktree directory names and git branch segments on the
# runner — restrict hard rather than sanitize.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# `api_key_env` must be an environment-variable NAME. A pasted raw key
# ("sk-…") fails this shape check, which is the point.
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def _validate_contributor_config(config: dict) -> None:
    """Raise HTTPException(422) unless `config` is a storable fleet plan."""
    def bad(msg: str):
        raise HTTPException(status_code=422, detail=msg)

    if not isinstance(config, dict):
        bad("config must be an object")
    unknown = set(config) - _CONTRIB_TOP_KEYS
    if unknown:
        bad(f"unknown top-level keys: {sorted(unknown)} — secrets and "
            "credentials must not be stored in the hosted config")
    agents = config.get("agents")
    if not isinstance(agents, list) or not agents:
        bad("config.agents must be a non-empty array")
    if len(agents) > _CONTRIB_MAX_AGENTS:
        bad(f"too many agents (max {_CONTRIB_MAX_AGENTS})")
    seen_names: set[str] = set()
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            bad(f"agents[{i}] must be an object")
        unknown = set(agent) - _CONTRIB_AGENT_KEYS
        if unknown:
            bad(f"agents[{i}] has unsupported keys: {sorted(unknown)} — "
                "raw secrets (c3_api_key, …) are not storable; LLM keys are "
                "referenced by env-var name via api_key_env")
        name = agent.get("name")
        if not isinstance(name, str) or not _AGENT_NAME_RE.match(name):
            bad(f"agents[{i}].name must match {_AGENT_NAME_RE.pattern} "
                "(it becomes a worktree directory / git branch on the runner)")
        if name in seen_names:
            bad(f"duplicate agent name {name!r}")
        seen_names.add(name)
        env_name = agent.get("api_key_env")
        if env_name is not None and (
            not isinstance(env_name, str) or not _ENV_NAME_RE.match(env_name)
        ):
            bad(f"agents[{i}].api_key_env must be an environment-variable "
                "NAME like OPENROUTER_API_KEY — never paste the key itself")
        for key, value in agent.items():
            if isinstance(value, str):
                if len(value) > 200:
                    bad(f"agents[{i}].{key} is too long")
            elif not isinstance(value, (int, float, bool, type(None))):
                bad(f"agents[{i}].{key} must be a JSON scalar")
    for key, value in config.items():
        if key == "agents":
            continue
        if isinstance(value, str):
            if len(value) > 200:
                bad(f"{key} is too long")
        elif not isinstance(value, (int, float, bool, type(None))):
            bad(f"{key} must be a JSON scalar")


@app.get("/api/contributor/config")
async def get_contributor_config(
    contributor_username: str = Depends(verify_swarm_password),
):
    """The caller's stored fleet plan; 404 before the first save (the console
    offers a starter config on 404 rather than treating it as an error)."""
    async with db.connect() as conn:
        row = await db.get_contributor_config(conn, contributor_username)
    if row is None:
        raise HTTPException(status_code=404, detail="No stored config yet")
    try:
        config = json.loads(row["config_json"]) if row["config_json"] else None
    except (ValueError, TypeError):
        config = None
    return {
        "config": config,
        "tacit": row["tacit_text"] or "",
        "updated_at": row["updated_at"],
    }


@app.put("/api/contributor/config")
async def put_contributor_config(
    req: ContributorConfigPut,
    contributor_username: str = Depends(verify_swarm_password),
):
    """Save the caller's fleet plan and/or tacit notes. Partial-update: a
    body with only `config` keeps the stored tacit text, and vice versa."""
    if req.config is None and req.tacit is None:
        raise HTTPException(status_code=422, detail="Provide config and/or tacit")
    config_json: str | None = None
    if req.config is not None:
        _validate_contributor_config(req.config)
        config_json = json.dumps(req.config)
        if len(config_json) > MAX_CONTRIB_CONFIG_LEN:
            raise HTTPException(status_code=422, detail="config too large")
    timestamp = now()
    async with db.connect() as conn:
        existing = await db.get_contributor_config(conn, contributor_username)
        await db.set_contributor_config(
            conn, contributor_username,
            config_json=config_json if req.config is not None
                else (existing or {}).get("config_json"),
            tacit_text=req.tacit if req.tacit is not None
                else (existing or {}).get("tacit_text"),
            updated_at=timestamp,
        )
        await conn.commit()
    return {"saved": True, "updated_at": timestamp}


@app.get("/api/contributor/agents")
async def contributor_agents(
    contributor_username: str = Depends(verify_swarm_password),
):
    """The caller's registered agents — the console's "my agents" strip.
    Same activity window as the dashboard's counters."""
    cutoff = await inactive_cutoff()
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT name, llm_type, status, last_heartbeat, tier "
            "FROM agents WHERE contributor_username = ? ORDER BY name",
            (contributor_username,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    for r in rows:
        r["active"] = bool(r["last_heartbeat"] and r["last_heartbeat"] >= cutoff)
    return {"agents": rows}


@app.get("/api/contributor/agent_defaults")
async def contributor_agent_defaults(
    provider: str | None = None,
    model: str | None = None,
    contributor_username: str = Depends(verify_swarm_password),
):
    """Tier-derived defaults for a provider/model pick — the same rules the
    local wizard applies (scripts/init_fleet.py:_build_agent), computed from
    server/tiers.py so the console can't drift from registration-time
    classification."""
    tier = tiers.classify_tier(provider, model)
    return {
        "tier": tier,
        "role": tiers.role_for_tier(tier),
        "detailed_prompts": tier == "standard",
    }


# The provider catalog the console's agent editor offers. Duplicated from
# scripts/init_fleet.py (the server image is self-contained and cannot import
# scripts/) — scripts/test_provider_catalog_parity.py asserts the two stay
# identical.
_PROVIDERS_PATH = Path(__file__).parent / "providers.json"


@app.get("/api/providers")
async def list_providers():
    try:
        return {"providers": json.loads(_PROVIDERS_PATH.read_text())}
    except (OSError, ValueError):
        return {"providers": []}


@app.post("/api/agents/{agent_id}/rename")
async def rename_agent(
    agent_id: str,
    req: RenameRequest,
    token_agent_id: str = Depends(verify_agent_token),
):
    """Update an existing agent's display name. `agents.name` is the
    single source of truth for an agent's name — leaderboard, messages
    GET, and every event broadcast resolve through it — so this is the
    only operation that affects what the dashboard shows for `agent_id`.

    Returns 403 if the caller's token doesn't resolve to `agent_id`, 404
    if the agent doesn't exist, 409 if `agent_name` collides with another
    agent, 400 if blank. Idempotent when the new name equals the current
    one (no broadcast in that case)."""
    require_token_matches(token_agent_id, agent_id)
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

    # Keep the name generator's used-set in sync so it never replays a
    # custom name adopted after boot.
    reserve_name(requested)

    await manager.broadcast(ws_events.AgentRenamed(
        agent_id=agent_id,
        old_name=old_name,
        new_name=requested,
        timestamp=timestamp,
    ))
    return {"agent_id": agent_id, "agent_name": requested}


@app.post("/api/agents/{agent_id}/heartbeat")
async def heartbeat(
    agent_id: str,
    req: HeartbeatRequest,
    token_agent_id: str = Depends(verify_agent_token),
):
    require_token_matches(token_agent_id, agent_id)
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
    seeded_start: str | None = None,
    token_agent_id: str | None = Depends(optional_agent_token),
):
    """Return current swarm state for the given challenge.

    When `agent_id` is supplied, the caller's X-Agent-Token must resolve to
    that agent (403 otherwise): the agent view mutates state (heartbeat
    bump, trajectory resets that consume inactive-pool seeds, hint
    counters) and returns the agent's private code. The dashboard view
    (no `agent_id`) is public and side-effect-free.

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
    if agent_id is not None:
        require_token_matches(token_agent_id, agent_id)
    challenge = await resolve_challenge(challenge)
    if agent_id is not None:
        return await _agent_state(agent_id, challenge, role, seeded_start)
    return await _dashboard_state(challenge)


async def _ensure_current_program_id(
    conn, agent_id: str, challenge: str, acs: dict | None,
) -> tuple[str, bool]:
    """Return the agent's current program id for this challenge, minting and
    persisting a fresh one when the acs row has none. The second element is
    True when a new id was written (callers running outside a transaction
    commit then). Shared by the agent state view and create_iteration."""
    current_program_id = (acs or {}).get("current_program_id")
    if current_program_id:
        return current_program_id, False
    current_program_id = new_id()
    await db.update_agent_challenge_state(
        conn, agent_id, challenge,
        set_fields={"current_program_id": current_program_id},
    )
    return current_program_id, True


async def _recent_hypotheses(
    conn, challenge: str, limit: int, before: str | None = None,
) -> list[dict]:
    """Newest-first hypotheses for a challenge, joined to the proposing
    agent's current name. Shared by GET /api/hypotheses and the dashboard
    state view's `recent_hypotheses` block (which previously duplicated
    this query). `before` is an optional created_at cursor: only rows
    strictly older than it are returned."""
    where = "h.challenge = ?"
    params: list = [challenge]
    if before:
        where += " AND h.created_at < ?"
        params.append(before)
    params.append(limit)
    cursor = await conn.execute(
        "SELECT h.id, h.title, h.strategy_tag, h.description, "
        "       a.name as agent_name, h.agent_id, h.parent_hypothesis_id, "
        "       h.created_at "
        "FROM hypotheses h JOIN agents a ON a.id = h.agent_id "
        f"WHERE {where} "
        "ORDER BY h.created_at DESC LIMIT ?",
        params,
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _agent_state(
    agent_id: str, challenge: str, role: str | None, seeded_start: str | None = None,
) -> dict:
    """Authenticated per-agent view of /api/state.

    Mutates state: bumps the agent's heartbeat / per-challenge
    last_active_at, may run the trajectory-reset state machine (which
    consumes inactive-pool seeds — see server/trajectory_reset.py), and
    increments hint counters. Returns the agent's private best code.
    """
    config = await get_config_cached()
    direction = await get_direction(challenge)
    challenge_cfg = await get_challenge_config_cached(challenge)

    async with db.connect() as conn:
        global_best = await db.get_global_best(conn, challenge, direction=direction)
        cutoff_ts = await inactive_cutoff()
        # Swarm counters are read BEFORE the heartbeat/last_active_at bump
        # below (same ordering as the original fused handler), so an
        # agent's very first poll doesn't count itself as active yet.
        counters = (
            await _challenge_counters(conn, cutoff_ts, challenge=challenge)
        ).get(challenge, {})
        # active = agents recently active on THIS challenge
        active = counters.get("active_agents", 0)
        total_exp = counters.get("total_experiments", 0)
        total_hyp = counters.get("hypotheses_count", 0)
        total_agents = await db.get_agent_count(conn, active_only=False)

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
        # Like role, `seeded_start` is contributor-owned and reported each
        # poll: 'true'/'false' override the tier/role seeding policy in
        # seed_for_agent; anything else (or absent) means "auto".
        agent_seeded = {"true": True, "false": False}.get(
            (seeded_start or "").strip().lower()
        )

        # ── Trajectory reset on stagnation_limit ──
        # The state machine itself (deactivate → adopt/fresh-start →
        # deposit → upsert/clear trajectory_bests) lives in
        # server/trajectory_reset.py and runs in its own BEGIN IMMEDIATE
        # transaction; it re-checks the stagnation condition under the
        # write lock, so concurrent calls can't double-reset.
        trajectory_reset = None
        # How this iteration's starting code was chosen ('seed' | 'peer' |
        # 'stub'), whenever it came from seed_for_agent — on a fresh reset
        # OR a true cold start. Surfaced in state so the client can log
        # whether a standard-tier agent actually got a seed vs. the bare
        # stub. None when the agent continued its own existing best.
        seed_start = None
        reset = None
        stagnation_limit = swarm_setting(config, "stagnation_limit")
        negative_limit = swarm_setting(config, "negative_trajectory_limit")
        stagnated = (stagnation_limit > 0 and runs_since >= stagnation_limit
                     and traj_best is not None)

        # ── … or cull a trajectory that never turned positive ──
        # A line inching upward while still below zero resets
        # runs_since_improvement on every small win, so it never trips
        # stagnation_limit — yet deposit_inactive refuses negative scores,
        # so nothing harvestable can ever come out of it. After
        # `negative_trajectory_limit` edits without crossing zero, treat it
        # exactly like a stagnation trip. This is only the cheap pre-check
        # (mirroring the stagnation one above); the machine re-checks both
        # conditions under its write lock.
        negative_cull = False
        if not stagnated and traj_best is not None and negative_limit > 0:
            cull_traj_id = acs["current_trajectory_id"] if acs else None
            if cull_traj_id and not db.is_better(direction, traj_best["score"], 0.0):
                traj_row = await db.get_trajectory(conn, cull_traj_id)
                if traj_row and (traj_row["num_edits"] or 0) >= negative_limit:
                    negative_cull = True

        if stagnated or negative_cull:
            reset = await maybe_reset_trajectory(
                conn, agent_id=agent_id, challenge=challenge,
                direction=direction, cutoff_ts=cutoff_ts,
                stagnation_limit=stagnation_limit,
                negative_trajectory_limit=negative_limit,
                agent_tier=agent_tier, agent_role=agent_role,
                seed_fn=functools.partial(seed_for_agent, seeded=agent_seeded),
                timestamp=now(),
            )
            if reset is None:
                # Lost the reset race: a concurrent /api/state call already
                # reset this trajectory. Re-read so we serve the post-reset
                # state instead of the stale pre-reset snapshot.
                traj_best = await db.get_trajectory_best(conn, agent_id, challenge)
                acs = await db.get_agent_challenge_state(conn, agent_id, challenge)
                runs_since = acs["runs_since_improvement"] if acs else 0
        if reset is not None:
            trajectory_reset = reset.reset_info
            seed_start = reset.seed_start
            traj_best = reset.traj_best
            current_trajectory_best = reset.current_trajectory_best
            traj_best_experiment_id = reset.traj_best_experiment_id
            traj_best_code = reset.algorithm_code
            traj_best_kernel_code = reset.kernel_code
            traj_best_files = reset.algorithm_files
            runs_since = 0
            agent_name = await get_agent_name(conn, agent_id)
            # Broadcast only after the machine's transaction committed —
            # same commit-before-broadcast ordering as before the split.
            await manager.broadcast(ws_events.TrajectoryReset(
                challenge=challenge,
                agent_name=agent_name,
                agent_id=agent_id,
                reset_type=trajectory_reset["type"],
                timestamp=reset.timestamp,
            ))
            # Re-read acs so subsequent reads see the reset state.
            acs = await db.get_agent_challenge_state(conn, agent_id, challenge)
        else:
            if traj_best:
                traj_best_code = traj_best["algorithm_code"]
                traj_best_kernel_code = traj_best.get("kernel_code")
                traj_best_files = _row_files(traj_best)
            else:
                traj_best_code, traj_best_kernel_code, traj_best_files, _start = await seed_for_agent(
                    conn, agent_id, challenge, agent_tier, agent_role,
                    direction=direction, cutoff_ts=cutoff_ts, seeded=agent_seeded,
                )
                seed_start = _start
            current_trajectory_best = traj_best["score"] if traj_best else None
            traj_best_experiment_id = traj_best["experiment_id"] if traj_best else None

        # ── Program ID management (per-(agent, challenge)) ──
        current_program_id, program_id_created = await _ensure_current_program_id(
            conn, agent_id, challenge, acs,
        )
        if program_id_created:
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
            pending_source_traj = None
            if all_bests:
                chosen = random.choice(all_bests)
                inspiration_code = chosen["algorithm_code"]
                inspiration_kernel_code = chosen.get("kernel_code")
                inspiration_agent_name = await get_agent_name(
                    conn, chosen["agent_id"]
                )
                if stagnation_hint == "inspiration":
                    pending_source = chosen["agent_id"]
                    # Capture the source trajectory NOW, while it's known to
                    # be correct. The matrix reads this instead of looking
                    # up the source's *current* trajectory_bests later (that
                    # row is wiped when the source agent stagnates).
                    pending_source_traj = chosen.get("trajectory_id")
            # Stash the hint (and inspiration source) so the next
            # iteration this agent publishes can be tagged with them.
            # /api/iterations reads + clears them atomically.
            await db.update_agent_challenge_state(
                conn, agent_id, challenge,
                set_fields={
                    "pending_hint": stagnation_hint,
                    "pending_inspiration_source": pending_source,
                    "pending_inspiration_source_trajectory": pending_source_traj,
                },
            )
            await conn.commit()

        best_solution_data = traj_best["solution_data"] if traj_best else None
        num_instances = get_num_instances_for(challenge_cfg, best_solution_data)
        leaderboard = await db.compute_leaderboard(
            conn, challenge, await inactive_cutoff(), direction=direction,
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
        # Winning hyperparameter config of the current trajectory best (when
        # it was tuned), surfaced so the loop / dashboard can recover the
        # config the best score was achieved with. None when scored at
        # in-code defaults or on a freshly reset/seeded trajectory.
        traj_best_hyperparameters = None
        if not trajectory_reset and traj_best and traj_best.get("hyperparameters"):
            try:
                traj_best_hyperparameters = json.loads(traj_best["hyperparameters"])
            except (json.JSONDecodeError, TypeError):
                traj_best_hyperparameters = None
        # Recent improvement scores for the active trajectory, keyed by its
        # (adoption-preserved) trajectory_id, so the HPO gate survives
        # restarts and adoption out of the inactive pool. Fresh starts get a
        # new id with no experiments → []; adopted trajectories inherit the
        # original id's real history.
        active_trajectory_id = (acs or {}).get("current_trajectory_id")
        improvement_scores = await db.get_recent_improvement_scores(
            conn, active_trajectory_id, _IMPROVEMENT_HISTORY_LIMIT
        )
        # Whether this trajectory has already been tuned once. The HPO gate
        # auto-fires the first time a mature trajectory is eligible, then
        # falls back to the improvement band.
        has_tuned = await db.trajectory_has_tuned(conn, active_trajectory_id)
        resp = {
            "challenge": challenge,
            "is_gpu": is_gpu,
            "agent_name": self_agent_name,
            "best_score": global_best_score,
            "best_algorithm_code": traj_best_code,
            "best_algorithm_files": traj_best_files,
            "best_experiment_id": traj_best_experiment_id,
            "best_hyperparameters": traj_best_hyperparameters,
            "improvement_scores": improvement_scores,
            "has_tuned": has_tuned,
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


async def _dashboard_state(challenge: str) -> dict:
    """Public dashboard view of /api/state (no agent_id): read-only swarm
    stats, recent experiments/hypotheses, and the global-best code. No
    side effects."""
    direction = await get_direction(challenge)
    challenge_cfg = await get_challenge_config_cached(challenge)

    async with db.connect() as conn:
        cutoff_ts = await inactive_cutoff()
        counters = (
            await _challenge_counters(conn, cutoff_ts, challenge=challenge)
        ).get(challenge, {})
        # active = agents recently active on THIS challenge
        active = counters.get("active_agents", 0)
        total_agents = await db.get_agent_count(conn, active_only=False)
        total_agents_in_challenge = counters.get("total_agents_in_challenge", 0)
        total_exp = counters.get("total_experiments", 0)
        total_hyp = counters.get("hypotheses_count", 0)
        total_traj = counters.get("total_trajectories", 0)
        global_best, baseline, num_instances, overall_imp = (
            await _challenge_best_stats(conn, challenge, direction, challenge_cfg)
        )

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

        recent_hypotheses = await _recent_hypotheses(conn, challenge, limit=30)

        served = global_best
        leaderboard = await db.compute_leaderboard(
            conn, challenge, await inactive_cutoff(), direction=direction,
        )

    global_best_score = global_best["score"] if global_best else None

    _initial_algo = (None, None) if served else await load_initial_algorithm(challenge)
    return {
        "challenge": challenge,
        "baseline_score": baseline,
        "best_score": global_best_score,
        "improvement_pct": overall_imp,
        "best_algorithm_code": served["algorithm_code"] if served else _initial_algo[0],
        "best_algorithm_files": _row_files(served) if served else None,
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
#
# POST /api/iterations is the swarm's hottest write path. The route handler
# is an orchestrator; each phase lives in a named helper directly below it.


@dataclass
class _IterationPayload:
    """Pre-encoded JSON columns + token accounting for one publish."""
    solution_data_json: str | None
    track_scores_json: str | None
    challenge_metrics_json: str | None
    algorithm_files_json: str | None
    hyperparameters_json: str | None
    fingerprint: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float


def _encode_iteration_payload(req: IterationCreate) -> _IterationPayload:
    return _IterationPayload(
        solution_data_json=json.dumps(req.solution_data) if req.solution_data else None,
        track_scores_json=json.dumps(req.track_scores) if req.track_scores else None,
        challenge_metrics_json=(
            json.dumps(req.challenge_metrics) if req.challenge_metrics else None
        ),
        algorithm_files_json=_files_json(req.algorithm_files),
        hyperparameters_json=(
            json.dumps(req.hyperparameters) if req.hyperparameters else None
        ),
        fingerprint=fingerprint(req.title, req.strategy_tag),
        input_tokens=req.input_tokens or 0,
        output_tokens=req.output_tokens or 0,
        estimated_cost=req.estimated_cost or 0.0,
    )


@dataclass
class _IterationVerdict:
    """How one publish scored against the global and trajectory bests."""
    prev_best: dict | None
    prev_trajectory_best: dict | None
    baseline: float | None
    is_new_best: bool
    beats_trajectory_best: bool
    is_refactor: bool
    hyp_status: str
    target_best_experiment_id: str | None
    delta_vs_best_pct: float | None
    delta_vs_trajectory_best_pct: float | None


async def _evaluate_iteration(
    conn, req: IterationCreate, challenge: str, direction: str,
) -> _IterationVerdict:
    """Scoring/floor phase: compare the published score against the global
    best and the agent's trajectory best (both feasibility-gated) and
    classify the iteration (improvement / refactor / failed)."""
    prev_best = await db.get_global_best(conn, challenge, direction=direction)
    prev_trajectory_best = await db.get_trajectory_best(conn, req.agent_id, challenge)
    baseline = await get_baseline_score(conn, challenge)

    # Both "best" comparisons are gated on feasibility. Without this gate an
    # infeasible run can outrank a feasible one numerically: the infeasible
    # aggregate is a fixed floor (benchmark.INFEASIBLE_QUALITY), while a
    # feasible-but-below-baseline score can sit *below* that floor (e.g. the
    # neuralnet baseline is ~-2.29M, well under the old -1M infeasible
    # floor). When that happened, an infeasible cheat edit registered as
    # "beats trajectory best", became the trajectory anchor, and every later
    # feasible recovery — scoring below the floor — was then rejected as
    # "not an improvement". The trajectory was pinned at the infeasible floor
    # forever (observed: 80+ flat edits). Feasibility is a hard precondition
    # for any score to count as a best; benchmark.py also lowers the floor
    # below the feasible clamp as defense-in-depth.
    is_new_best = req.feasible and (
        prev_best is None or db.is_better(direction, req.score, prev_best["score"])
    )
    beats_trajectory_best = req.feasible and (
        prev_trajectory_best is None
        or db.is_better(direction, req.score, prev_trajectory_best["score"])
    )

    # Refactor path (docs/cleaner-agent-plan.md): a behavior-preserving
    # bloat reduction the client has already benchmarked and delta-gated.
    # It swaps the trajectory-best CODE while KEEPING the recorded score
    # (a −2% refactor must not lower the bar the next mutation has to
    # beat), and counts as neither improvement nor stagnation. Only
    # meaningful when there IS a parent and the refactor didn't beat it —
    # a refactor that beats outright is just a normal improvement.
    is_refactor = (
        req.iteration_type == "refactor"
        and req.feasible
        and prev_trajectory_best is not None
        and not beats_trajectory_best
    )

    hyp_status = (
        "refactor" if is_refactor
        else "succeeded" if beats_trajectory_best else "failed"
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

    return _IterationVerdict(
        prev_best=prev_best,
        prev_trajectory_best=prev_trajectory_best,
        baseline=baseline,
        is_new_best=is_new_best,
        beats_trajectory_best=beats_trajectory_best,
        is_refactor=is_refactor,
        hyp_status=hyp_status,
        target_best_experiment_id=(
            prev_trajectory_best["experiment_id"] if prev_trajectory_best else None
        ),
        delta_vs_best_pct=delta_vs_best_pct,
        delta_vs_trajectory_best_pct=delta_vs_trajectory_best_pct,
    )


async def _ensure_trajectory(
    conn, agent_id: str, challenge: str, acs: dict | None, timestamp: str,
    *, initial_score: float | None,
) -> str:
    """Trajectory-tracking phase (per-(agent, challenge)): reuse the agent's
    current trajectory, or create one on its first publish."""
    trajectory_id = (acs or {}).get("current_trajectory_id")
    if not trajectory_id:
        trajectory_id = new_id()
        await db.create_trajectory(
            conn, trajectory_id, challenge, timestamp,
            current_score=initial_score,
        )
        await db.update_agent_challenge_state(
            conn, agent_id, challenge,
            set_fields={"current_trajectory_id": trajectory_id},
        )
        await db.increment_agent_challenge_counters(
            conn, agent_id, challenge, num_trajectories_inc=1,
        )
    return trajectory_id


async def _record_experiment(
    conn, req: IterationCreate, *, exp_id: str, hyp_id: str, challenge: str,
    trajectory_id: str, acs: dict | None, verdict: _IterationVerdict,
    enc: _IterationPayload, timestamp: str,
) -> None:
    """Persist the experiments row, tagged with (and consuming) any pending
    stagnation hint."""
    # Hint that drove this iteration (set on the prior /api/state call
    # when the agent was stagnating). We read + clear atomically so the
    # next iteration only carries a hint if the server hands one out
    # again.
    received_hint = (acs or {}).get("pending_hint")
    inspiration_source_id = (acs or {}).get("pending_inspiration_source")
    inspiration_source_trajectory_id = (acs or {}).get(
        "pending_inspiration_source_trajectory"
    )

    await conn.execute(
        """INSERT INTO experiments
           (id, agent_id, challenge, hypothesis_id, algorithm_code, kernel_code,
            algorithm_files, hyperparameters,
            score, default_score, feasible,
            challenge_metrics, notes, solution_data, track_scores,
            delta_vs_best_pct, delta_vs_trajectory_best_pct, beats_trajectory_best,
            trajectory_id, received_hint, inspiration_source_id,
            inspiration_source_trajectory_id,
            input_tokens, output_tokens, estimated_cost, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (exp_id, req.agent_id, challenge, hyp_id, req.algorithm_code, req.kernel_code,
         enc.algorithm_files_json, enc.hyperparameters_json,
         req.score,
         # default_score falls back to the published score for untuned
         # iterations (where they are equal) and legacy clients that omit it.
         req.default_score if req.default_score is not None else req.score,
         1 if req.feasible else 0, enc.challenge_metrics_json,
         req.notes, enc.solution_data_json, enc.track_scores_json,
         verdict.delta_vs_best_pct, verdict.delta_vs_trajectory_best_pct,
         1 if verdict.beats_trajectory_best else 0,
         trajectory_id, received_hint, inspiration_source_id,
         inspiration_source_trajectory_id,
         enc.input_tokens, enc.output_tokens, enc.estimated_cost, timestamp),
    )

    if received_hint is not None:
        await db.update_agent_challenge_state(
            conn, req.agent_id, challenge,
            set_fields={
                "pending_hint": None,
                "pending_inspiration_source": None,
                "pending_inspiration_source_trajectory": None,
            },
        )


async def _apply_iteration_outcome(
    conn, req: IterationCreate, *, challenge: str, direction: str,
    verdict: _IterationVerdict, exp_id: str, trajectory_id: str,
    enc: _IterationPayload, timestamp: str,
) -> None:
    """Trajectory-best / counter bookkeeping for the scored iteration:
    momentum update, then the improvement / refactor / failed branch."""
    await db.update_trajectory_after_edit(
        conn, trajectory_id, verdict.beats_trajectory_best,
        new_score=req.score if verdict.beats_trajectory_best else None,
    )

    # Personal best-ever is a record of what THIS agent actually achieved
    # with a mutation it made — it is independent of the trajectory floor.
    # It must update on every feasible publish (even one that scores below
    # an inherited trajectory best), and must never be raised by an
    # infeasible run or by the adopted floor an agent is handed on pickup.
    # increment_agent_challenge_counters applies a monotonic CASE guard, so
    # passing the score on every feasible run only ever ratchets it up.
    personal_best_candidate = req.score if req.feasible else None

    if verdict.beats_trajectory_best:
        new_program_id = new_id()
        await db.increment_agent_challenge_counters(
            conn, req.agent_id, challenge,
            runs=1,
            improvements=1,
            runs_since_improvement_reset=True,
            best_ever_score=personal_best_candidate,
            direction=direction,
            input_tokens=enc.input_tokens,
            output_tokens=enc.output_tokens,
            estimated_cost=enc.estimated_cost,
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
            challenge_metrics=enc.challenge_metrics_json,
            solution_data=enc.solution_data_json,
            updated_at=timestamp, trajectory_id=trajectory_id,
            track_scores=enc.track_scores_json,
            kernel_code=req.kernel_code,
            algorithm_files=enc.algorithm_files_json,
            hyperparameters=enc.hyperparameters_json,
        )
    elif verdict.is_refactor:
        # Neither improvement (no momentum/HPO-band credit) nor
        # stagnation (runs_since_improvement untouched): pure
        # bookkeeping. Swap in the lean code at the PARENT's score.
        await db.increment_agent_challenge_counters(
            conn, req.agent_id, challenge,
            runs=1,
            best_ever_score=personal_best_candidate,
            direction=direction,
            input_tokens=enc.input_tokens,
            output_tokens=enc.output_tokens,
            estimated_cost=enc.estimated_cost,
        )
        await db.upsert_trajectory_best(
            conn, agent_id=req.agent_id, challenge=challenge,
            experiment_id=exp_id,
            algorithm_code=req.algorithm_code,
            score=verdict.prev_trajectory_best["score"],
            feasible=req.feasible,
            challenge_metrics=enc.challenge_metrics_json,
            solution_data=enc.solution_data_json,
            updated_at=timestamp, trajectory_id=trajectory_id,
            track_scores=enc.track_scores_json,
            kernel_code=req.kernel_code,
            algorithm_files=enc.algorithm_files_json,
            # Preserve the parent's tuned config unless the client sent
            # one: the refactor kept the Map plumbing, so the winning
            # hyperparameters still apply to the lean code.
            hyperparameters=(
                enc.hyperparameters_json
                if enc.hyperparameters_json is not None
                else verdict.prev_trajectory_best.get("hyperparameters")
            ),
        )
    else:
        await db.increment_agent_challenge_counters(
            conn, req.agent_id, challenge,
            runs=1,
            runs_since_improvement_inc=1,
            best_ever_score=personal_best_candidate,
            direction=direction,
            input_tokens=enc.input_tokens,
            output_tokens=enc.output_tokens,
            estimated_cost=enc.estimated_cost,
        )


async def _maybe_harvest_seed(
    conn, req: IterationCreate, challenge: str, timestamp: str,
) -> None:
    # Auto-harvest into the seed pool: a frontier agent's feasible, SIMPLE,
    # and structurally-NOVEL algorithm becomes a launch point for other
    # agents. Diversity is by code similarity (server/seed_diversity.py), not
    # strategy tags; the pool is capped at K and, when full, the most
    # REDUNDANT seed is evicted (never the lowest-scoring) so seeds stay
    # simple and sticky. strategy_tag is kept only as a display label.
    if not (req.feasible and req.algorithm_code.strip()):
        return
    if (await db.get_agent_tier(conn, req.agent_id)) != "frontier":
        return
    swarm_cfg = await get_config_cached()
    existing = await db.list_seeds(conn, challenge)
    decision = seed_diversity.decide_admission(
        req.algorithm_code,
        [s["algorithm_code"] for s in existing],
        pool_size=swarm_setting(swarm_cfg, "seed_pool_size"),
        similarity_threshold=swarm_setting_float(
            swarm_cfg, "seed_similarity_threshold"),
        max_loc=swarm_setting(swarm_cfg, "seed_max_loc"),
    )
    if decision.admit:
        if decision.evict_index is not None:
            await db.evict_seed(
                conn, existing[decision.evict_index]["id"])
        await db.insert_seed(
            conn, challenge, req.strategy_tag, req.algorithm_code,
            created_at=timestamp, source="harvested", score=req.score,
            feasible=True, kernel_code=req.kernel_code,
            origin_agent_id=req.agent_id,
            algorithm_files=_files_json(req.algorithm_files),
        )


async def _broadcast_iteration(
    *, challenge: str, challenge_cfg: dict, direction: str,
    req: IterationCreate, exp_id: str, hyp_id: str, agent_name: str,
    verdict: _IterationVerdict, leaderboard: list, timestamp: str,
) -> None:
    """WS fan-out for a published iteration. Runs AFTER the transaction
    committed — the dashboard must never see an event for a row that could
    still roll back."""
    effective_solution_data = req.solution_data or (
        verdict.prev_best["solution_data"] if verdict.prev_best else None
    )
    num_instances = get_num_instances_for(challenge_cfg, effective_solution_data)
    imp = (
        improvement_pct(verdict.baseline, req.score, direction)
        if verdict.baseline is not None else 0.0
    )
    incremental_pct = verdict.delta_vs_best_pct if verdict.is_new_best else None

    await manager.broadcast(ws_events.ExperimentPublished(
        challenge=challenge,
        experiment_id=exp_id,
        agent_name=agent_name,
        agent_id=req.agent_id,
        score=req.score,
        feasible=req.feasible,
        improvement_pct=imp,
        delta_vs_best_pct=verdict.delta_vs_best_pct,
        beats_trajectory_best=verdict.beats_trajectory_best,
        delta_vs_trajectory_best_pct=verdict.delta_vs_trajectory_best_pct,
        num_instances=num_instances,
        is_new_best=verdict.is_new_best,
        hypothesis_id=hyp_id,
        strategy_tag=req.strategy_tag,
        title=req.title,
        notes=req.notes or "",
        track_scores=req.track_scores,
        timestamp=timestamp,
    ))

    if verdict.is_new_best:
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


@app.post("/api/iterations", response_model=IterationResponse)
async def create_iteration(
    req: IterationCreate,
    token_agent_id: str = Depends(verify_agent_token),
):
    """One published iteration (unified hypothesis + experiment).

    Orchestrates the phases (helpers above): validate → score against the
    global/trajectory bests → record hypothesis + experiment → trajectory /
    counter bookkeeping → maybe harvest a seed → commit → broadcast.
    """
    require_token_matches(token_agent_id, req.agent_id)
    challenge = await resolve_challenge(req.challenge)
    direction = await get_direction(challenge)
    challenge_cfg = await get_challenge_config_cached(challenge)
    exp_id = new_id()
    hyp_id = new_id()
    timestamp = now()
    enc = _encode_iteration_payload(req)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")

        # FKs ARE enforced (db.connect() sets PRAGMA foreign_keys=ON), but a
        # violation would only surface as an opaque IntegrityError 500 midway
        # through the transaction. Pre-check the agent row so an unregistered
        # agent_id gets a clear 404 up front instead.
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

        verdict = await _evaluate_iteration(conn, req, challenge, direction)

        # ── Program ID: tag hypothesis with current program (per-(agent, challenge)) ──
        acs = await db.get_agent_challenge_state(conn, req.agent_id, challenge)
        current_program_id, _ = await _ensure_current_program_id(
            conn, req.agent_id, challenge, acs,
        )

        await conn.execute(
            """INSERT INTO hypotheses
               (id, agent_id, challenge, title, description, strategy_tag, status,
                fingerprint, target_best_experiment_id, program_id, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hyp_id, req.agent_id, challenge, req.title, req.description,
             req.strategy_tag, verdict.hyp_status, enc.fingerprint,
             verdict.target_best_experiment_id, current_program_id, req.role,
             timestamp),
        )

        # ── Trajectory tracking (per-(agent, challenge)) ──
        trajectory_id = await _ensure_trajectory(
            conn, req.agent_id, challenge, acs, timestamp,
            initial_score=req.score if verdict.beats_trajectory_best else None,
        )

        await _record_experiment(
            conn, req, exp_id=exp_id, hyp_id=hyp_id, challenge=challenge,
            trajectory_id=trajectory_id, acs=acs, verdict=verdict, enc=enc,
            timestamp=timestamp,
        )

        await _apply_iteration_outcome(
            conn, req, challenge=challenge, direction=direction,
            verdict=verdict, exp_id=exp_id, trajectory_id=trajectory_id,
            enc=enc, timestamp=timestamp,
        )

        agent_name = await get_agent_name(conn, req.agent_id)

        if verdict.is_new_best:
            await conn.execute(
                """INSERT INTO best_history
                   (experiment_id, agent_id, challenge, agent_name, score, solution_data, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (exp_id, req.agent_id, challenge, agent_name, req.score,
                 enc.solution_data_json, timestamp),
            )

        # This agent got a result through the benchmark (feasible or not), so
        # it has "ever benchmarked" on this challenge. Agents that never reach
        # here keep ever_benchmarked=0 — a quiet signal, no feed message.
        await db.update_agent_challenge_state(
            conn, req.agent_id, challenge, set_fields={"ever_benchmarked": 1},
        )

        await _maybe_harvest_seed(conn, req, challenge, timestamp)

        await conn.commit()

        # Pull updated counters from the per-challenge row.
        acs = await db.get_agent_challenge_state(conn, req.agent_id, challenge)
        agent_info = {
            "experiments_completed": acs["experiments_completed"] if acs else 0,
            "improvements": acs["improvements"] if acs else 0,
            "runs_since_improvement": acs["runs_since_improvement"] if acs else 0,
        }
        leaderboard = await db.compute_leaderboard(
            conn, challenge, await inactive_cutoff(), direction=direction,
        )
        rank = next(
            (e["rank"] for e in leaderboard if e["agent_id"] == req.agent_id),
            0,
        )

    await _broadcast_iteration(
        challenge=challenge, challenge_cfg=challenge_cfg, direction=direction,
        req=req, exp_id=exp_id, hyp_id=hyp_id, agent_name=agent_name,
        verdict=verdict, leaderboard=leaderboard, timestamp=timestamp,
    )

    return IterationResponse(
        experiment_id=exp_id,
        hypothesis_id=hyp_id,
        is_new_best=verdict.is_new_best,
        beats_trajectory_best=verdict.beats_trajectory_best,
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
            conn, challenge, await inactive_cutoff(), direction=direction,
        )
    return {"challenge": challenge, "updated_at": now(), "entries": leaderboard}


# ── Messages (chat feed) ──

@app.post("/api/messages")
async def create_message(
    req: MessageCreate,
    token_agent_id: str = Depends(verify_agent_token),
):
    require_token_matches(token_agent_id, req.agent_id)
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
    """Recent hypotheses for the challenge, newest-first. Same query as the
    `recent_hypotheses` block of /api/state (shared via _recent_hypotheses)
    but paginated via an optional `before` (created_at) cursor so the Ideas
    research feed can page backwards through history that scrolled off its
    in-memory buffer."""
    challenge = await resolve_challenge(challenge)
    limit = max(1, min(limit, 200))
    async with db.connect() as conn:
        rows = await _recent_hypotheses(conn, challenge, limit, before=before)
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
    trajectory_id; the source trajectory is read from
    ``experiments.inspiration_source_trajectory_id`` — captured when the hint
    was issued, so it survives the source agent's later stagnation resets.

    Legacy rows (published before that column existed) have it NULL; for those
    we fall back to the old best-effort reconstruction via the source agent's
    *current* trajectory_bests row, via a LEFT JOIN so the row is never dropped
    when no such best exists. Previously this was an INNER JOIN, which silently
    discarded every event whose source agent's trajectory_bests had been wiped
    — emptying the matrix as trajectories churned.
    """
    challenge = await resolve_challenge(challenge)
    async with db.connect() as conn:
        cursor = await conn.execute(
            """SELECT e.trajectory_id        AS recv_traj,
                      COALESCE(e.inspiration_source_trajectory_id,
                               ab_src.trajectory_id) AS src_traj,
                      a_recv.name            AS recv_agent,
                      a_src.name             AS src_agent,
                      COUNT(*)               AS cnt
               FROM experiments e
               JOIN agents a_recv ON a_recv.id = e.agent_id
               JOIN agents a_src  ON a_src.id  = e.inspiration_source_id
               LEFT JOIN trajectory_bests ab_src
                 ON ab_src.agent_id  = e.inspiration_source_id
                AND ab_src.challenge = e.challenge
              WHERE e.challenge = ?
                AND e.received_hint = 'inspiration'
                AND e.inspiration_source_id IS NOT NULL
                AND e.trajectory_id IS NOT NULL
              GROUP BY e.trajectory_id, src_traj""",
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

        code_col = ", e.algorithm_code, e.kernel_code" if include_code else ""
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
            d["kernel_code"] = r["kernel_code"]
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

        code_col = ", e.algorithm_code, e.kernel_code" if include_code else ""
        cursor = await conn.execute(
            f"""SELECT e.id, e.trajectory_id, e.agent_id, a.name AS agent_name,
                       e.score, e.feasible, e.beats_trajectory_best, e.notes,
                       e.created_at, h.title, h.description, h.strategy_tag, h.role
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
            "role": r["role"],
            "created_at": r["created_at"],
        }
        if include_code:
            d["algorithm_code"] = r["algorithm_code"]
            # GPU challenges store the CUDA source separately in kernel_code;
            # CPU challenges leave it NULL (serialized as null). Surfaced under
            # the same include_code flag so one fetch returns the full pair.
            d["kernel_code"] = r["kernel_code"]
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


@app.post("/api/admin/seed_inactive")
async def admin_seed_inactive(req: AdminSeedInactive):
    """Insert an externally-sourced algorithm into the inactive_algorithms
    pool. The next stagnated agent on this challenge that does NOT qualify
    for a fresh start (i.e. inactive pool is non-empty AND n_trajectories^1.5
    >= total_deactivations) picks it up via the existing `adopted_inactive`
    branch in server.py — at which point it is removed from the pool
    (consume-once semantics).

    Supports every challenge, single- or multi-file: `algorithm_code` is the
    entry file and `algorithm_files` (when present) carries the full map —
    multiple `.rs` modules and multiple `.cu` kernels, names preserved. The
    Pydantic `ChallengeName` Literal already rejects unknown challenge names,
    so no explicit allowlist is needed here."""
    await verify_admin(req)
    if not req.algorithm_code.strip():
        raise HTTPException(status_code=400, detail="algorithm_code is empty")
    timestamp = now()
    async with db.connect() as conn:
        agent_id = await db.ensure_synthetic_agent(
            conn, req.source_label, timestamp,
        )
        # Idempotency guard: if this source already has an unconsumed seed for
        # the challenge, skip — re-running `setup.py create` must not pile up
        # duplicate mainnet seeds. Consume-once means an adopted seed leaves no
        # row, so a later create still re-seeds.
        existing = await db.count_inactive_from_agent(
            conn, agent_id, req.challenge,
        )
        if existing:
            return {
                "seeded": False,
                "challenge": req.challenge,
                "reason": "already_seeded",
                "source": req.source_label,
            }
        inactive_id = await db.deposit_inactive(
            conn, agent_id, req.challenge,
            req.algorithm_code, None, timestamp,
            kernel_code=req.kernel_code,
            algorithm_files=_files_json(req.algorithm_files),
        )
        await conn.commit()
    return {
        "seeded": True,
        "challenge": req.challenge,
        "inactive_id": inactive_id,
        "source": req.source_label,
    }


@app.post("/api/admin/clear_inactive")
async def admin_clear_inactive(req: AdminClearInactive):
    """Empty the inactive-pool for a challenge (optionally keeping one source),
    so agents reliably adopt a specific seed on their next reset instead of a
    diluting mix. Point-in-time only — the pool refills as agents stagnate."""
    await verify_admin(req)
    async with db.connect() as conn:
        keep_agent_id = None
        if req.keep_source_label:
            row = await (await conn.execute(
                "SELECT id FROM agents WHERE name = ?", (req.keep_source_label,),
            )).fetchone()
            keep_agent_id = row["id"] if row else None
        deleted = await db.clear_inactive_pool(conn, req.challenge, keep_agent_id)
        await conn.commit()
    return {
        "cleared": True,
        "challenge": req.challenge,
        "deleted": deleted,
        "kept_source": req.keep_source_label,
    }


@app.post("/api/admin/seed_pool")
async def admin_seed_pool(req: AdminSeedPool):
    """Deposit a host-authored seed algorithm into `seed_pool` — an UPSERT
    keyed by (challenge, strategy_tag): identical re-deposits are no-ops
    (idempotent `setup.py create` re-runs) and changed code REPLACES the pool
    copy (an edited seeds/<tag>.rs propagates on the next create). Harvested
    seeds are never touched. `seeded` stays in the response for older
    hostadmin clients; `action` says what actually happened."""
    await verify_admin(req)
    if not req.algorithm_code.strip():
        raise HTTPException(status_code=400, detail="algorithm_code is empty")
    timestamp = now()
    async with db.connect() as conn:
        action = await db.upsert_authored_seed(
            conn, req.challenge, req.strategy_tag, req.algorithm_code,
            created_at=timestamp, score=req.score,
            kernel_code=req.kernel_code,
        )
        await conn.commit()
    return {
        "seeded": action != "unchanged",
        "action": action,
        "challenge": req.challenge,
        "strategy_tag": req.strategy_tag,
    }


@app.post("/api/admin/seeds")
async def admin_list_seeds(req: AdminSeedsQuery):
    """Read-only: every seed_pool row for a challenge (feasible or not) as
    metadata — tag, source, score, feasibility, code sizes, provenance. Code
    bodies are omitted (they can run to megabytes); the point is to let the
    host see at a glance whether the pool is populated, instead of inferring
    it from agents' start-source log lines."""
    await verify_admin(req)
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT id, strategy_tag, source, score, feasible, origin_agent_id, "
            "       created_at, LENGTH(algorithm_code) AS code_chars, "
            "       LENGTH(COALESCE(kernel_code, '')) AS kernel_chars, "
            "       (algorithm_files IS NOT NULL) AS multi_file "
            "FROM seed_pool WHERE challenge = ? ORDER BY strategy_tag, id",
            (req.challenge,),
        )
        seeds = [dict(r) for r in await cursor.fetchall()]
    return {"challenge": req.challenge, "count": len(seeds), "seeds": seeds}


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
    cutoff = await inactive_cutoff()
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
            # Canonical track labels for this challenge — a superset of the
            # configured `tracks` keys. Lets the Admin Console's instances
            # editor offer tracks currently at 0 instances (absent from
            # `tracks`), which would otherwise be invisible and uneditable.
            "track_keys": list(ch_def.track_keys) if ch_def else [],
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
        "negative_trajectory_limit": swarm_setting(
            config, "negative_trajectory_limit",
        ),
        "hypothesis_recall_threshold": swarm_setting(
            config, "hypothesis_recall_threshold",
        ),
        # HPO gate + search knobs, read client-side by scripts/run_loop.py.
        "hpo_first_tune_improvements": swarm_setting(config, "hpo_first_tune_improvements"),
        "hpo_min_improvements": swarm_setting(config, "hpo_min_improvements"),
        "hpo_search_budget": swarm_setting(config, "hpo_search_budget"),
        "hpo_num_suggested_configs": swarm_setting(config, "hpo_num_suggested_configs"),
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
            ("negative_trajectory_limit", str(req.negative_trajectory_limit) if req.negative_trajectory_limit is not None else None),
            ("hypothesis_recall_threshold", str(req.hypothesis_recall_threshold) if req.hypothesis_recall_threshold is not None else None),
            ("hpo_first_tune_improvements", str(req.hpo_first_tune_improvements) if req.hpo_first_tune_improvements is not None else None),
            ("hpo_min_improvements", str(req.hpo_min_improvements) if req.hpo_min_improvements is not None else None),
            ("hpo_search_budget", str(req.hpo_search_budget) if req.hpo_search_budget is not None else None),
            ("hpo_num_suggested_configs", str(req.hpo_num_suggested_configs) if req.hpo_num_suggested_configs is not None else None),
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
