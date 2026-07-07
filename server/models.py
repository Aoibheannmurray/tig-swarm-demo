import json
from pydantic import BaseModel, Field, field_validator
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

# Size ceilings on client-writable payloads — generous enough that no
# legitimate client ever hits them (algorithms are a few KB; the biggest
# solution_data blobs observed are ~100 KB), but they stop a compromised
# token from ballooning the SQLite file with multi-GB writes.
MAX_CODE_LEN = 2 * 1024 * 1024        # algorithm_code / solution_data / one file
MAX_FILES_TOTAL_LEN = 8 * 1024 * 1024  # whole algorithm_files map
MAX_NOTES_LEN = 64 * 1024
MAX_LABEL_LEN = 300                    # title / agent_name / llm_type
MAX_MESSAGE_LEN = 32 * 1024


class RegisterRequest(BaseModel):
    client_version: str = "1.0"
    # Optional: contributor's own preferred display name. The server falls
    # back to its built-in name generator when this is omitted or already
    # taken (so the wizard's "use default" path still works).
    agent_name: Optional[str] = Field(default=None, max_length=MAX_LABEL_LEN)
    # Optional: free-form label identifying which LLM is driving this agent
    # (e.g. "claude_code", "gemini_api", "gpt-4o"). Surfaced on the dashboard
    # so projected swarms can see what each contributor is running.
    llm_type: Optional[str] = Field(default=None, max_length=MAX_LABEL_LEN)
    # Optional structured provider/model. When supplied, used for tier
    # auto-classification (server/tiers.py); otherwise the server falls back
    # to parsing the llm_type label. Both optional for backward compatibility.
    provider: Optional[str] = None
    model: Optional[str] = None


class HeartbeatRequest(BaseModel):
    status: Literal["idle", "working"] = "working"


class RenameRequest(BaseModel):
    agent_name: str = Field(max_length=MAX_LABEL_LEN)


class IterationCreate(BaseModel):
    agent_id: str
    title: str = Field(max_length=MAX_LABEL_LEN)
    description: str = ""
    strategy_tag: str = "other"
    algorithm_code: str = Field(default="", max_length=MAX_CODE_LEN)
    kernel_code: Optional[str] = None
    # Full multi-file algorithm as a {relpath: content} map (keys relative to the
    # algorithm dir; `mod.rs` is the entry). None/absent for single-file clients,
    # where `algorithm_code` is the whole algorithm. When present it is the
    # source of truth and `algorithm_code` holds the entry file for back-compat.
    algorithm_files: Optional[dict] = None
    score: float
    feasible: bool = True
    notes: str = Field(default="", max_length=MAX_NOTES_LEN)
    solution_data: Optional[dict] = None
    track_scores: Optional[dict] = None
    challenge: Optional["ChallengeName"] = None
    challenge_metrics: Optional[dict] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None
    # The publishing agent's role at iteration time: "explorer" or "exploiter"
    # (None for legacy clients). Stored on the hypothesis row for attribution.
    role: Optional[str] = None
    # Winning hyperparameter config when this iteration was tuned (the `score`
    # is then the tuned score); None means the algorithm was scored at its
    # in-code defaults. When tuned this is a per-track map
    # {track_key: {param: value}} (a winner per track); see
    # docs/hyperparameter-search-plan.md.
    hyperparameters: Optional[dict] = None
    # The candidate's no-hyperparameters (default-config) score on the test
    # seed. Equals `score` for untuned iterations; for tuned ones it is the
    # pre-tuning score. The HPO gate's band compares default-vs-default, so the
    # server stores this and serves it back via improvement_scores. None for
    # legacy clients — the server falls back to `score`.
    default_score: Optional[float] = None
    # "mutation" (default) or "refactor". A refactor is a behavior-preserving
    # bloat reduction (docs/cleaner-agent-plan.md): the server swaps the
    # trajectory-best CODE for the leaner version but KEEPS the recorded best
    # score (no ratchet erosion), and counts it as neither an improvement nor
    # stagnation.
    iteration_type: str = "mutation"

    @field_validator("solution_data")
    @classmethod
    def _solution_data_size(cls, v):
        if v is not None and len(json.dumps(v)) > MAX_CODE_LEN:
            raise ValueError("solution_data too large")
        return v

    @field_validator("algorithm_files")
    @classmethod
    def _algorithm_files_size(cls, v):
        if v is not None:
            total = 0
            for path, content in v.items():
                size = len(content) if isinstance(content, str) else len(json.dumps(content))
                if size > MAX_CODE_LEN:
                    raise ValueError(f"algorithm_files[{path!r}] too large")
                total += size
            if total > MAX_FILES_TOTAL_LEN:
                raise ValueError("algorithm_files too large in total")
        return v


class AdminAuth(BaseModel):
    admin_key: str


