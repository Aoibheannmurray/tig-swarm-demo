from pydantic import BaseModel
from typing import Literal, Optional, get_args
import uuid

# Import the registry so we can fail loudly at import time if the Literal
# below drifts away from server/challenges.py. The Literal itself stays
# static — Python's type checker needs concrete strings — but the assert
# at the bottom of this file proves the union and the registry agree.
from challenges import (
    CHALLENGE_NAMES,
    assert_literal_matches_registry,
)


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
    # Optional: contributor's own preferred display name. The server falls
    # back to its built-in name generator when this is omitted or already
    # taken (so the wizard's "use default" path still works).
    agent_name: Optional[str] = None
    # Optional: free-form label identifying which LLM is driving this agent
    # (e.g. "claude_code", "gemini_api", "gpt-4o"). Surfaced on the dashboard
    # so projected swarms can see what each contributor is running.
    llm_type: Optional[str] = None


class HeartbeatRequest(BaseModel):
    status: Literal["idle", "working"] = "working"
    current_hypothesis_id: Optional[str] = None


class RenameRequest(BaseModel):
    agent_name: str


class IterationCreate(BaseModel):
    agent_id: str
    title: str
    description: str = ""
    strategy_tag: str = "other"
    algorithm_code: str = ""
    kernel_code: Optional[str] = None
    score: float
    feasible: bool = True
    notes: str = ""
    solution_data: Optional[dict] = None
    track_scores: Optional[dict] = None
    challenge: Optional["ChallengeName"] = None
    challenge_metrics: Optional[dict] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None


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


class AdminSeedInactive(AdminAuth):
    """Owner-only: deposit an externally-sourced algorithm into the
    `inactive_algorithms` pool so the next stagnated agent that doesn't
    qualify for a fresh start adopts it (server.py's `adopted_inactive`
    path). Used at swarm-create time to seed the pool with the current
    top-earning TIG mainnet algorithm.

    Restricted server-side to {knapsack, satisfiability} — the only
    challenges whose mainnet algorithms ship as a single mod.rs (+ optional
    kernels.cu), which is what the `agent_bests` / `inactive_algorithms`
    wire format expects today.
    """
    challenge: "ChallengeName"
    algorithm_code: str
    kernel_code: Optional[str] = None
    # Free-form label for the synthetic agent the pool entry is attributed
    # to (e.g. "tig-foundation"). The server creates the agent on first use.
    source_label: str = "tig-foundation"


# Swarm-wide configuration set by the owner via the setup wizard.
# challenge: which TIG challenge this swarm is optimizing.
# tracks: keys are track labels (e.g. "n_nodes=600"), values are instance counts.
# timeout: per-instance solver timeout in seconds.
# scoring_direction: "min" (smaller score wins) or "max" (larger wins).
# swarm_name / owner_name are display-only.
ChallengeName = Literal[
    "satisfiability",
    "vehicle_routing",
    "knapsack",
    "job_scheduling",
    "energy_arbitrage",
    "hypergraph",
    "neuralnet_optimizer",
    "vector_search",
]


class ChallengeSubConfig(BaseModel):
    """Per-challenge configuration. The owner can populate all seven in
    parallel via the wizard; switching the active challenge is independent."""
    tracks: dict = {}
    timeout: int = 30
    scoring_direction: Literal["min", "max"] = "max"
    initial_algorithm_code: str = ""
    initial_kernel_code: str = ""
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
    swarm_type: Optional[Literal["cpu", "gpu"]] = None
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


# Boot-time consistency check. If you add a 6th challenge by editing the
# registry but forget to extend ChallengeName above (or vice versa), the
# server fails to import with a clear message — instead of half-broken
# endpoints that silently reject an unknown name string at request time.
assert_literal_matches_registry(get_args(ChallengeName))
