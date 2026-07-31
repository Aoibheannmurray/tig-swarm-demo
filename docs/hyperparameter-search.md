# Hyperparameter search (HPO)

How the swarm tunes an algorithm's hyperparameters, and when it decides to.
Code: `scripts/hpo.py` (the search), the gate in `scripts/run_loop.py`
(`_should_tune_hyperparameters` / `_maybe_tune_hyperparameters`), storage in
`server/db.py` (`improvement_scores`, `trajectory_has_tuned`).

This replaces an internal planning doc that older comments cited as
`docs/hyperparameter-search-plan.md`; it describes the system as shipped.

## The search

A deliberately "decent, not optimal" random search. Given a
hyperparameter-enabled variant of the algorithm and a search space (both
produced by an extraction LLM call), it benchmarks a fixed budget of `N`
configurations (`hpo_search_budget`, default 13) and keeps the best:

- the **default config** `{}` is always included — the variant falls back to
  its in-code defaults, so the tuned winner can never score worse than the
  untuned algorithm;
- up to `hpo_num_suggested_configs` LLM-suggested configs (default 5);
- random draws from the search space for the remainder.

The search runs on a **non-test seed** (`hpo_seed`, default `"hpo"`), so it
never fits to the scored instances; only the winning config is re-scored on
the test seed. Winners are stored **per track** — each track keeps the config
that scored best on *that* track. Feasibility is a hard gate when comparing
trials: a feasible trial beats an infeasible one regardless of score.

The search space schema is a list of typed entries:

```json
{"name": "learning_rate", "type": "float", "range": [1e-4, 1e-1], "scale": "log"}
{"name": "n_restarts",    "type": "int",   "range": [1, 16]}
{"name": "strategy",      "type": "categorical", "choices": ["greedy", "random"]}
```

## The gate — when a tune fires

Tuning costs a benchmark per configuration, so it only fires on trajectories
that have earned it:

1. **First tune** on a trajectory: fires once the trajectory has recorded
   `hpo_first_tune_improvements` improvements (default 10) — no further
   condition.
2. **After that**: fires only when the candidate's *default* score lands in
   the improvement band — better than the score from `hpo_min_improvements`
   improvements ago (default 4, the band floor) but not yet better than the
   parent (the band ceiling). In other words: the trajectory is climbing but
   this edit didn't beat the best on its own — tuning may push it over.

The band compares **default-vs-default**: the server stores each iteration's
no-hyperparameters score (`default_score`) alongside the published (possibly
tuned) score, so an ancestor that tuned never raises the bar for its
descendants. Gate state is keyed by `trajectory_id`, so it survives adoption
from the inactive pool and process restarts.

## Knobs

All host-tunable in `fleet.config.json` (top level = fleet-wide default,
per-agent override wins) and hot-reloadable — see
`scripts/agent_config_keys.py`: `hpo_min_improvements`,
`hpo_first_tune_improvements`, `hpo_num_suggested_configs`,
`hpo_search_budget`, `hpo_seed`.
