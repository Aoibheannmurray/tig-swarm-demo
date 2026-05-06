from pydantic import BaseModel
from typing import Literal, Optional
import uuid


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def improvement_pct(baseline: float, score: float, direction: str = "min") -> float:
    """Percentage improvement of `score` over `baseline`.

    For min-direction challenges, lower is better, so positive improvement
    means score < baseline. For max-direction (knapsack/SAT/energy), the
    sign flips. baseline == 0 is degenerate; report 0.
    """
    if baseline == 0:
        return 0.0
    if direction == "max":
        return round(((score - baseline) / abs(baseline)) * 100, 2)
    return round(((baseline - score) / abs(baseline)) * 100, 2)


# ── Request models ──

class RegisterRequest(BaseModel):
    client_version: str = "1.0"


class HeartbeatRequest(BaseModel):
    status: Literal["idle", "working"] = "working"
    current_hypothesis_id: Optional[str] = None


class IterationCreate(BaseModel):
    agent_id: str
    title: str
    description: str = ""
    strategy_tag: str = "other"
    algorithm_code: str = ""
    score: float
    feasible: bool = True
    notes: str = ""
    solution_data: Optional[dict] = None
    # Per-track mean quality (e.g. {"n_nodes=200": 4.2e6, "n_nodes=600": 3.1e6}).
    # Used by the dashboard to show how the best program scores on each track
    # before they're combined into the overall geometric mean.
    track_scores: Optional[dict] = None
    challenge: Optional["ChallengeName"] = None
    # VRP-only: vehicles used and total tour distance. Stored as NULL for
    # every other challenge — these have no meaning for SAT, knapsack, etc.
    num_vehicles: Optional[int] = None
    total_distance: Optional[float] = None


class AdminAuth(BaseModel):
    admin_key: str


class AdminBroadcast(AdminAuth):
    message: str
    priority: Literal["normal", "high"] = "normal"


class AdminResetChallenge(AdminAuth):
    """Owner-only request to clear the leaderboard for a single challenge:
    drops `best_history` + `agent_bests` rows for that challenge so the next
    feasible publish becomes the new global best. Preserves `experiments`,
    `hypotheses`, and `agent_challenge_state` so prior research and per-agent
    counters (run counts, trajectories, stagnation) remain intact."""
    challenge: "ChallengeName"


# Swarm-wide configuration set by the owner via the setup wizard.
# challenge: which TIG challenge this swarm is optimizing.
# tracks: object mirroring the per-challenge test.json — keys are track
#         labels (e.g. "n_nodes=600"), values are instance counts.
# timeout: per-instance solver timeout in seconds.
# scoring_direction: "min" (smaller score wins) or "max" (larger wins).
# swarm_name / owner_name are display-only.
ChallengeName = Literal[
    "satisfiability",
    "vehicle_routing",
    "knapsack",
    "job_scheduling",
    "energy_arbitrage",
]


class ChallengeSubConfig(BaseModel):
    """Per-challenge configuration. The owner can populate all five in
    parallel via the wizard; switching the active challenge is independent."""
    tracks: dict = {}
    timeout: int = 5
    scoring_direction: Literal["min", "max"] = "max"
    initial_algorithm_code: str = ""
    strategy_tags: Optional[list[str]] = None


class SwarmConfigUpdate(AdminAuth):
    """Owner-only swarm config update.

    Set `active_challenge` to flip the swarm's active challenge, and/or
    `challenges` to update per-challenge sub-configs (partial updates
    supported — only the keys passed get written). Global keys (swarm
    name, stagnation thresholds) update independently.
    """
    active_challenge: Optional[ChallengeName] = None
    challenges: Optional[dict[ChallengeName, ChallengeSubConfig]] = None
    swarm_name: Optional[str] = None
    owner_name: Optional[str] = None
    stagnation_threshold: Optional[int] = None
    stagnation_limit: Optional[int] = None
    hypothesis_recall_threshold: Optional[int] = None


class MessageCreate(BaseModel):
    agent_id: Optional[str] = None
    agent_name: str
    content: str
    msg_type: Literal["agent", "milestone"] = "agent"
    challenge: Optional[ChallengeName] = None


# ── Response models ──

class AgentResponse(BaseModel):
    agent_id: str
    agent_name: str
    registered_at: str
    config: dict


class IterationResponse(BaseModel):
    experiment_id: str
    hypothesis_id: str
    is_new_best: bool
    beats_own_best: bool
    rank: int
    runs: int
    improvements: int
    runs_since_improvement: int
