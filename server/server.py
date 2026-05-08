import json
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from models import (
    RegisterRequest, HeartbeatRequest,
    IterationCreate, AdminBroadcast, AdminAuth, AdminResetChallenge,
    MessageCreate,
    SwarmConfigUpdate,
    AgentResponse,
    IterationResponse, new_id, improvement_pct,
)
from names import generate_agent_name, load_used_names
from dedup import fingerprint
import db
import ws_events
import api_models
import challenges

logger = logging.getLogger("swarm")


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
# when none is provided — preserving back-compat with old clients during
# the rollout window.

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
    return cfg.get("active_challenge") or cfg.get("challenge") or "vehicle_routing"


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
    cfg = row or {
        "challenge": challenge,
        "tracks": "{}",
        "timeout": 5,
        "scoring_direction": "max",
        "initial_algorithm_code": "",
        "initial_kernel_code": "",
    }
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


async def get_agent_name(conn, agent_id: str) -> str:
    cursor = await conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,))
    row = await cursor.fetchone()
    return row["name"] if row else "unknown"


# ── WebSocket manager ──

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
        results = await asyncio.gather(
            *(ws.send_json(payload) for ws in self.connections),
            return_exceptions=True,
        )
        self.connections = [
            ws for ws, result in zip(self.connections, results)
            if not isinstance(result, Exception)
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
    return (datetime.now(timezone.utc) - timedelta(minutes=INACTIVE_MINUTES)).isoformat()


# ── Periodic stats ──

async def periodic_stats():
    while True:
        await asyncio.sleep(10)
        try:
            cutoff_ts = inactive_cutoff()
            active_challenge = await get_active_challenge()
            async with db.connect() as conn:
                total_agents = await db.get_agent_count(conn, active_only=False)
                # Per-challenge slices.
                per_challenge: dict[str, dict] = {}
                for ch in challenges.CHALLENGE_NAMES:
                    direction = await get_direction(ch)
                    cfg = await get_challenge_config_cached(ch)
                    best = await db.get_global_best(conn, ch, direction=direction)
                    baseline = await get_baseline_score(conn, ch)
                    # Active agents on this challenge = agents whose
                    # agent_challenge_state(*, ch).last_active_at is recent.
                    cur = await conn.execute(
                        "SELECT COUNT(*) as c FROM agent_challenge_state "
                        "WHERE challenge = ? AND last_active_at >= ?",
                        (ch, cutoff_ts),
                    )
                    active_ch = (await cur.fetchone())["c"]
                    cur = await conn.execute(
                        "SELECT COUNT(*) as c FROM experiments WHERE challenge = ?",
                        (ch,),
                    )
                    total_exp = (await cur.fetchone())["c"]
                    cur = await conn.execute(
                        "SELECT COUNT(*) as c FROM hypotheses WHERE challenge = ?",
                        (ch,),
                    )
                    total_hyp = (await cur.fetchone())["c"]
                    cur = await conn.execute(
                        "SELECT COUNT(*) as c FROM trajectories WHERE challenge = ?",
                        (ch,),
                    )
                    total_traj = (await cur.fetchone())["c"]
                    best_solution_data = best["solution_data"] if best else None
                    num_instances = get_num_instances_for(cfg, best_solution_data)
                    best_score = best["score"] if best else None
                    imp = (
                        improvement_pct(baseline, best_score, direction)
                        if baseline is not None and best_score is not None
                        else 0
                    )
                    per_challenge[ch] = {
                        "active_agents": active_ch,
                        "best_score": best_score,
                        "baseline_score": baseline,
                        "num_instances": num_instances,
                        "improvement_pct": imp,
                        "total_experiments": total_exp,
                        "hypotheses_count": total_hyp,
                        "total_trajectories": total_traj,
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
async def register_agent(req: RegisterRequest):
    agent_id = new_id()
    agent_name = generate_agent_name()
    timestamp = now()

    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, status) VALUES (?, ?, ?, ?, ?)",
            (agent_id, agent_name, timestamp, timestamp, "idle"),
        )
        await conn.commit()
        config = await db.get_config(conn)

    await manager.broadcast(ws_events.AgentJoined(
        agent_id=agent_id,
        agent_name=agent_name,
        timestamp=timestamp,
    ))

    active_challenge = (
        config.get("active_challenge")
        or config.get("challenge")
        or "vehicle_routing"
    )

    # `active_challenge` is the swarm-wide challenge the contributor should
    # auto-follow (set by the owner via `setup.py switch`). `challenge` is
    # kept as an alias for back-compat with older clients that read
    # `config.challenge`. Per-track counts / timeout live in
    # /api/swarm_config — the agent polls that on every iteration.
    swarm_type = config.get("swarm_type", "cpu")
    available = [
        ch for ch in challenges.CHALLENGE_NAMES
        if challenges.CHALLENGES[ch].is_gpu == (swarm_type == "gpu")
    ]

    return AgentResponse(
        agent_id=agent_id,
        agent_name=agent_name,
        registered_at=timestamp,
        config={
            "heartbeat_interval_seconds": 30,
            "active_challenge": active_challenge,
            "challenge": active_challenge,
            "swarm_type": swarm_type,
            "available_challenges": available,
        },
    )


