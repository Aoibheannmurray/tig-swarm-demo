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


class HypothesisCreate(BaseModel):
    agent_id: str
    title: str
    description: str
    strategy_tag: str = "other"
    parent_hypothesis_id: Optional[str] = None
    # Optional during the rollout window; server fills in the swarm's active
    # challenge if missing. After the transition this becomes required.
    challenge: Optional["ChallengeName"] = None


class ExperimentCreate(BaseModel):
    agent_id: str
    hypothesis_id: Optional[str] = None
    algorithm_code: str = ""
    score: float
    feasible: bool = True
    num_vehicles: int = 0
    total_distance: float = 0.0
    runtime_seconds: float = 0.0
    notes: str = ""
    solution_data: Optional[dict] = None
    challenge: Optional["ChallengeName"] = None


class IterationCreate(BaseModel):
    agent_id: str
    title: str
    description: str = ""
    strategy_tag: str = "other"
    algorithm_code: str = ""
    score: float
    feasible: bool = True
    num_vehicles: int = 0
    total_distance: float = 0.0
    notes: str = ""
    solution_data: Optional[dict] = None
    # Per-track mean quality (e.g. {"n_nodes=200": 4.2e6, "n_nodes=600": 3.1e6}).
    # Used by the dashboard to show how the best program scores on each track
    # before they're combined into the overall geometric mean.
    track_scores: Optional[dict] = None
    challenge: Optional["ChallengeName"] = None


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
    """Owner-only swarm config update. Two shapes are accepted during the
    rollout window:

      1. New shape (preferred): set `active_challenge` to flip the swarm's
         active challenge, and/or `challenges` to update per-challenge
         sub-configs (partial updates supported — only the keys passed
         get written).
      2. Legacy flat shape: `challenge` + `tracks` + `timeout` +
         `scoring_direction` + `initial_algorithm_code` writes a single
         challenge's sub-config and bumps `active_challenge` to it.

    All fields are optional; the server merges what's provided.
    """
    # New per-challenge model.
    active_challenge: Optional[ChallengeName] = None
    challenges: Optional[dict[ChallengeName, ChallengeSubConfig]] = None
    # Legacy flat fields (kept for back-compat).
    challenge: Optional[ChallengeName] = None
    tracks: Optional[dict] = None
    timeout: Optional[int] = None
    scoring_direction: Optional[Literal["min", "max"]] = None
    initial_algorithm_code: Optional[str] = None
    # Global keys.
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


class HypothesisResponse(BaseModel):
    hypothesis_id: str
    status: str
    fingerprint: str


class ExperimentResponse(BaseModel):
    experiment_id: str
    is_new_best: bool
    rank: int
    improvement_over_baseline_pct: float
    hypothesis_status_updated_to: Optional[str] = None


class IterationResponse(BaseModel):
    experiment_id: str
    hypothesis_id: str
    is_new_best: bool
    beats_own_best: bool
    rank: int
    runs: int
    improvements: int
    runs_since_improvement: int
