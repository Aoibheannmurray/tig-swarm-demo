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
    RegisterRequest, HeartbeatRequest, HypothesisCreate, ExperimentCreate,
    IterationCreate, AdminBroadcast, AdminAuth, MessageCreate,
    SwarmConfigUpdate,
    AgentResponse, HypothesisResponse,
    ExperimentResponse, IterationResponse, new_id, improvement_pct,
)
from names import generate_agent_name, load_used_names
from dedup import fingerprint
import db

logger = logging.getLogger("swarm")

# Initial algorithm broadcast to every agent on a fresh trajectory:
# their first iteration, and again whenever a trajectory reset draws the
# "fresh start" slot from the inactive-algorithms pool. Set by the host
# at create time (the wizard reads `initial_algorithm.rs` from the repo
# root and POSTs it to /api/swarm_config); empty until the first
# successful POST, in which case agents start from a blank slate.


def load_initial_algorithm(config: dict) -> str:
    return config.get("initial_algorithm_code", "") or ""


# Cached config — refreshed on admin config update
_config_cache: dict | None = None


async def get_config_cached() -> dict:
    global _config_cache
    if _config_cache is None:
        async with db.connect() as conn:
            _config_cache = await db.get_config(conn)
    return _config_cache


async def get_direction() -> str:
    cfg = await get_config_cached()
    d = cfg.get("scoring_direction", "min")
    return "max" if d == "max" else "min"


def get_num_instances(config: dict, route_data=None) -> int:
    # Authoritative count: the actual keys in the current best experiment's
    # route_data (one entry per benchmark instance). The swarm config's
    # `tracks` dict is the fallback for the pre-first-experiment moment —
    # sum the per-track instance counts (excluding the "seed" key).
    if route_data:
        try:
            rd = json.loads(route_data) if isinstance(route_data, str) else route_data
            if rd:
                return len(rd)
        except Exception:
            pass
    try:
        tracks = json.loads(config.get("tracks", "{}"))
        total = sum(
            int(v) for k, v in tracks.items()
            if k != "seed" and isinstance(v, (int, float))
        )
        return total or 1
    except Exception:
        return 1