@app.post("/api/agents/{agent_id}/heartbeat")
async def heartbeat(agent_id: str, req: HeartbeatRequest):
    timestamp = now()
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE agents SET last_heartbeat = ?, status = ? WHERE id = ?",
            (timestamp, req.status, agent_id),
        )
        await conn.commit()
    return {"ack": True, "server_time": timestamp}


# ── State endpoint ──

INACTIVE_MINUTES = 20


@app.get("/api/state")
async def get_state(
    agent_id: str | None = None,
    challenge: str | None = None,
):
    """Return current swarm state for the given challenge.

    When `agent_id` is supplied, the agent receives its own current best
    code for the requested challenge (or the per-challenge initial seed on
    first run). When stagnating past the `hypothesis_recall_threshold`,
    prior failed hypotheses for the current program are included with a
    directive to try something different. When stagnating past
    `stagnation_threshold`, a stagnation_hint field (50/50 "tacit_knowledge"
    or "inspiration") and inspiration_code are included — both filtered by
    the same challenge so per-challenge state stays disjoint.

    When `agent_id` is omitted, returns a global dashboard view (filtered
    by the requested or active challenge).

    `challenge` defaults to the swarm's `active_challenge` for back-compat
    with old clients that don't pass it.
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

            my_best = await db.get_agent_best(conn, agent_id, challenge)
            acs = await db.get_agent_challenge_state(conn, agent_id, challenge)
            runs_since = acs["runs_since_improvement"] if acs else 0

            # ── Trajectory reset on stagnation_limit ──
            trajectory_reset = None
            stagnation_limit = int(config.get("stagnation_limit", "10"))
            if stagnation_limit > 0 and runs_since >= stagnation_limit and my_best is not None:
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

                if not inactive_pool:
                    new_code, new_kernel_code = await load_initial_algorithm(challenge)
                    new_program_id = new_id()
                    trajectory_reset = {"type": "fresh_start"}
                else:
                    # Weighted sampling: p_fresh = max(0, 1 - kappa / mean(n_i))
                    # where mean(n_i) is over ALL trajectories (active + inactive).
                    # If not fresh, sample inactive trajectory j with weight 1/n_j.
                    kappa = float(config.get("restart_kappa", "2"))
                    mean_deact = await db.mean_trajectory_deactivations(conn, challenge)
                    p_fresh = max(0.0, 1.0 - kappa / mean_deact) if mean_deact > 0 else 0.0

                    if random.random() < p_fresh:
                        new_code, new_kernel_code = await load_initial_algorithm(challenge)
                        new_program_id = new_id()
                        trajectory_reset = {"type": "fresh_start"}
                    else:
                        picked = random.choice(inactive_pool)
                        new_code = picked["algorithm_code"]
                        new_kernel_code = picked.get("kernel_code")
                        new_program_id = picked.get("program_id") or new_id()
                        await db.remove_inactive(conn, picked["id"])
                        trajectory_reset = {
                            "type": "adopted_inactive",
                            "prior_score": picked["score"],
                        }
                        if picked.get("trajectory_id"):
                            new_traj_id = picked["trajectory_id"]
                            await db.reactivate_trajectory(conn, new_traj_id)
                            await db.increment_trajectory_agents(conn, new_traj_id)

                # Now deposit the stagnated code into the per-challenge pool.
                await db.deposit_inactive(
                    conn, agent_id, challenge,
                    my_best["algorithm_code"], my_best["score"], timestamp,
                    trajectory_id=cur_traj_id, program_id=old_program_id,
                    kernel_code=my_best.get("kernel_code"),
                )

                await db.clear_agent_best(conn, agent_id, challenge)
                await db.update_agent_challenge_state(
                    conn, agent_id, challenge,
                    set_fields={
                        "runs_since_improvement": 0,
                        "current_trajectory_id": new_traj_id,
                        "current_program_id": new_program_id,
                    },
                )
                await conn.commit()
                my_best = None
                my_best_code = new_code
                my_best_kernel_code = new_kernel_code
                my_best_score = None
                my_best_experiment_id = None
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
                if my_best:
                    my_best_code = my_best["algorithm_code"]
                    my_best_kernel_code = my_best.get("kernel_code")
                else:
                    my_best_code, my_best_kernel_code = await load_initial_algorithm(challenge)
                my_best_score = my_best["score"] if my_best else None
                my_best_experiment_id = my_best["experiment_id"] if my_best else None

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
            hypothesis_recall_threshold = int(config.get("hypothesis_recall_threshold", "3"))
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
            n_stagnation = int(config.get("stagnation_threshold", "2"))
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
                await conn.commit()
                all_bests = await db.list_agent_bests(
                    conn, challenge,
                    exclude_agent_ids=[agent_id],
                    direction=direction,
                    active_only=True,
                    inactive_cutoff=cutoff_ts,
                )
                if all_bests:
                    chosen = random.choice(all_bests)
                    inspiration_code = chosen["algorithm_code"]
                    inspiration_kernel_code = chosen.get("kernel_code")
                    inspiration_agent_name = await get_agent_name(
                        conn, chosen["agent_id"]
                    )

            best_solution_data = my_best["solution_data"] if my_best else None
            num_instances = get_num_instances_for(challenge_cfg, best_solution_data)
            leaderboard = await db.compute_leaderboard(
                conn, challenge, inactive_cutoff(), direction=direction,
            )
            global_best_score = global_best["score"] if global_best else None

            return {
                "challenge": challenge,
                "best_score": global_best_score,
                "best_algorithm_code": my_best_code,
                "best_kernel_code": my_best_kernel_code or None,
                "best_experiment_id": my_best_experiment_id,
                "my_best_score": my_best_score,
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
                "inspiration_kernel_code": inspiration_kernel_code or None,
                "inspiration_agent_name": inspiration_agent_name,
                "stagnation_hint": stagnation_hint,
                "trajectory_reset": trajectory_reset,
                "leaderboard": leaderboard,
            }

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
        "total_experiments": total_exp,
        "hypotheses_count": total_hyp,
        "total_trajectories": total_traj,
        "recent_experiments": [
            {
                "id": e["id"],
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
                "delta_vs_own_best_pct": e.get("delta_vs_own_best_pct"),
                "beats_own_best": bool(e.get("beats_own_best")),
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

@app.post("/api/iterations", response_model=IterationResponse)
async def create_iteration(req: IterationCreate):
    challenge = await resolve_challenge(req.challenge)
    direction = await get_direction(challenge)
    challenge_cfg = await get_challenge_config_cached(challenge)
    exp_id = new_id()
    hyp_id = new_id()
    timestamp = now()
    solution_data_json = json.dumps(req.solution_data) if req.solution_data else None
    track_scores_json = json.dumps(req.track_scores) if req.track_scores else None
    fp = fingerprint(req.title, req.strategy_tag)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")

        await db.ensure_agent_challenge_state(conn, req.agent_id, challenge, timestamp)

        prev_best = await db.get_global_best(conn, challenge, direction=direction)
        prev_agent_best = await db.get_agent_best(conn, req.agent_id, challenge)
        baseline = await get_baseline_score(conn, challenge)

        is_new_best = prev_best is None or db.is_better(direction, req.score, prev_best["score"])
        beats_own_best = (
            prev_agent_best is None
            or db.is_better(direction, req.score, prev_agent_best["score"])
        )

        target_best_experiment_id = (
            prev_agent_best["experiment_id"] if prev_agent_best else None
        )
        hyp_status = "succeeded" if beats_own_best else "failed"

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
        delta_vs_own_best_pct: float | None = None
        if prev_agent_best is not None and prev_agent_best["score"] != 0:
            delta_vs_own_best_pct = round(
                improvement_pct(prev_agent_best["score"], req.score, direction), 6
            )

        # ── Trajectory tracking (per-(agent, challenge)) ──
        trajectory_id = (acs or {}).get("current_trajectory_id")
        if not trajectory_id:
            trajectory_id = new_id()
            await db.create_trajectory(
                conn, trajectory_id, challenge, timestamp,
                current_score=req.score if beats_own_best else None,
            )
            await db.update_agent_challenge_state(
                conn, req.agent_id, challenge,
                set_fields={"current_trajectory_id": trajectory_id},
            )
            await db.increment_agent_challenge_counters(
                conn, req.agent_id, challenge, num_trajectories_inc=1,
            )

        await conn.execute(
            """INSERT INTO experiments
               (id, agent_id, challenge, hypothesis_id, algorithm_code, kernel_code,
                score, feasible,
                num_vehicles, total_distance, notes, solution_data, track_scores,
                delta_vs_best_pct, delta_vs_own_best_pct, beats_own_best,
                trajectory_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exp_id, req.agent_id, challenge, hyp_id, req.algorithm_code, req.kernel_code,
             req.score,
             1 if req.feasible else 0, req.num_vehicles, req.total_distance,
             req.notes, solution_data_json, track_scores_json,
             delta_vs_best_pct, delta_vs_own_best_pct,
             1 if beats_own_best else 0,
             trajectory_id, timestamp),
        )

        await db.update_trajectory_after_edit(
            conn, trajectory_id, beats_own_best,
            new_score=req.score if beats_own_best else None,
        )

        if beats_own_best:
            new_program_id = new_id()
            await db.increment_agent_challenge_counters(
                conn, req.agent_id, challenge,
                runs=1,
                improvements=1,
                runs_since_improvement_reset=True,
                best_ever_score=req.score,
                direction=direction,
            )
            await db.update_agent_challenge_state(
                conn, req.agent_id, challenge,
                set_fields={"current_program_id": new_program_id},
            )
            await db.upsert_agent_best(
                conn, agent_id=req.agent_id, challenge=challenge,
                experiment_id=exp_id,
                algorithm_code=req.algorithm_code, score=req.score,
                feasible=req.feasible, num_vehicles=req.num_vehicles,
                total_distance=req.total_distance, solution_data=solution_data_json,
                updated_at=timestamp, trajectory_id=trajectory_id,
                track_scores=track_scores_json,
                kernel_code=req.kernel_code,
            )
        else:
            await db.increment_agent_challenge_counters(
                conn, req.agent_id, challenge,
                runs=1,
                runs_since_improvement_inc=1,
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
        beats_own_best=beats_own_best,
        delta_vs_own_best_pct=delta_vs_own_best_pct,
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
        beats_own_best=beats_own_best,
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

@app.post("/api/messages")
async def create_message(req: MessageCreate):
    challenge = await resolve_challenge(req.challenge)
    msg_id = new_id()
    timestamp = now()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO messages (id, agent_id, challenge, agent_name, content, msg_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, req.agent_id, challenge, req.agent_name, req.content, req.msg_type, timestamp),
        )
        await conn.commit()

    await manager.broadcast(ws_events.ChatMessage(
        challenge=challenge,
        message_id=msg_id,
        agent_name=req.agent_name,
        agent_id=req.agent_id,
        content=req.content,
        msg_type=req.msg_type,
        timestamp=timestamp,
    ))

    return {"message_id": msg_id, "timestamp": timestamp, "challenge": challenge}


