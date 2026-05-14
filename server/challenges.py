"""Single source of truth for challenge definitions on the server side.

Before this module existed, the canonical list of challenges was duplicated
between ``server/models.py:ChallengeName`` (a Literal union) and
``setup.py:CHALLENGES`` (a dict with the per-challenge defaults). Adding
or renaming a challenge required edits in both places — and the wizard
silently no-op'd if you forgot setup.py.

Now both files import from here. The Literal stays static (Python's type
system requires it), but the validation that the Literal matches the
registry runs at import time — so adding a 6th challenge means: add an
entry below, then add the same key string to the Literal in models.py.
A dev who forgets the second step gets an ImportError at server boot,
not a quietly-broken endpoint at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ChallengeDef:
    """Server-side metadata for one challenge.

    Mirrored on the dashboard by ``dashboard/src/lib/challengeRegistry.ts``.
    Fields here are wire-shape only; UI metadata (panel factory, pretty
    name, score label) lives in the dashboard registry.
    """
    name: str
    scoring_direction: Literal["min", "max"]
    track_keys: tuple[str, ...]
    strategy_tags: tuple[str, ...]
    default_timeout: int = 30
    is_gpu: bool = False


CHALLENGES: dict[str, ChallengeDef] = {
    "satisfiability": ChallengeDef(
        name="satisfiability",
        scoring_direction="max",
        track_keys=(
            "n_vars=5000,ratio=4267",
            "n_vars=7500,ratio=4267",
            "n_vars=10000,ratio=4267",
            "n_vars=100000,ratio=4150",
            "n_vars=100000,ratio=4200",
        ),
        strategy_tags=(
            "construction", "local_search", "metaheuristic",
            "decomposition", "hybrid", "data_structure", "other",
        ),
    ),
    "vehicle_routing": ChallengeDef(
        name="vehicle_routing",
        scoring_direction="max",
        track_keys=(
            "n_nodes=600",
            "n_nodes=700",
            "n_nodes=800",
            "n_nodes=900",
            "n_nodes=1000",
        ),
        strategy_tags=(
            "construction", "local_search", "metaheuristic",
            "constraint_relaxation", "decomposition", "hybrid",
            "data_structure", "other",
        ),
    ),
    "knapsack": ChallengeDef(
        name="knapsack",
        scoring_direction="max",
        track_keys=(
            "n_items=1000,budget=10",
            "n_items=1000,budget=25",
            "n_items=1000,budget=5",
            "n_items=5000,budget=10",
            "n_items=5000,budget=25",
        ),
        strategy_tags=(
            "greedy", "dp", "branch_and_bound", "metaheuristic",
            "decomposition", "hybrid", "data_structure", "other",
        ),
    ),
    "job_scheduling": ChallengeDef(
        name="job_scheduling",
        scoring_direction="max",
        track_keys=(
            "n=20,s=FLOW_SHOP",
            "n=20,s=HYBRID_FLOW_SHOP",
            "n=20,s=JOB_SHOP",
            "n=20,s=FJSP_MEDIUM",
            "n=20,s=FJSP_HIGH",
        ),
        strategy_tags=(
            "greedy", "construction", "local_search", "metaheuristic",
            "constraint_relaxation", "decomposition", "hybrid",
            "data_structure", "other",
        ),
    ),
    "energy_arbitrage": ChallengeDef(
        name="energy_arbitrage",
        scoring_direction="max",
        track_keys=(
            "s=BASELINE",
            "s=CONGESTED",
            "s=MULTIDAY",
            "s=DENSE",
            "s=CAPSTONE",
        ),
        strategy_tags=(
            "greedy", "dp", "local_search", "metaheuristic",
            "decomposition", "hybrid", "data_structure", "other",
        ),
    ),
    "hypergraph": ChallengeDef(
        name="hypergraph",
        scoring_direction="max",
        track_keys=(
            "n_h_edges=10000",
            "n_h_edges=20000",
            "n_h_edges=50000",
            "n_h_edges=100000",
            "n_h_edges=200000",
        ),
        strategy_tags=(
            "greedy", "construction", "local_search", "metaheuristic",
            "decomposition", "hybrid", "data_structure", "other",
        ),
        default_timeout=60,
        is_gpu=True,
    ),
    "neuralnet_optimizer": ChallengeDef(
        name="neuralnet_optimizer",
        scoring_direction="max",
        track_keys=(
            "n_hidden=4",
            "n_hidden=6",
            "n_hidden=8",
            "n_hidden=10",
            "n_hidden=12",
        ),
        strategy_tags=(
            "greedy", "metaheuristic", "hybrid", "data_structure", "other",
        ),
        default_timeout=120,
        is_gpu=True,
    ),
    "vector_search": ChallengeDef(
        name="vector_search",
        scoring_direction="max",
        track_keys=(
            "n_queries=10",
            "n_queries=20",
            "n_queries=50",
            "n_queries=100",
            "n_queries=200",
        ),
        strategy_tags=(
            "construction", "local_search", "metaheuristic",
            "decomposition", "hybrid", "data_structure", "other",
        ),
        default_timeout=60,
        is_gpu=True,
    ),
}


CHALLENGE_NAMES: tuple[str, ...] = tuple(CHALLENGES.keys())

# Fallback when a request omits `challenge` AND the swarm has no
# `active_challenge` set in the singleton config table (i.e. brand-new DB
# before the wizard's first POST /api/swarm_config). Picks the registry's
# first entry so dropping a challenge means one edit here, not five.
DEFAULT_CHALLENGE: str = CHALLENGE_NAMES[0]


def is_known_challenge(name: str) -> bool:
    return name in CHALLENGES


def get_challenge(name: str) -> ChallengeDef:
    return CHALLENGES[name]


def assert_literal_matches_registry(literal_values: tuple[str, ...]) -> None:
    """Called from models.py at import time to fail loudly if the
    `ChallengeName` Literal drifts away from this registry."""
    if set(literal_values) != set(CHALLENGE_NAMES):
        missing = set(CHALLENGE_NAMES) - set(literal_values)
        extra = set(literal_values) - set(CHALLENGE_NAMES)
        raise RuntimeError(
            "ChallengeName Literal in server/models.py is out of sync with "
            "server/challenges.py CHALLENGES. "
            f"Missing from Literal: {sorted(missing)}; "
            f"Extra in Literal: {sorted(extra)}. "
            "Update the Literal so the union matches the registry keys."
        )
