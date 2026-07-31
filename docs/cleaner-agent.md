# Cleaner — deterministic bloat reduction

Why algorithms don't grow without bound, and what a `refactor` iteration is.
Code: `scripts/cleaner_prepass.py` (the LLM-free pass), the trigger in
`scripts/run_loop.py` (search for "Cleaner:"), the server-side swap in
`server/server.py` (`iteration_type="refactor"`).

This replaces an internal planning doc that older comments cited as
`docs/cleaner-agent-plan.md`; it describes the system as shipped.

## The problem

Iterative LLM editing accretes code: dead files, duplicated helpers,
abandoned experiments. Past ~500 KB an algorithm slows every prompt (it rides
in the context), every search/replace call, and every benchmark upload.

## What happens

When a trajectory's best outgrows `cleaner_trigger_chars` (default 500,000
chars), the agent spends one iteration cleaning instead of mutating:

1. Run the **Tier-0 pre-pass** (`cleaner_prepass.py`): duplicate-file merge
   and unreachable-file removal. Purely structural — no LLM call, so it can't
   invent behavior changes.
2. Benchmark the cleaned code as usual.
3. Publish it as `iteration_type="refactor"` **only if** the score stays
   within `cleaner_score_delta_pct` (default 2%) of the parent *and* the size
   dropped to ≤ `cleaner_target_pct` (default 60%) of the original.

On a refactor the server swaps in the lean code but **keeps the parent's
score** — no ratchet erosion from benchmark noise — and counts it as neither
an improvement nor stagnation. A cooldown (`cleaner_cooldown_iters`, default
15) stops back-to-back cleaning; above ~80% of the trigger size, prompts also
get a "prefer size-reducing edits" steer, since bloat prevented is a
benchmark never spent.

## Knobs

All host-tunable in `fleet.config.json` and hot-reloadable — see
`scripts/agent_config_keys.py`: `cleaner_trigger_chars`,
`cleaner_target_pct`, `cleaner_score_delta_pct`, `cleaner_cooldown_iters`.