class AdminBroadcast(AdminAuth):
    message: str
    priority: Literal["normal", "high"] = "normal"


class AdminResetChallenge(AdminAuth):
    """Owner-only request to clear the leaderboard for a single challenge:
    drops `best_history` + `trajectory_bests` rows for that challenge so the next
    feasible publish becomes the new global best. Preserves `experiments`,
    `hypotheses`, and `agent_challenge_state` so prior research and per-agent
    counters (run counts, trajectories, stagnation) remain intact."""
    challenge: "ChallengeName"


class AdminRevoke(AdminAuth):
    """Owner-only: revoke a contributor by username so they can no longer
    register new agents. Existing agents owned by that contributor have
    their per-agent token cleared in the same call, so in-flight workers
    stop authenticating immediately. Agent rows (and their history) are
    preserved — only the auth token + the entry in config.revoked_contributors
    are touched."""
    username: str


class AdminSeedInactive(AdminAuth):
    """Owner-only: deposit an externally-sourced algorithm into the
    `inactive_algorithms` pool so the next stagnated agent that doesn't
    qualify for a fresh start adopts it (server.py's `adopted_inactive`
    path). Used at swarm-create time to seed the pool with the current
    top-earning TIG mainnet algorithm.

    Supports every challenge, single- or multi-file. `algorithm_code` always
    carries the entry file (`mod.rs`) for single-file/back-compat consumers;
    `algorithm_files` carries the full {relpath: content} map (multiple `.rs`
    and multiple `.cu` kernels, names preserved) and is the source of truth on
    adoption when present — `_row_files` / the `adopted_inactive` branch already
    round-trip it.
    """
    challenge: "ChallengeName"
    algorithm_code: str
    kernel_code: Optional[str] = None
    # Full multi-file algorithm as a {relpath: content} map (keys relative to
    # the algorithm dir; `mod.rs` is the entry). None/absent for single-file
    # seeds, where `algorithm_code` is the whole algorithm. When present it is
    # the source of truth and `algorithm_code` holds the entry file.
    algorithm_files: Optional[dict] = None
    # Free-form label for the synthetic agent the pool entry is attributed
    # to (e.g. "tig-foundation"). The server creates the agent on first use.
    source_label: str = "tig-foundation"


class AdminClearInactive(AdminAuth):
    """Owner-only: empty the `inactive_algorithms` pool for a challenge.

    Use to remove stale/diluting inactive trajectories so agents reliably adopt
    a specific seed on their next reset. `keep_source_label` preserves entries
    attributed to that synthetic source (e.g. a just-seeded test algorithm);
    everything else on the challenge is deleted. Note the pool refills over time
    as stagnating agents deposit their feasible bests, so this is a point-in-time
    clear, not a permanent state.
    """
    challenge: "ChallengeName"
    keep_source_label: Optional[str] = None


class AdminSeedPool(AdminAuth):
    """Owner-only: deposit a host-authored seed algorithm into `seed_pool`.

    Seeds are working starter algorithms handed to standard-tier / exploiter
    agents on a fresh trajectory (instead of the bare stub). Deduped by
    (challenge, strategy_tag, source='authored') via the UNIQUE index — one
    authored seed per (challenge, strategy_tag). Used at swarm-create time to
    load any `initial_algorithms/<challenge>/seeds/*.rs` files the host wrote.
    """
    challenge: "ChallengeName"
    strategy_tag: str
    algorithm_code: str
    kernel_code: Optional[str] = None
    score: Optional[float] = None


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
    # Edits a trajectory may accumulate while its best is still not better
    # than 0 before it gets reset. 0 = disabled (see SWARM_DEFAULTS).
    negative_trajectory_limit: Optional[int] = None
    hypothesis_recall_threshold: Optional[int] = None


class MessageCreate(BaseModel):
    agent_id: Optional[str] = None
    agent_name: str = Field(max_length=MAX_LABEL_LEN)
    content: str = Field(max_length=MAX_MESSAGE_LEN)
    msg_type: Literal["agent", "milestone"] = "agent"
    challenge: Optional[ChallengeName] = None


# ── Response models ──

class AgentResponse(BaseModel):
    agent_id: str
    agent_name: str
    agent_token: str
    registered_at: str
    config: dict


class IterationResponse(BaseModel):
    experiment_id: str
    hypothesis_id: str
    is_new_best: bool
    beats_trajectory_best: bool
    rank: int
    runs: int
    improvements: int
    runs_since_improvement: int


# Boot-time consistency check. If you add a 6th challenge by editing the
# registry but forget to extend ChallengeName above (or vice versa), the
# server fails to import with a clear message — instead of half-broken
# endpoints that silently reject an unknown name string at request time.
assert_literal_matches_registry(get_args(ChallengeName))