async def get_baseline_score(conn) -> float | None:
    """The baseline is the score of the very first feasible experiment
    published to the DB.  Scores are already per-instance averages (computed
    by benchmark.py), so no extra normalisation is needed.  Returns None when
    nothing feasible has landed yet."""
    cursor = await conn.execute(
        "SELECT score FROM experiments "
        "WHERE feasible = 1 ORDER BY created_at ASC LIMIT 1"
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

    async def broadcast(self, event: dict):
        if not self.connections:
            return
        results = await asyncio.gather(
            *(ws.send_json(event) for ws in self.connections),
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
            config = await get_config_cached()
            direction = await get_direction()
            async with db.connect() as conn:
                best = await db.get_global_best(conn, direction=direction)
                baseline = await get_baseline_score(conn)
                cutoff_ts = inactive_cutoff()
                active = await db.get_agent_count(
                    conn, active_only=True, inactive_cutoff=cutoff_ts
                )
                total_agents = await db.get_agent_count(conn, active_only=False)
                total_exp = (await (await conn.execute("SELECT COUNT(*) as c FROM experiments")).fetchone())["c"]
                total_hyp = (await (await conn.execute("SELECT COUNT(*) as c FROM hypotheses")).fetchone())["c"]
                total_traj = (await (await conn.execute("SELECT COUNT(*) as c FROM trajectories")).fetchone())["c"]

            best_route_data = best["route_data"] if best else None
            num_instances = get_num_instances(config, best_route_data)
            best_score = best["score"] if best else None
            imp = (
                improvement_pct(baseline, best_score, direction)
                if baseline is not None and best_score is not None
                else 0
            )

            await manager.broadcast({
                "type": "stats_update",
                "active_agents": active,
                "total_agents": total_agents,
                "total_experiments": total_exp,
                "hypotheses_count": total_hyp,
                "total_trajectories": total_traj,
                "best_score": best_score,
                "baseline_score": baseline,
                "num_instances": num_instances,
                "improvement_pct": imp,
                "timestamp": now(),
            })
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

    await manager.broadcast({
        "type": "agent_joined",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "timestamp": timestamp,
    })

    # Tell the agent which challenge it's joining and how often to heartbeat.
    # Per-track instance counts and timeout live in /api/swarm_config — agents
    # already poll that endpoint at startup, so we don't duplicate the data
    # here.
    return AgentResponse(
        agent_id=agent_id,
        agent_name=agent_name,
        registered_at=timestamp,
        config={
            "heartbeat_interval_seconds": 30,
            "challenge": config.get("challenge", "vehicle_routing"),
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


def _pick_inspiration(
    all_bests: list[dict],
    agent_id: str,
    active_agent_ids: set[str],
) -> dict | None:
    """Pick a random active peer's best for inspiration (excluding self)."""
    pool = [
        b for b in all_bests
        if b["agent_id"] != agent_id and b["agent_id"] in active_agent_ids
    ]
    if not pool:
        return None
    return random.choice(pool)


@app.get("/api/state")
async def get_state(agent_id: str | None = None):
    """Return current swarm state.

    When `agent_id` is supplied, the agent receives its own current best
    code (or the challenge seed on first run). When stagnating past the
    `hypothesis_recall_threshold`, prior failed hypotheses for the current
    program are included with a directive to try something different.
    When stagnating past `stagnation_threshold`, a stagnation_hint field
    (50/50 "tacit_knowledge" or "inspiration") and inspiration_code are
    included.

    When `agent_id` is omitted, returns a global dashboard view.
    """
    config = await get_config_cached()
    direction = await get_direction()

    async with db.connect() as conn:
        global_best = await db.get_global_best(conn, direction=direction)
        baseline = await get_baseline_score(conn)
        cutoff_ts = inactive_cutoff()
        active = await db.get_agent_count(
            conn, active_only=True, inactive_cutoff=cutoff_ts
        )
        total_agents = await db.get_agent_count(conn, active_only=False)
        total_exp = (await (await conn.execute(
            "SELECT COUNT(*) as c FROM experiments"
        )).fetchone())["c"]
        total_hyp = (await (await conn.execute(
            "SELECT COUNT(*) as c FROM hypotheses"
        )).fetchone())["c"]
        total_traj = (await (await conn.execute(
            "SELECT COUNT(*) as c FROM trajectories"
        )).fetchone())["c"]

        # ── Agent-specific view ──
        if agent_id is not None:
            await conn.execute(
                "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
                (now(), agent_id),
            )
            await conn.commit()

            my_best = await db.get_agent_best(conn, agent_id)
            cursor = await conn.execute(
                "SELECT experiments_completed, runs_since_improvement, "
                "improvements FROM agents WHERE id = ?",
                (agent_id,),
            )
            agent_row = await cursor.fetchone()
            runs_since = agent_row["runs_since_improvement"] if agent_row else 0

            # ── Trajectory reset on stagnation_limit ──
            trajectory_reset = None
            stagnation_limit = int(config.get("stagnation_limit", "10"))
            if stagnation_limit > 0 and runs_since >= stagnation_limit and my_best is not None:
                timestamp = now()
                # Deactivate the current trajectory
                cur_traj_id = None
                cur_traj_row = await conn.execute(
                    "SELECT current_trajectory_id, current_program_id FROM agents WHERE id = ?",
                    (agent_id,),
                )
                cur_traj = await cur_traj_row.fetchone()
                old_program_id = cur_traj["current_program_id"] if cur_traj else None
                if cur_traj and cur_traj["current_trajectory_id"]:
                    cur_traj_id = cur_traj["current_trajectory_id"]
                    await db.deactivate_trajectory(conn, cur_traj_id, timestamp)

                # Pick from the inactive pool BEFORE depositing, so the
                # agent can't re-adopt its own just-deposited code.
                n_inactive = await db.count_inactive(conn)
                new_traj_id = None
                new_program_id = None
                # Uniform pick: 1/(n_inactive+1) for fresh, rest for each inactive
                if n_inactive == 0 or random.randint(0, n_inactive) == 0:
                    new_code = load_initial_algorithm(config)
                    new_program_id = new_id()
                    trajectory_reset = {"type": "fresh_start"}
                else:
                    picked = await db.pick_random_inactive(conn)
                    if picked:
                        new_code = picked["algorithm_code"]
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
                    else:
                        new_code = load_initial_algorithm(config)
                        new_program_id = new_id()
                        trajectory_reset = {"type": "fresh_start"}

                # Now deposit the stagnated code into the inactive pool.
                await db.deposit_inactive(
                    conn, agent_id, my_best["algorithm_code"],
                    my_best["score"], timestamp,
                )
                # Tag the deposited inactive with trajectory and program_id
                # so adopters inherit the hypothesis history.
                await conn.execute(
                    "UPDATE inactive_algorithms SET trajectory_id = ?, program_id = ? "
                    "WHERE agent_id = ? AND deposited_at = ?",
                    (cur_traj_id, old_program_id, agent_id, timestamp),
                )

                await db.clear_agent_best(conn, agent_id)
                await conn.execute(
                    "UPDATE agents SET runs_since_improvement = 0, "
                    "best_score = NULL, current_trajectory_id = ?, "
                    "current_program_id = ? WHERE id = ?",
                    (new_traj_id, new_program_id, agent_id),
                )
                await conn.commit()
                my_best = None
                my_best_code = new_code
                my_best_score = None
                my_best_experiment_id = None
                runs_since = 0
                agent_name = await get_agent_name(conn, agent_id)
                await manager.broadcast({
                    "type": "trajectory_reset",
                    "agent_name": agent_name,
                    "agent_id": agent_id,
                    "reset_type": trajectory_reset["type"],
                    "timestamp": timestamp,
                })
            else:
                my_best_code = (
                    my_best["algorithm_code"] if my_best
                    else load_initial_algorithm(config)
                )
                my_best_score = my_best["score"] if my_best else None
                my_best_experiment_id = my_best["experiment_id"] if my_best else None

            # ── Program ID management ──
            prog_cursor = await conn.execute(
                "SELECT current_program_id FROM agents WHERE id = ?",
                (agent_id,),
            )
            prog_row = await prog_cursor.fetchone()
            current_program_id = prog_row["current_program_id"] if prog_row else None
            if not current_program_id:
                current_program_id = new_id()
                await conn.execute(
                    "UPDATE agents SET current_program_id = ? WHERE id = ?",
                    (current_program_id, agent_id),
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
                       WHERE h.program_id = ? AND h.status = 'failed'
                       ORDER BY h.created_at DESC LIMIT 20""",
                    (current_program_id,),
                )
                prior_hypotheses = [dict(row) for row in await cursor.fetchall()]
                if prior_hypotheses:
                    hypothesis_recall_message = (
                        "The following strategies were tried on this program and "
                        "did not improve the score. Try something structurally "
                        "different from these approaches."
                    )

            # Inspiration on stagnation (only when not trajectory-resetting)
            inspiration_code = None
            inspiration_agent_name = None
            stagnation_hint = None
            n_stagnation = int(config.get("stagnation_threshold", "2"))
            if trajectory_reset is None and runs_since >= n_stagnation:
                stagnation_hint = random.choice(["tacit_knowledge", "inspiration"])
                if stagnation_hint == "tacit_knowledge":
                    await conn.execute(
                        "UPDATE agents SET tacit_knowledge_count = tacit_knowledge_count + 1 WHERE id = ?",
                        (agent_id,),
                    )
                else:
                    await conn.execute(
                        "UPDATE agents SET inspiration_count = inspiration_count + 1 WHERE id = ?",
                        (agent_id,),
                    )
                await conn.commit()
                all_bests = await db.list_agent_bests(conn, direction=direction)
                cutoff_ts = inactive_cutoff()
                cursor = await conn.execute(
                    "SELECT id FROM agents WHERE last_heartbeat >= ?",
                    (cutoff_ts,),
                )
                active_ids = {row["id"] for row in await cursor.fetchall()}
                chosen = _pick_inspiration(all_bests, agent_id, active_ids)
                if chosen:
                    inspiration_code = chosen["algorithm_code"]
                    inspiration_agent_name = await get_agent_name(
                        conn, chosen["agent_id"]
                    )

            best_route_data = my_best["route_data"] if my_best else None
            num_instances = get_num_instances(config, best_route_data)
            leaderboard = await db.compute_leaderboard(conn, inactive_cutoff(), direction=direction)
            global_best_score = global_best["score"] if global_best else None

            return {
                "best_score": global_best_score,
                "best_algorithm_code": my_best_code,
                "best_experiment_id": my_best_experiment_id,
                "my_best_score": my_best_score,
                "my_runs": agent_row["experiments_completed"] if agent_row else 0,
                "my_improvements": agent_row["improvements"] if agent_row else 0,
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
                "leaderboard": leaderboard,
            }

        # ── Dashboard view (no agent_id) ──
        cursor = await conn.execute("""
            SELECT e.*, a.name as agent_name,
                   EXISTS(SELECT 1 FROM best_history bh
                          WHERE bh.experiment_id = e.id) as is_new_best
            FROM experiments e JOIN agents a ON a.id = e.agent_id
            ORDER BY e.created_at DESC LIMIT 20
        """)
        recent_experiments = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """SELECT h.id, h.title, h.strategy_tag, h.description,
                      a.name as agent_name, h.agent_id, h.parent_hypothesis_id
               FROM hypotheses h JOIN agents a ON a.id = h.agent_id
               ORDER BY h.created_at DESC LIMIT 30"""
        )
        recent_hypotheses = [dict(row) for row in await cursor.fetchall()]

        served = global_best
        best_route_data = served["route_data"] if served else None
        num_instances = get_num_instances(config, best_route_data)
        leaderboard = await db.compute_leaderboard(conn, inactive_cutoff(), direction=direction)

    global_best_score = global_best["score"] if global_best else None
    overall_imp = (
        improvement_pct(baseline, global_best_score, direction)
        if baseline is not None and global_best_score is not None
        else 0
    )

    return {
        "baseline_score": baseline,
        "best_score": global_best_score,
        "improvement_pct": overall_imp,
        "best_algorithm_code": (
            served["algorithm_code"] if served
            else load_initial_algorithm(config)
        ),
        "best_experiment_id": served["id"] if served else None,
        "best_route_data": json.loads(served["route_data"]) if served and served["route_data"] else None,
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
             "agent_id": h.get("agent_id", "")}
            for h in recent_hypotheses
        ],
        "leaderboard": leaderboard,
    }


# ── Iteration endpoint (unified hypothesis + experiment) ──

@app.post("/api/iterations", response_model=IterationResponse)
async def create_iteration(req: IterationCreate):
    config = await get_config_cached()
    direction = await get_direction()
    exp_id = new_id()
    hyp_id = new_id()
    timestamp = now()
    route_data_json = json.dumps(req.route_data) if req.route_data else None
    fp = fingerprint(req.title, req.strategy_tag)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")

        prev_best = await db.get_global_best(conn, direction=direction)
        prev_agent_best = await db.get_agent_best(conn, req.agent_id)
        baseline = await get_baseline_score(conn)

        is_new_best = prev_best is None or db.is_better(direction, req.score, prev_best["score"])
        beats_own_best = (
            prev_agent_best is None
            or db.is_better(direction, req.score, prev_agent_best["score"])
        )

        target_best_experiment_id = (
            prev_agent_best["experiment_id"] if prev_agent_best else None
        )
        hyp_status = "succeeded" if beats_own_best else "failed"

        # ── Program ID: tag hypothesis with current program ──
        prog_cursor = await conn.execute(
            "SELECT current_program_id FROM agents WHERE id = ?",
            (req.agent_id,),
        )
        prog_row = await prog_cursor.fetchone()
        current_program_id = prog_row["current_program_id"] if prog_row else None
        if not current_program_id:
            current_program_id = new_id()
            await conn.execute(
                "UPDATE agents SET current_program_id = ? WHERE id = ?",
                (current_program_id, req.agent_id),
            )

        await conn.execute(
            """INSERT INTO hypotheses
               (id, agent_id, title, description, strategy_tag, status,
                fingerprint, target_best_experiment_id, program_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hyp_id, req.agent_id, req.title, req.description,
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

        # ── Trajectory tracking ──
        traj_cursor = await conn.execute(
            "SELECT current_trajectory_id FROM agents WHERE id = ?",
            (req.agent_id,),
        )
        traj_row = await traj_cursor.fetchone()
        trajectory_id = traj_row["current_trajectory_id"] if traj_row else None
        if not trajectory_id:
            trajectory_id = new_id()
            await db.create_trajectory(
                conn, trajectory_id, timestamp,
                current_score=req.score if beats_own_best else None,
            )
            await conn.execute(
                "UPDATE agents SET current_trajectory_id = ?, "
                "num_trajectories = num_trajectories + 1 WHERE id = ?",
                (trajectory_id, req.agent_id),
            )

        await conn.execute(
            """INSERT INTO experiments
               (id, agent_id, hypothesis_id, algorithm_code, score, feasible,
                num_vehicles, total_distance, notes, route_data,
                delta_vs_best_pct, delta_vs_own_best_pct, beats_own_best,
                trajectory_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exp_id, req.agent_id, hyp_id, req.algorithm_code, req.score,
             1 if req.feasible else 0, req.num_vehicles, req.total_distance,
             req.notes, route_data_json,
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
            await conn.execute(
                f"""UPDATE agents SET
                    experiments_completed = experiments_completed + 1,
                    runs_since_improvement = 0,
                    improvements = improvements + 1,
                    best_score = ?,
                    current_program_id = ?,
                    best_ever_score = CASE
                        WHEN best_ever_score IS NULL THEN ?
                        WHEN ? {('>' if direction == 'max' else '<')} best_ever_score THEN ?
                        ELSE best_ever_score
                    END
                   WHERE id = ?""",
                (req.score, new_program_id, req.score, req.score, req.score, req.agent_id),
            )
            await db.upsert_agent_best(
                conn, agent_id=req.agent_id, experiment_id=exp_id,
                algorithm_code=req.algorithm_code, score=req.score,
                feasible=req.feasible, num_vehicles=req.num_vehicles,
                total_distance=req.total_distance, route_data=route_data_json,
                updated_at=timestamp, trajectory_id=trajectory_id,
            )
        else:
            await conn.execute(
                """UPDATE agents SET
                    experiments_completed = experiments_completed + 1,
                    runs_since_improvement = runs_since_improvement + 1
                   WHERE id = ?""",
                (req.agent_id,),
            )

        agent_name = await get_agent_name(conn, req.agent_id)
        incremental_pct = delta_vs_best_pct if is_new_best else None

        if is_new_best:
            await conn.execute(
                """INSERT INTO best_history
                   (experiment_id, agent_id, agent_name, score, route_data, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (exp_id, req.agent_id, agent_name, req.score, route_data_json, timestamp),
            )

        await conn.commit()

        cursor = await conn.execute(
            "SELECT experiments_completed, runs_since_improvement, "
            "improvements FROM agents WHERE id = ?",
            (req.agent_id,),
        )
        agent_info = dict(await cursor.fetchone())
        leaderboard = await db.compute_leaderboard(conn, inactive_cutoff(), direction=direction)
        rank = next(
            (e["rank"] for e in leaderboard if e["agent_id"] == req.agent_id),
            0,
        )

    effective_route_data = req.route_data or (
        prev_best["route_data"] if prev_best else None
    )
    num_instances = get_num_instances(config, effective_route_data)
    imp = improvement_pct(baseline, req.score, direction) if baseline is not None else 0.0

    await manager.broadcast({
        "type": "experiment_published",
        "experiment_id": exp_id,
        "agent_name": agent_name,
        "agent_id": req.agent_id,
        "score": req.score,
        "feasible": req.feasible,
        "improvement_pct": imp,
        "delta_vs_best_pct": delta_vs_best_pct,
        "beats_own_best": beats_own_best,
        "delta_vs_own_best_pct": delta_vs_own_best_pct,
        "num_instances": num_instances,
        "is_new_best": is_new_best,
        "hypothesis_id": hyp_id,
        "strategy_tag": req.strategy_tag,
        "title": req.title,
        "notes": req.notes,
        "timestamp": timestamp,
    })

    if is_new_best:
        await manager.broadcast({
            "type": "new_global_best",
            "experiment_id": exp_id,
            "agent_name": agent_name,
            "agent_id": req.agent_id,
            "score": req.score,
            "improvement_pct": imp,
            "incremental_improvement_pct": incremental_pct,
            "num_instances": num_instances,
            "route_data": req.route_data,
            "timestamp": timestamp,
        })

    await manager.broadcast({
        "type": "leaderboard_update",
        "entries": leaderboard,
        "timestamp": timestamp,
    })

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


# ── Hypothesis endpoints (legacy) ──

@app.post("/api/hypotheses")
async def create_hypothesis(req: HypothesisCreate):
    async with db.connect() as conn:
        my_best = await db.get_agent_best(conn, req.agent_id)
        target_best_experiment_id = (
            my_best["experiment_id"] if my_best else None
        )

        hyp_id = new_id()
        fp = fingerprint(req.title, req.strategy_tag)
        timestamp = now()
        # Legacy endpoint compatibility:
        # this route only creates a hypothesis row, while /api/experiments
        # later determines its real outcome. There is no "active" status.
        # Until evaluated, keep it as failed-by-default; /api/experiments
        # will overwrite to succeeded when it improves the agent's own best.
        status = "failed"

        await conn.execute(
            """INSERT INTO hypotheses
               (id, agent_id, title, description, strategy_tag, status, fingerprint,
                parent_hypothesis_id, created_at,
                target_best_experiment_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hyp_id, req.agent_id, req.title, req.description, req.strategy_tag,
             status, fp, req.parent_hypothesis_id, timestamp,
             target_best_experiment_id),
        )
        await conn.commit()

        agent_name = await get_agent_name(conn, req.agent_id)

    await manager.broadcast({
        "type": "hypothesis_proposed",
        "hypothesis_id": hyp_id,
        "agent_name": agent_name,
        "agent_id": req.agent_id,
        "title": req.title,
        "description": req.description,
        "strategy_tag": req.strategy_tag,
        "parent_hypothesis_id": req.parent_hypothesis_id,
        "timestamp": timestamp,
    })

    return HypothesisResponse(hypothesis_id=hyp_id, status=status, fingerprint=fp)


@app.get("/api/hypotheses")
async def list_hypotheses(status: str | None = None, strategy_tag: str | None = None):
    async with db.connect() as conn:
        query = "SELECT h.*, a.name as agent_name FROM hypotheses h JOIN agents a ON a.id = h.agent_id WHERE 1=1"
        params = []
        if status:
            query += " AND h.status = ?"
            params.append(status)
        if strategy_tag:
            query += " AND h.strategy_tag = ?"
            params.append(strategy_tag)
        query += " ORDER BY h.created_at DESC"
        cursor = await conn.execute(query, params)
        return [dict(row) for row in await cursor.fetchall()]


# ── Experiment endpoints ──

@app.post("/api/experiments", response_model=ExperimentResponse)
async def create_experiment(req: ExperimentCreate):
    config = await get_config_cached()
    direction = await get_direction()

    exp_id = new_id()
    timestamp = now()
    route_data_json = json.dumps(req.route_data) if req.route_data else None

    async with db.connect() as conn:
        # Take the SQLite write lock up front (BEGIN IMMEDIATE) so the
        # read→decide→write block below runs atomically with respect to
        # concurrent publishes. Without this, two agents can both read the
        # same prev_best, both conclude is_new_best=True, and both insert
        # into best_history — producing non-monotonic rows in /api/replay.
        await conn.execute("BEGIN IMMEDIATE")

        # Capture the previous global best, the publishing agent's prior
        # own-best, and the baseline BEFORE inserting. Otherwise a read
        # after the insert would return the row we just wrote — breaking
        # is_new_best, beats_own_best, and baseline detection.
        prev_best = await db.get_global_best(conn, direction=direction)
        prev_agent_best = await db.get_agent_best(conn, req.agent_id)
        baseline = await get_baseline_score(conn)

        is_new_best = prev_best is None or db.is_better(direction, req.score, prev_best["score"])
        beats_own_best = (
            prev_agent_best is None
            or db.is_better(direction, req.score, prev_agent_best["score"])
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

        # ── Trajectory tracking ──
        traj_cursor = await conn.execute(
            "SELECT current_trajectory_id FROM agents WHERE id = ?",
            (req.agent_id,),
        )
        traj_row = await traj_cursor.fetchone()
        trajectory_id = traj_row["current_trajectory_id"] if traj_row else None
        if not trajectory_id:
            trajectory_id = new_id()
            await db.create_trajectory(
                conn, trajectory_id, timestamp,
                current_score=req.score if beats_own_best else None,
            )
            await conn.execute(
                "UPDATE agents SET current_trajectory_id = ?, "
                "num_trajectories = num_trajectories + 1 WHERE id = ?",
                (trajectory_id, req.agent_id),
            )

        await conn.execute(
            """INSERT INTO experiments
               (id, agent_id, hypothesis_id, algorithm_code, score, feasible,
                num_vehicles, total_distance, runtime_seconds, notes, route_data,
                delta_vs_best_pct, delta_vs_own_best_pct, beats_own_best,
                trajectory_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exp_id, req.agent_id, req.hypothesis_id, req.algorithm_code, req.score,
             1 if req.feasible else 0, req.num_vehicles, req.total_distance,
             req.runtime_seconds, req.notes, route_data_json,
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
            await conn.execute(
                f"""UPDATE agents SET
                    experiments_completed = experiments_completed + 1,
                    runs_since_improvement = 0,
                    improvements = improvements + 1,
                    best_score = ?,
                    current_program_id = ?,
                    best_ever_score = CASE
                        WHEN best_ever_score IS NULL THEN ?
                        WHEN ? {('>' if direction == 'max' else '<')} best_ever_score THEN ?
                        ELSE best_ever_score
                    END
                   WHERE id = ?""",
                (req.score, new_program_id, req.score, req.score, req.score, req.agent_id),
            )
            await db.upsert_agent_best(
                conn,
                agent_id=req.agent_id,
                experiment_id=exp_id,
                algorithm_code=req.algorithm_code,
                score=req.score,
                feasible=req.feasible,
                num_vehicles=req.num_vehicles,
                total_distance=req.total_distance,
                route_data=route_data_json,
                updated_at=timestamp,
                trajectory_id=trajectory_id,
            )
        else:
            await conn.execute(
                """UPDATE agents SET
                    experiments_completed = experiments_completed + 1,
                    runs_since_improvement = runs_since_improvement + 1
                   WHERE id = ?""",
                (req.agent_id,),
            )

        agent_name = await get_agent_name(conn, req.agent_id)
        incremental_pct = delta_vs_best_pct if is_new_best else None

        if is_new_best:
            await conn.execute(
                "INSERT INTO best_history (experiment_id, agent_id, agent_name, score, route_data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (exp_id, req.agent_id, agent_name, req.score, route_data_json, timestamp),
            )

        # Prefer this experiment's own route_data; if it wasn't provided,
        # fall back to the previous global best's.
        effective_route_data = req.route_data or (prev_best["route_data"] if prev_best else None)
        num_instances = get_num_instances(config, effective_route_data)

        hyp_status = None
        if req.hypothesis_id:
            # Under the branch model: a hypothesis succeeds iff it improves
            # the publishing agent's own branch. This replaces the old
            # "beats baseline" rule, which became noisy once many branches
            # existed at different score levels.
            hyp_status = "succeeded" if beats_own_best else "failed"
            await conn.execute(
                "UPDATE hypotheses SET status = ? WHERE id = ?",
                (hyp_status, req.hypothesis_id),
            )

        await conn.commit()
        leaderboard = await db.compute_leaderboard(conn, inactive_cutoff(), direction=direction)
        rank = next((e["rank"] for e in leaderboard if e["agent_id"] == req.agent_id), 0)

    imp = improvement_pct(baseline, req.score, direction) if baseline is not None else 0.0

    if hyp_status and req.hypothesis_id:
        await manager.broadcast({
            "type": "hypothesis_status_changed",
            "hypothesis_id": req.hypothesis_id,
            "new_status": hyp_status,
            "agent_name": agent_name,
            "timestamp": timestamp,
        })

    # Strategy tag and title come from the hypothesis; null when the legacy
    # flow was called without one (seed-era experiments).
    strategy_tag = None
    hyp_title = None
    if req.hypothesis_id:
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT strategy_tag, title FROM hypotheses WHERE id = ?",
                (req.hypothesis_id,),
            )
            hyp_row = await cursor.fetchone()
            if hyp_row:
                strategy_tag = hyp_row["strategy_tag"]
                hyp_title = hyp_row["title"]

    await manager.broadcast({
        "type": "experiment_published",
        "experiment_id": exp_id,
        "agent_name": agent_name,
        "agent_id": req.agent_id,
        "score": req.score,
        "feasible": req.feasible,
        "improvement_pct": imp,
        "delta_vs_best_pct": delta_vs_best_pct,
        "beats_own_best": beats_own_best,
        "delta_vs_own_best_pct": delta_vs_own_best_pct,
        "num_instances": num_instances,
        "is_new_best": is_new_best,
        "hypothesis_id": req.hypothesis_id,
        "strategy_tag": strategy_tag,
        "title": hyp_title,
        "notes": req.notes,
        "timestamp": timestamp,
    })

    if is_new_best:
        await manager.broadcast({
            "type": "new_global_best",
            "experiment_id": exp_id,
            "agent_name": agent_name,
            "agent_id": req.agent_id,
            "score": req.score,
            "improvement_pct": imp,
            "incremental_improvement_pct": incremental_pct,
            "num_instances": num_instances,
            "route_data": req.route_data,
            "timestamp": timestamp,
        })

    await manager.broadcast({
        "type": "leaderboard_update",
        "entries": leaderboard,
        "timestamp": timestamp,
    })

    return ExperimentResponse(
        experiment_id=exp_id,
        is_new_best=is_new_best,
        rank=rank,
        improvement_over_baseline_pct=imp,
        hypothesis_status_updated_to=hyp_status,
    )


# ── Leaderboard ──

@app.get("/api/leaderboard")
async def get_leaderboard():
    direction = await get_direction()
    async with db.connect() as conn:
        leaderboard = await db.compute_leaderboard(conn, inactive_cutoff(), direction=direction)
    return {"updated_at": now(), "entries": leaderboard}


# ── Messages (chat feed) ──

@app.post("/api/messages")
async def create_message(req: MessageCreate):
    msg_id = new_id()
    timestamp = now()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO messages (id, agent_id, agent_name, content, msg_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, req.agent_id, req.agent_name, req.content, req.msg_type, timestamp),
        )
        await conn.commit()

    await manager.broadcast({
        "type": "chat_message",
        "message_id": msg_id,
        "agent_name": req.agent_name,
        "agent_id": req.agent_id,
        "content": req.content,
        "msg_type": req.msg_type,
        "timestamp": timestamp,
    })

    return {"message_id": msg_id, "timestamp": timestamp}


@app.get("/api/messages")
async def list_messages(limit: int = 50):
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return rows



# ── Diversity ──

@app.get("/api/diversity")
async def get_diversity():
    direction = await get_direction()
    order = db._direction_order(direction)
    async with db.connect() as conn:
        cursor = await conn.execute(
            f"""SELECT ab.agent_id, a.name as agent_name, ab.algorithm_code
               FROM agent_bests ab
               JOIN agents a ON a.id = ab.agent_id
               WHERE ab.feasible = 1
               ORDER BY ab.score {order}"""
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

@app.get("/api/replay")
async def get_replay():
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT * FROM best_history ORDER BY created_at ASC"
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return [
        {
            "experiment_id": r["experiment_id"],
            "agent_id": r.get("agent_id"),
            "agent_name": r["agent_name"],
            "score": r["score"],
            "route_data": json.loads(r["route_data"]) if r["route_data"] else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/api/top_scores")
async def get_top_scores(limit: int = 20):
    # Top-N feasible iterations across the whole swarm, joined to the
    # proposing hypothesis for its strategy tag + title. Same agent can
    # appear multiple times — each row is one iteration, not a per-agent
    # roll-up. title / strategy_tag come back null when the experiment has
    # no associated hypothesis (legacy/seed rows).
    direction = await get_direction()
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
               WHERE e.feasible = 1
               ORDER BY e.score {order}
               LIMIT ?""",
            (limit,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return {"entries": rows, "limit": limit}


@app.get("/api/agent_experiments")
async def get_agent_experiments(agent_id: str):
    # Per-agent full attempt history for the personal progress chart.
    # Returns every experiment (improvement or not, feasible or not) so the
    # dashboard can render a step plot of the agent's whole journey.
    async with db.connect() as conn:
        ag = await conn.execute(
            "SELECT id, name, registered_at FROM agents WHERE id = ?",
            (agent_id,),
        )
        agent_row = await ag.fetchone()
        if agent_row is None:
            return {"agent_id": agent_id, "agent_name": None,
                    "registered_at": None, "experiments": []}

        cursor = await conn.execute(
            """SELECT e.id, e.score, e.feasible, e.beats_own_best, e.notes,
                      e.created_at, h.title, h.description, h.strategy_tag
               FROM experiments e
               LEFT JOIN hypotheses h ON h.id = e.hypothesis_id
               WHERE e.agent_id = ? ORDER BY e.created_at ASC""",
            (agent_id,),
        )
        rows = await cursor.fetchall()

    return {
        "agent_id": agent_id,
        "agent_name": agent_row["name"],
        "registered_at": agent_row["registered_at"],
        "experiments": [
            {
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
            for r in rows
        ],
    }


# ── Trajectories ──

@app.get("/api/trajectories")
async def get_trajectories():
    direction = await get_direction()
    async with db.connect() as conn:
        trajectories = await db.list_trajectories(conn)
        result = []
        for t in trajectories:
            history = await db.get_trajectory_score_history(
                conn, t["id"], direction
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
        "total": len(result),
        "active": active,
        "inactive": inactive,
        "trajectories": result,
    }


# ── Admin endpoints ──

@app.post("/api/admin/broadcast")
async def admin_broadcast(req: AdminBroadcast):
    await verify_admin(req)
    await manager.broadcast({
        "type": "admin_broadcast",
        "message": req.message,
        "priority": req.priority,
        "timestamp": now(),
    })
    return {"sent": True}


# Reset endpoint disabled to protect experiment data.
# @app.post("/api/admin/reset")
# async def admin_reset(req: AdminAuth):
#     await verify_admin(req)
#     async with db.connect() as conn:
#         await conn.execute("DELETE FROM experiments")
#         await conn.execute("DELETE FROM hypotheses")
#         await conn.execute("DELETE FROM agents")
#         await conn.execute("DELETE FROM messages")
#         await conn.execute("DELETE FROM agent_bests")
#         await conn.execute("DELETE FROM best_history")
#         await conn.commit()
#     await manager.broadcast({"type": "reset", "timestamp": now()})
#     return {"reset": True}


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

    The wizard writes these via POST /api/swarm_config; clones poll this
    endpoint on startup so all agents in a swarm optimize the same
    challenge with the same instance set and timeout.
    """
    config = await get_config_cached()
    try:
        tracks = json.loads(config.get("tracks", "{}"))
    except Exception:
        tracks = {}
    try:
        timeout = int(config.get("timeout", "30"))
    except Exception:
        timeout = 30
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
    return {
        "challenge": config.get("challenge", "vehicle_routing"),
        "tracks": tracks,
        "timeout": timeout,
        "scoring_direction": config.get("scoring_direction", "min"),
        "swarm_name": config.get("swarm_name", ""),
        "owner_name": config.get("owner_name", ""),
        "stagnation_threshold": stagnation_threshold,
        "stagnation_limit": stagnation_limit,
        "hypothesis_recall_threshold": hypothesis_recall_threshold,
    }


@app.post("/api/swarm_config")
async def update_swarm_config(req: SwarmConfigUpdate):
    """Owner-only endpoint to set swarm-wide configuration.

    Gated by admin_key — same secret used for /api/admin/broadcast.
    """
    global _config_cache
    await verify_admin(req)
    updates = {
        "challenge": req.challenge,
        "tracks": json.dumps(req.tracks),
        "timeout": str(req.timeout),
        "scoring_direction": req.scoring_direction,
        "swarm_name": req.swarm_name,
        "owner_name": req.owner_name,
        "stagnation_threshold": str(req.stagnation_threshold),
        "stagnation_limit": str(req.stagnation_limit),
        "hypothesis_recall_threshold": str(req.hypothesis_recall_threshold),
        "initial_algorithm_code": req.initial_algorithm_code,
    }
    async with db.connect() as conn:
        for key, value in updates.items():
            await conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    _config_cache = None
    # Tell connected dashboards to refetch swarm_config so labels and the
    # active visualization swap to the new challenge without a page reload.
    await manager.broadcast({
        "type": "swarm_config_updated",
        "challenge": req.challenge,
        "scoring_direction": req.scoring_direction,
        "swarm_name": req.swarm_name,
        "timestamp": now(),
    })
    return {"updated": True, **updates, "tracks": req.tracks, "timeout": req.timeout}


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