@app.get("/api/messages")
async def list_messages(limit: int = 50, challenge: str | None = None):
    challenge = await resolve_challenge(challenge)
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM messages WHERE challenge = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (challenge, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return rows



# ── Diversity ──

@app.get("/api/diversity", response_model=api_models.DiversityResponse)
async def get_diversity(challenge: str | None = None):
    challenge = await resolve_challenge(challenge)
    direction = await get_direction(challenge)
    order = db._direction_order(direction)
    async with db.connect() as conn:
        cursor = await conn.execute(
            f"""SELECT ab.agent_id, a.name as agent_name, ab.algorithm_code
               FROM agent_bests ab
               JOIN agents a ON a.id = ab.agent_id
               WHERE ab.feasible = 1 AND ab.challenge = ?
               ORDER BY ab.score {order}""",
            (challenge,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    if not rows:
        return {"agents": [], "matrix": []}

    agents = []
    line_sets = []
    for row in rows:
        agents.append({
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
        })
        lines = set(row["algorithm_code"].splitlines())
        lines.discard("")
        line_sets.append(lines)

    n = len(agents)
    all_others = [set().union(*(line_sets[k] for k in range(n) if k != i)) for i in range(n)]

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
        cursor = await conn.execute(
            "SELECT * FROM best_history WHERE challenge = ? ORDER BY created_at ASC",
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
            f"""SELECT e.id, e.score, e.feasible, e.beats_own_best, e.notes,
                      e.created_at, h.title, h.description, h.strategy_tag
                      {code_col}
               FROM experiments e
               LEFT JOIN hypotheses h ON h.id = e.hypothesis_id
               WHERE e.agent_id = ? AND e.challenge = ?
               ORDER BY e.created_at ASC""",
            (agent_id, challenge),
        )
        rows = await cursor.fetchall()

    def _row_dict(r):
        d = {
            "id": r["id"],
            "score": r["score"],
            "feasible": bool(r["feasible"]),
            "beats_own_best": bool(r["beats_own_best"]) if r["beats_own_best"] is not None else False,
            "notes": r["notes"],
            "title": r["title"],
            "description": r["description"],
            "strategy_tag": r["strategy_tag"],
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
        "experiments": [_row_dict(r) for r in rows],
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
            result.append({
                "id": t["id"],
                "started_at": t["started_at"],
                "status": t["status"],
                "current_score": t["current_score"],
                "num_edits": t["num_edits"],
                "num_improvements": t["num_improvements"],
                "momentum": round(t["momentum"], 4) if t["momentum"] else 0,
                "num_agents": t["num_agents"],
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
                       e.score, e.feasible, e.beats_own_best, e.notes,
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
            "beats_own_best": bool(r["beats_own_best"]) if r["beats_own_best"] is not None else False,
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
    """Per-challenge leaderboard reset. Drops `agent_bests` + `best_history`
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
            "DELETE FROM agent_bests WHERE challenge = ?", (challenge,),
        )
        agent_bests_deleted = cur.rowcount
        await conn.commit()
    await manager.broadcast(ws_events.ResetEvt(
        challenge=challenge,
        timestamp=now(),
    ))
    return {
        "reset": True,
        "challenge": challenge,
        "best_history_deleted": best_history_deleted,
        "agent_bests_deleted": agent_bests_deleted,
    }


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

    Multi-challenge model: the swarm hosts all five challenges in parallel.
    `active_challenge` is the swarm-wide challenge contributors auto-follow
    (set by the owner via POST /api/swarm_config). `available_challenges` is
    the per-challenge sub-config map (tracks, timeout, scoring_direction,
    initial_algorithm_code).

    For back-compat with old clients that still expect a flat shape, the
    active challenge's sub-config is also flattened to the top level
    (`challenge`, `tracks`, `timeout`, `scoring_direction`).
    """
    config = await get_config_cached()
    active_challenge = (
        config.get("active_challenge")
        or config.get("challenge")
        or "vehicle_routing"
    )
    try:
        stagnation_threshold = int(config.get("stagnation_threshold", "2"))
    except Exception:
        stagnation_threshold = 2
    try:
        stagnation_limit = int(config.get("stagnation_limit", "10"))
    except Exception:
        stagnation_limit = 10
    try:
        hypothesis_recall_threshold = int(config.get("hypothesis_recall_threshold", "3"))
    except Exception:
        hypothesis_recall_threshold = 3

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
            "timeout": row.get("timeout") or 5,
            "scoring_direction": row.get("scoring_direction") or "max",
            # Flag-only: don't ship the algorithm body in this response (it
            # can be large and is fetched separately from /api/initial_algorithm).
            "has_initial_algorithm": bool(row.get("initial_algorithm_code")),
            "has_initial_kernel_code": bool(row.get("initial_kernel_code")),
            "is_gpu": ch_def.is_gpu if ch_def else False,
            "strategy_tags": strategy_tags,
        }

    active_cfg = available.get(active_challenge, {})
    return {
        "active_challenge": active_challenge,
        "available_challenges": available,
        # Flat back-compat fields — populated from the active challenge.
        "challenge": active_challenge,
        "tracks": active_cfg.get("tracks", {}),
        "timeout": active_cfg.get("timeout", 5),
        "scoring_direction": active_cfg.get("scoring_direction", "max"),
        # Global keys.
        "swarm_name": config.get("swarm_name", ""),
        "owner_name": config.get("owner_name", ""),
        "swarm_type": config.get("swarm_type", "cpu"),
        "stagnation_threshold": stagnation_threshold,
        "stagnation_limit": stagnation_limit,
        "hypothesis_recall_threshold": hypothesis_recall_threshold,
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

    # Tell connected dashboards to refetch swarm_config so labels and the
    # active visualization swap to the new challenge without a page reload.
    await manager.broadcast(ws_events.SwarmConfigUpdated(
        active_challenge=config_after["active_challenge"],
        available_challenges=config_after["available_challenges"],
        scoring_direction=config_after["scoring_direction"],
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
