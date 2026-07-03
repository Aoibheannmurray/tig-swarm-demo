# Branch summary — `multifile-sr-roles-seed-hpo`

Short summary of everything this branch adds on top of `main`. Design detail:
`docs/hyperparameter-search-plan.md` and the plan doc
`/root/.claude/plans/lets-write-a-plan-purrfect-rabin.md`.

## Core features

- **Multi-file algorithms** — an algorithm is now a `{relpath: content}`
  files-map (JSON), threaded through agent state, worktrees, the publish payload,
  and the server (`algorithm_files` columns on experiments / trajectory_bests /
  seed_pool / inactive_algorithms). Single-file collapses to `{"mod.rs": code}`.

- **Soft search/replace edits** (`scripts/search_replace.py`) — in API mode the
  LLM emits targeted SEARCH/REPLACE blocks instead of whole files: fuzzy match →
  1–2 LLM repair rounds → skip unmatched (no full-rewrite fallback). Exploiters,
  multi-file algorithms, and `edit_mode: search_replace` use it; single-file
  explorers still full-rewrite.

- **Roles ↔ tiers** — `init_fleet` defaults each agent's role from its tier
  (`role_for_tier`: frontier→explorer, standard→exploiter); hypotheses are tagged
  with the role; exploiters are search/replace-only. Role hot-reloads from config.

- **Seed-pool diversity** (`server/seed_diversity.py`) — replaces 1-seed-per-tag
  dedup with code-similarity admission: simple (LOC ceiling), novel, sticky
  seeds; pool capped at K with most-redundant eviction (never by score); random
  per-trajectory selection.

- **Hyperparameter search (HPO)** — both server support (`db` gate state +
  storage) and the client (`scripts/hpo.py`): per-track random search, candidate
  scoring, a firing gate, and extraction of tunable constants. C5: extraction is
  multi-file-aware. Tuned per-track configs published unconditionally (feasibility
  safety only).

## Supporting changes

- **Server** — inactive-pool blocks all negative deposits; leaderboard counts
  only *consumed* hints; `inactive_minutes` default 20→60.
- **Dashboard** — benchmark chart y-axis log/linear toggle + 2-D pan/zoom;
  responsive leaderboard / mobile layout.
- **Challenge** — `neuralnet_optimizer` tracks matched to TIG
  (`n_hidden=4,7,10,14,18`).
- **Docs** — dev guide split into per-directory `CLAUDE.md` files; `ARCHITECTURE`
  moved under `docs/`.

## Live-test hardening (2026-06-24)

- Fixed a `_try_compile_fix` arg-count crash that killed the loop on the
  runtime→compile recovery path.
- Generated-code corruption defense: `sanitize_source` (confusables→ASCII,
  CRLF→LF on every write) + a non-ASCII charset guard in `validate_code`.
- Hardened writes via `_safe_write` (UTF-8, `errors="replace"`, `newline="\n"`).
- Corrected the HPO gate: after the first tune it fires only when
  `floor < candidate < parent` (direction-aware).
- Temporary HPO result logging to `<worktree>/hpo_results/hpo_runs.jsonl`
  (remove after testing).

## Tests (self-running, no pytest)
`scripts/test_search_replace.py`, `scripts/test_hyperparameter_extract.py`,
`scripts/test_code_sanitize.py`, `scripts/test_hpo_gate.py`,
`server/test_seed_diversity.py`, `server/test_role_multifile_hpo.py`,
`server/test_inactive_pool_negative_gate.py`.
