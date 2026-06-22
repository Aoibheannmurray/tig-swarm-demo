"""Hyperparameter search for a mutated algorithm.

This is the "decent, not optimal" random search described in
`docs/hyperparameter-search-plan.md`. Given a hyperparameter-enabled variant of
an algorithm, a search space, and a handful of LLM-suggested configurations, it
evaluates a fixed budget of `N` configurations and returns the best one.

Composition of the `N` evaluated configs (default N=13):
  - the **default config** (`{}`) — the solver/variant falls back to its in-code
    defaults, reproducing the "default score". Including it guarantees the
    winning (tuned) score is never worse than the default score.
  - up to `num_suggested_configs` LLM-suggested configs (default 5).
  - random draws from the search space for the remainder.

The search is run on a NON-test seed (so it never touches the scored instances);
the winning config is re-scored on the test seed by the caller.

This module is deliberately decoupled from the compute layer: the caller injects
a `benchmark_fn(seed, hyperparameters) -> (bench_dict | None, err_str)` so the
same search works for local Docker and C3 cloud GPU, and so the module is unit
testable without Docker. `bench_dict` is the `benchmark.py` JSON (it must carry
`"score"` and `"feasible"`).

Search-space schema (emitted by the extraction LLM, Phase 3) — a list of:
    {"name": "learning_rate", "type": "float", "range": [1e-4, 1e-1], "scale": "log"}
    {"name": "n_restarts",    "type": "int",   "range": [1, 16],     "scale": "linear"}
    {"name": "strategy",      "type": "categorical", "choices": ["greedy", "random"]}
`scale` defaults to "linear"; `type` defaults to "float".
"""

from __future__ import annotations

import json
import math
import random
from typing import Callable

DEFAULT_N = 13
DEFAULT_NUM_SUGGESTED = 5
DEFAULT_HPO_SEED = "hpo"

# A trial result: the config tried plus its benchmark outcome.
# {"config": {...}, "score": float | None, "feasible": bool, "error": str | None}

BenchmarkFn = Callable[[str, str], "tuple[dict | None, str]"]


def _is_better(direction: str, a_score: float, a_feasible: bool,
               b_score: float, b_feasible: bool) -> bool:
    """Is trial A strictly better than trial B?

    Feasibility is a hard gate (mirrors the server's feasibility-gated
    `is_better`): a feasible trial always beats an infeasible one, regardless of
    raw score. Among trials of equal feasibility, higher score wins (every TIG
    challenge is higher-is-better; `direction` is accepted for forward-compat).
    """
    if a_feasible != b_feasible:
        return a_feasible  # feasible beats infeasible
    if direction == "min":
        return a_score < b_score
    return a_score > b_score


def _score_better(direction: str, a: float, b: float) -> bool:
    """Is raw score `a` strictly better than `b`? (no feasibility dimension)."""
    return a < b if direction == "min" else a > b


def _per_track_winners(trials: list[dict], direction: str) -> dict[str, dict]:
    """Select the best config per track from the collected trials.

    For each track key seen in any trial's `track_scores`, pick the config whose
    score on *that track* is best. Per-track means already bake in feasibility
    (infeasible instances clamp a track's mean to the worst feasible value — see
    `benchmark.aggregate`), so a direction-aware comparison on the track score
    both prefers feasible configs and avoids infeasible-heavy ones, without a
    separate feasibility dimension. The default config `{}` is among the trials,
    so each track's winner is never worse than that track's default *on the HPO
    seed*. Returns {track_key: config}; empty if no trial produced track scores.
    """
    best_cfg: dict[str, dict] = {}
    best_score: dict[str, float] = {}
    for trial in trials:
        track_scores = trial.get("track_scores") or {}
        cfg = trial["config"]
        for tk, sc in track_scores.items():
            if sc is None:
                continue
            sc = float(sc)
            if tk not in best_cfg or _score_better(direction, sc, best_score[tk]):
                best_cfg[tk] = cfg
                best_score[tk] = sc
    return best_cfg


