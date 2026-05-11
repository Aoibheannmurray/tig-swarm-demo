"""Pydantic schemas for previously-implicit REST endpoints.

Before this module, ``/api/state``, ``/api/replay``, and ``/api/diversity``
returned ad-hoc dicts assembled inline in server.py — the dashboard's
``types.ts`` redeclared them by hand and the agent loop dict-poked them
with ``python3 -c`` snippets in CLAUDE.md. The contract was implicit,
documented only in field comments scattered across server.py.

This module makes those contracts explicit. Each endpoint that has a
model here imports it and either passes it as ``response_model`` (when
the response shape is small and the filtering is safe) or instantiates
the model and serialises with ``model.model_dump(mode="json")`` (when
the existing dict construction is too tangled to flip in one go).

The TS mirrors live in ``dashboard/src/types.ts``. The drift-check
pattern from ws_events.py applies here — when you change a field below
you must update the matching ``types.ts`` interface by hand.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class _ResponseBase(BaseModel):
    # We do NOT use extra="forbid" here — these endpoints have grown
    # optional fields over time and existing dashboards expect to see
    # them. The schema documents the wire form; FastAPI will pass through
    # extra keys that aren't on the model.
    model_config = ConfigDict(extra="allow")


# ── /api/replay ──────────────────────────────────────────────────────────


class ReplayRow(_ResponseBase):
    """One entry in the best-history replay stream.

    This is what every per-challenge visualization panel's ``fetchHistory``
    consumes. Formalising it lets ``DisplayPanelBase.fetchHistory`` pass
    rows to a typed ``parseReplayRow`` hook (Phase 2 already lays the
    groundwork — subclasses can read this shape directly).

    ``solution_data`` is omitted (set to None at the wire level) when
    the caller passes ``compact=1`` — the chart panel does this to save
    100+ KB per page-load.
    """
    experiment_id: str
    agent_id: Optional[str] = None
    agent_name: str
    score: float
    created_at: str
    # Per-challenge solution shape; opaque at this layer.
    solution_data: Optional[Any] = None


class ReplayCompactRow(_ResponseBase):
    """``compact=1`` variant — strictly score/agent/timestamp."""
    experiment_id: str
    agent_id: Optional[str] = None
    agent_name: str
    score: float
    created_at: str


# ── /api/diversity ───────────────────────────────────────────────────────


class DiversityAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    agent_name: str


class DiversityResponse(BaseModel):
    """N×N matrix of pairwise code-diversity ratios.

    ``matrix[i][j]`` semantics:
      - ``i == j``: fraction of agent i's lines that are unique to them
        (no other agent has those lines).
      - ``i != j``: fraction of agent i's lines that also appear in
        agent j's program.
    """
    model_config = ConfigDict(extra="forbid")
    agents: list[DiversityAgent]
    matrix: list[list[float]]


# ── /api/state — documentation only ──────────────────────────────────────


class StateRecentExperiment(_ResponseBase):
    id: str
    agent_name: str
    score: float
    feasible: bool
    is_new_best: bool
    improvement_pct: float
    delta_vs_best_pct: Optional[float] = None
    delta_vs_own_best_pct: Optional[float] = None
    beats_own_best: bool = False
    created_at: str
    notes: Optional[str] = None


class StateRecentHypothesis(_ResponseBase):
    id: str
    title: str
    strategy_tag: str
    agent_name: str
    description: str
    parent_hypothesis_id: Optional[str] = None
    agent_id: str = ""
    created_at: Optional[str] = None


class StateLeaderboardEntry(_ResponseBase):
    rank: int
    agent_id: str
    agent_name: str
    runs: int
    improvements: int
    runs_since_improvement: int
    current_score: Optional[float] = None
    best_ever_score: Optional[float] = None
    num_trajectories: int
    tacit_knowledge_count: int
    inspiration_count: int
    active: bool


class StateResponse(_ResponseBase):
    """Documentation-only schema for ``/api/state``.

    NOT YET enforced as a FastAPI ``response_model``. The endpoint still
    returns hand-built dicts in server.py; this model captures the wire
    form so the dashboard's ``types.ts`` and CLAUDE.md's agent-loop
    snippets have a single named contract to reference.

    Agent-loop view (``?agent_id=…``) and dashboard view (no agent_id)
    diverge — fields that only appear in one are marked Optional. A
    later PR can split this into ``AgentStateResponse`` /
    ``DashboardStateResponse`` once the call-site dict construction in
    server.py is itself refactored.
    """
    challenge: str

    # Always present
    best_score: Optional[float] = None
    num_instances: int
    active_agents: int
    total_agents: int
    total_experiments: int
    hypotheses_count: int

    # Agent-loop view fields
    is_gpu: Optional[bool] = None
    best_algorithm_code: Optional[str] = None
    best_kernel_code: Optional[str] = None
    best_experiment_id: Optional[str] = None
    my_best_score: Optional[float] = None
    my_runs: Optional[int] = None
    my_improvements: Optional[int] = None
    my_runs_since_improvement: Optional[int] = None
    prior_hypotheses: Optional[list[dict]] = None
    hypothesis_recall_message: Optional[str] = None
    inspiration_code: Optional[str] = None
    inspiration_kernel_code: Optional[str] = None
    inspiration_agent_name: Optional[str] = None
    stagnation_hint: Optional[str] = None
    trajectory_reset: Optional[dict] = None

    # Dashboard view fields
    baseline_score: Optional[float] = None
    improvement_pct: Optional[float] = None
    best_solution_data: Optional[Any] = None
    best_track_scores: Optional[dict] = None
    total_trajectories: Optional[int] = None
    recent_experiments: Optional[list[StateRecentExperiment]] = None
    recent_hypotheses: Optional[list[StateRecentHypothesis]] = None

    # Both views
    leaderboard: Optional[list[StateLeaderboardEntry]] = None
