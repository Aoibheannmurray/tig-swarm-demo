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
    strategy_tag: Literal[
        "construction",
        "local_search",
        "metaheuristic",
        "constraint_relaxation",
        "decomposition",
        "hybrid",
        "data_structure",
        "other",
    ]
    parent_hypothesis_id: Optional[str] = None


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
    route_data: Optional[dict] = None


class IterationCreate(BaseModel):
    agent_id: str
    title: str
    description: str = ""
    strategy_tag: Literal[
        "construction",
        "local_search",
        "metaheuristic",
        "constraint_relaxation",
        "decomposition",
        "hybrid",
        "data_structure",
        "other",
    ] = "other"
    algorithm_code: str = ""
    score: float
    feasible: bool = True
    num_vehicles: int = 0
    total_distance: float = 0.0
    notes: str = ""
    route_data: Optional[dict] = None


class AdminAuth(BaseModel):
    admin_key: str


class AdminBroadcast(AdminAuth):
    message: str
    priority: Literal["normal", "high"] = "normal"


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


class SwarmConfigUpdate(AdminAuth):
    challenge: ChallengeName
    tracks: dict
    timeout: int
    scoring_direction: Literal["min", "max"]
    swarm_name: str = ""
    owner_name: str = ""
    stagnation_threshold: int = 2
    stagnation_limit: int = 10
    hypothesis_recall_threshold: int = 3
    # Source of `initial_algorithm.rs` from the host's clone; broadcast as
    # the starting code for every fresh trajectory (first iteration + the
    # "fresh start" slot of trajectory resets). Empty string is fine and
    # means "agents start from nothing."
    initial_algorithm_code: str = ""


class MessageCreate(BaseModel):
    agent_id: Optional[str] = None
    agent_name: str
    content: str
    msg_type: Literal["agent", "milestone"] = "agent"


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