def sample_config(search_space: list[dict], rng: random.Random) -> dict:
    """Draw one random configuration from the search space."""
    cfg: dict = {}
    for param in search_space:
        name = param["name"]
        ptype = param.get("type", "float")
        if ptype == "categorical":
            cfg[name] = rng.choice(param["choices"])
            continue
        lo, hi = param["range"]
        if param.get("scale", "linear") == "log":
            if lo <= 0:
                raise ValueError(f"log-scaled param {name!r} needs a positive range")
            value = math.exp(rng.uniform(math.log(lo), math.log(hi)))
        else:
            value = rng.uniform(lo, hi)
        cfg[name] = int(round(value)) if ptype == "int" else value
    return cfg


def _config_key(cfg: dict) -> str:
    """Stable key for de-duplicating configs (order-independent)."""
    return json.dumps(cfg, sort_keys=True)


def build_config_set(
    search_space: list[dict],
    suggested_configs: list[dict],
    n: int = DEFAULT_N,
    num_suggested: int = DEFAULT_NUM_SUGGESTED,
    rng: random.Random | None = None,
) -> list[dict]:
    """Assemble the `n` configs to evaluate: default + suggested + random.

    Duplicates are skipped; random draws fill whatever budget remains after the
    default and the (capped) suggested configs. If the search space is tiny and
    random draws keep colliding, the set may end up shorter than `n` — that's
    fine, we just evaluate fewer.
    """
    if rng is None:
        rng = random.Random(DEFAULT_HPO_SEED)
    configs: list[dict] = []
    seen: set[str] = set()

    def _add(cfg: dict) -> None:
        key = _config_key(cfg)
        if key not in seen:
            seen.add(key)
            configs.append(cfg)

    _add({})  # default config — guarantees tuned >= default
    for cfg in suggested_configs[:num_suggested]:
        if len(configs) >= n:
            break
        _add(cfg)

    # Fill the rest with random draws. Bound the attempts so a degenerate
    # (tiny / all-categorical) space can't loop forever.
    attempts = 0
    max_attempts = max(50, (n - len(configs)) * 20)
    while len(configs) < n and attempts < max_attempts:
        attempts += 1
        try:
            _add(sample_config(search_space, rng))
        except (KeyError, ValueError):
            break  # malformed space — stop padding, evaluate what we have
    return configs


def search(
    benchmark_fn: BenchmarkFn,
    search_space: list[dict],
    suggested_configs: list[dict],
    *,
    n: int = DEFAULT_N,
    num_suggested: int = DEFAULT_NUM_SUGGESTED,
    hpo_seed: str = DEFAULT_HPO_SEED,
    direction: str = "max",
    log: Callable[[str], None] = print,
) -> dict:
    """Run the search; return the per-track winners and all trial results.

    Returns:
        {
          "winning_configs": dict,         # {track_key: config} — a winner per track
          "winning_config": dict,          # global best (aggregate score); {} if none
          "winning_score": float | None,   # aggregate score of the global best
          "winning_feasible": bool,
          "trials": [ {config, score, feasible, track_scores, error}, ... ],
        }

    There is no single tuned config: `winning_configs` maps each track to the
    config that scored best on *that* track (see docs/hyperparameter-search-plan.md).
    `winning_config` (the best by aggregate cross-track score) is retained for
    logging / back-compat. `winning_configs` is empty when no trial produced a
    per-track breakdown, in which case the caller falls back to the default.
    """
    rng = random.Random(hpo_seed)
    configs = build_config_set(search_space, suggested_configs, n, num_suggested, rng)
    log(f"  [HPO] evaluating {len(configs)} configs on seed '{hpo_seed}' "
        f"(N={n}, suggested={num_suggested})")

    trials: list[dict] = []
    best_cfg: dict | None = None
    best_score: float | None = None
    best_feasible = False

    for i, cfg in enumerate(configs):
        bench, err = benchmark_fn(hpo_seed, json.dumps(cfg))
        if bench is None:
            trials.append({"config": cfg, "score": None, "feasible": False,
                           "track_scores": None, "error": err})
            log(f"  [HPO] {i + 1}/{len(configs)} FAILED: {err[:80]}")
            continue
        score = bench.get("score")
        feasible = bool(bench.get("feasible", False))
        trials.append({"config": cfg, "score": score, "feasible": feasible,
                       "track_scores": bench.get("track_scores"), "error": None})
        tag = "default" if cfg == {} else json.dumps(cfg)
        log(f"  [HPO] {i + 1}/{len(configs)} score={score} feasible={feasible} {tag}")
        if score is None:
            continue
        if best_cfg is None or _is_better(direction, score, feasible, best_score, best_feasible):
            best_cfg, best_score, best_feasible = cfg, score, feasible

    if best_cfg is None:
        best_cfg = {}
    winning_configs = _per_track_winners(trials, direction)
    log(f"  [HPO] global best: score={best_score} feasible={best_feasible} "
        f"config={json.dumps(best_cfg)}")
    log(f"  [HPO] per-track winners: {json.dumps(winning_configs)}")
    return {
        "winning_configs": winning_configs,
        "winning_config": best_cfg,
        "winning_score": best_score,
        "winning_feasible": best_feasible,
        "trials": trials,
    }


