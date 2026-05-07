"""Server → dashboard WebSocket event schema.

Until this module existed, every `manager.broadcast({...})` call site in
server.py constructed an ad-hoc dict, and the dashboard re-declared the
union by hand in `dashboard/src/types.ts`. The two had already drifted
(LeaderboardUpdate missing `challenge`, StatsUpdate missing
`per_challenge`, hypothesis_proposed defined client-side but never
actually emitted by the server).

This file is the single source of truth for what the server sends. Every
broadcast goes through `WSEvent` so type errors fail at import time, not
at runtime in some panel that silently dropped a missing field.

The TS counterpart (`dashboard/src/types.ts`) is hand-mirrored. Run
``python -m server.ws_events --dump-schema`` to dump every event's
JSON schema; CI hashes the output so any change here without a matching
edit to ``types.ts`` shows up as a drift.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# A challenge id on the wire. Loosely typed as `str` here so this module
# stays free of circular imports against server.challenges; validation of
# the full enumeration happens at the API-layer boundary in models.py.
ChallengeId = str


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: str


class AgentJoined(_EventBase):
    type: Literal["agent_joined"] = "agent_joined"
    agent_id: str
    agent_name: str


class TrajectoryReset(_EventBase):
    type: Literal["trajectory_reset"] = "trajectory_reset"
    challenge: ChallengeId
    agent_name: str
    agent_id: str
    reset_type: Literal["fresh_start", "adopted_inactive"]


class ExperimentPublished(_EventBase):
    type: Literal["experiment_published"] = "experiment_published"
    challenge: ChallengeId
    experiment_id: str
    agent_name: str
    agent_id: str
    score: float
    feasible: bool
    improvement_pct: float
    delta_vs_best_pct: Optional[float] = None
    beats_own_best: bool = False
    delta_vs_own_best_pct: Optional[float] = None
    num_instances: int
    is_new_best: bool
    hypothesis_id: Optional[str] = None
    strategy_tag: Optional[str] = None
    title: Optional[str] = None
    notes: str = ""
    track_scores: Optional[dict[str, float]] = None


class NewGlobalBest(_EventBase):
    type: Literal["new_global_best"] = "new_global_best"
    challenge: ChallengeId
    experiment_id: str
    agent_name: str
    agent_id: str
    score: float
    improvement_pct: float
    incremental_improvement_pct: Optional[float] = None
    num_instances: int
    # Per-challenge solution shape; opaque to the WS layer.
    solution_data: Optional[Any] = None
    track_scores: Optional[dict[str, float]] = None


class _LeaderboardEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


class LeaderboardUpdate(_EventBase):
    type: Literal["leaderboard_update"] = "leaderboard_update"
    # Was missing on the dashboard side until this module landed; the
    # dashboard's challenge-scoped filter (main.ts) was already keying on
    # `challenge`, but the type didn't list it.
    challenge: ChallengeId
    entries: list[_LeaderboardEntry]


class _StatsPerChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_agents: int
    best_score: Optional[float] = None
    baseline_score: Optional[float] = None
    num_instances: int
    improvement_pct: float
    total_experiments: int
    hypotheses_count: int
    total_trajectories: int


class StatsUpdate(_EventBase):
    type: Literal["stats_update"] = "stats_update"
    active_challenge: ChallengeId
    # Map: challenge_id → counters. The dashboard slices by viewed
    # challenge before populating its per-panel state.
    per_challenge: dict[str, _StatsPerChallenge]
    total_agents: int


class ChatMessage(_EventBase):
    type: Literal["chat_message"] = "chat_message"
    challenge: ChallengeId
    message_id: str
    agent_name: str
    agent_id: Optional[str] = None
    content: str
    msg_type: Literal["agent", "milestone"]


class AdminBroadcastEvt(_EventBase):
    type: Literal["admin_broadcast"] = "admin_broadcast"
    message: str
    priority: Literal["normal", "high"]


class ResetEvt(_EventBase):
    type: Literal["reset"] = "reset"
    challenge: ChallengeId


class SwarmConfigUpdated(_EventBase):
    type: Literal["swarm_config_updated"] = "swarm_config_updated"
    active_challenge: ChallengeId
    available_challenges: dict[str, Any]
    scoring_direction: Literal["min", "max"]
    swarm_name: str


# Discriminated union — every emitted event must be one of these.
WSEvent = Annotated[
    Union[
        AgentJoined,
        TrajectoryReset,
        ExperimentPublished,
        NewGlobalBest,
        LeaderboardUpdate,
        StatsUpdate,
        ChatMessage,
        AdminBroadcastEvt,
        ResetEvt,
        SwarmConfigUpdated,
    ],
    Field(discriminator="type"),
]


def event_to_payload(event: BaseModel) -> dict[str, Any]:
    """Serialize an event for `manager.broadcast`. Centralised so any
    future schema-level wrapping (envelope, schema version, etc.) lives
    in one place."""
    return event.model_dump(mode="json")


def _dump_schema() -> None:
    """Dump JSON schemas for every WS event as one stable JSON object.

    The dashboard's ``types.ts`` is hand-mirrored, not codegened — but
    CI can still detect drift by hashing this output. If a field is
    added/renamed/retyped here, the hash changes; the matching change
    in ``types.ts`` is then a manual but auditable step.
    """
    import json

    out = {
        name: model.model_json_schema()
        for name, model in [
            ("AgentJoined", AgentJoined),
            ("TrajectoryReset", TrajectoryReset),
            ("ExperimentPublished", ExperimentPublished),
            ("NewGlobalBest", NewGlobalBest),
            ("LeaderboardUpdate", LeaderboardUpdate),
            ("StatsUpdate", StatsUpdate),
            ("ChatMessage", ChatMessage),
            ("AdminBroadcast", AdminBroadcastEvt),
            ("Reset", ResetEvt),
            ("SwarmConfigUpdated", SwarmConfigUpdated),
        ]
    }
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    import sys

    if "--dump-schema" in sys.argv:
        _dump_schema()
    else:
        print("usage: python -m server.ws_events --dump-schema", file=sys.stderr)
        sys.exit(2)