if __name__ == "__main__":
    # Dry run: exercise sampling + config-set assembly without Docker. Uses a
    # fake benchmark_fn that scores configs by a toy function so the search has a
    # clear winner, proving the plumbing end to end.
    space = [
        {"name": "lr", "type": "float", "range": [1e-4, 1e-1], "scale": "log"},
        {"name": "k", "type": "int", "range": [1, 16], "scale": "linear"},
        {"name": "mode", "type": "categorical", "choices": ["a", "b"]},
    ]
    # Two configs share the ideal lr/mode but differ in k, so the per-track
    # winners can diverge on k alone (the easy track peaks at k=8, the hard track
    # keeps rewarding larger k).
    suggested = [
        {"lr": 0.01, "k": 8, "mode": "a"},
        {"lr": 0.05, "k": 4, "mode": "b"},
        {"lr": 0.01, "k": 16, "mode": "a"},
    ]

    def fake_benchmark(seed: str, hp_json: str):
        cfg = json.loads(hp_json)
        # Toy objective: peak near lr=0.01, k=8, mode=="a"; empty config is mid.
        if not cfg:
            return {"score": 100.0, "feasible": True,
                    "track_scores": {"easy": 100.0, "hard": 100.0}}, ""
        base = (
            1000.0
            - abs(math.log10(cfg["lr"]) - math.log10(0.01)) * 200
            - abs(cfg["k"] - 8) * 10
            + (50 if cfg["mode"] == "a" else 0)
        )
        # Two tracks with *different* optima so the per-track winners diverge:
        # the "hard" track rewards larger k strongly enough (+15/unit vs the
        # -10/unit |k-8| penalty) that its optimum sits at the top of the k
        # range, pulling its winner away from the easy track's k=8 optimum.
        track_scores = {
            "easy": base,
            "hard": base + cfg["k"] * 15,
        }
        score = sum(track_scores.values()) / len(track_scores)
        return {"score": score, "feasible": True, "track_scores": track_scores}, ""

    result = search(fake_benchmark, space, suggested, n=DEFAULT_N)
    print(json.dumps({k: v for k, v in result.items() if k != "trials"}, indent=2))
    print(f"trials run: {len(result['trials'])}")
    winners = result["winning_configs"]
    assert set(winners) == {"easy", "hard"}, winners
    # The 'easy' track peaks at k=8; the 'hard' track rewards larger k, so its
    # winner's k must be strictly greater — proving per-track selection diverges
    # rather than collapsing to one global config.
    assert winners["easy"]["k"] == 8, winners
    assert winners["hard"]["k"] > winners["easy"]["k"], winners
    print(f"per-track winners diverge as expected ✓ "
          f"(easy k={winners['easy']['k']}, hard k={winners['hard']['k']})")
