# Test plan — `multifile-sr-roles-seed-hpo` → `staging`

Goal: fully exercise every part of this branch before merging to `staging`.
Scope: **7 commits, 22 files, ~2,574 insertions** on top of `staging`.

Branch carries 10 feature areas (see `docs/branch-summary-multifile-sr-roles-seed-hpo.md`).
Test bed: the live production swarm `https://multi-file-roles-production.up.railway.app`
(challenge = `vehicle_routing`, **CPU** — local Docker agents, no GPU needed).

Status legend:  ✅ have coverage · ⚠️ partial/gap · ⬜ to do · 🔬 needs live/manual

---

## Layer 0 — preflight (run first, every time)

| Check | Command | Pass |
|---|---|---|
| All self-running tests | `for t in scripts/test_*.py server/test_*.py; do python3 "$t"; done` | 14/14 green (baseline ✅) |
| Dashboard unit tests | `cd dashboard && npm install && npm test` | vitest green |
| Rust compiles (CPU) | `cargo check --features solver,vehicle_routing` (+ knapsack, satisfiability) | clean |
| Python imports | `python3 -c "import ast,glob;[ast.parse(open(f).read()) for f in glob.glob('scripts/*.py')+glob.glob('server/*.py')]"` | no syntax errors |
| Clean merge preview | `git merge --no-commit --no-ff origin/staging` (then abort) | no conflicts |

> Live testing must run from a **`multifile-sr-roles-seed-hpo` checkout** (worktrees inherit the
> current branch). Confirm `git branch --show-current` before launching a fleet.

---

## Layer 1 — per-feature matrix (the core)

For each feature: existing coverage, the gaps to add, and the pass criteria.

### F1 — Multi-file algorithms
*`{relpath:content}` map through agent state, worktree I/O, publish payload, 4 server columns.*
- ✅ `server/test_role_multifile_hpo.py` (multifile round-trips to experiments/trajectory_bests),
  `server/test_seed_inactive_multifile.py` (inactive pool map round-trip).
- ⬜ **Add** `scripts/test_challenge_files_multifile.py`: `write_files` prunes stale modules;
  `read_files` walks `.rs/.cu/.cuh`; single-file collapses to `{"mod.rs": code}`;
  `_state_files_map` fallback (legacy single-file state → map); `..`-escape rejected.
- 🔬 Live: the production best is already a **7-file** algo (`Fast Lane v4`: builder/config/
  evolution/gene_pool/instance/mod/operators.rs) → multi-file publish+serve already works.
- **Pass:** map stored + served intact; pruning removes orphaned modules; single-file unaffected.

### F2 — Soft search/replace (`scripts/search_replace.py`)
*SEARCH/REPLACE blocks, fuzzy match → 1–2 LLM repair rounds → skip unmatched (no full-rewrite fallback).*
- ✅ `scripts/test_search_replace.py`.
- ⚠️ **Extend**: exact + fuzzy (whitespace-only) match; multiple blocks in one response;
  block targeting a non-`mod.rs` file (multi-file); **unmatched block is skipped, not applied**
  (assert no corruption / partial write); interaction with `sanitize_source`.
- 🔬 Live: exploiter agent (A2) — confirm small localized diffs apply and it **never** full-rewrites.
- **Pass:** matched blocks apply byte-correct; unmatched skipped cleanly; exploiter SR-only.

### F3 — Roles ↔ tiers
*`init_fleet` role_for_tier (frontier→explorer, standard→exploiter); hypotheses tagged with role;
exploiters SR-only; role hot-reloads from config every ~5s.*
- ✅ role stored on hypothesis (`server/test_role_multifile_hpo.py`).
- ⬜ **Add** `scripts/test_tiers_roles.py`: `classify_tier` markers (haiku/mini/flash→standard,
  opus/pro→frontier); `role_for_tier`; `_normalize_role` (unknown→explorer).
- 🔬 Live: run explorer + exploiter (A1+A2) → role tags appear on dashboard hypotheses/feed;
  **hot-reload** = flip `role` in `fleet.config.json` mid-run, confirm the agent switches within ~5s.
- **Pass:** tier→role correct; hypotheses tagged; exploiter SR-only; hot-reload observed.

### F4 — Seed-pool diversity (`server/seed_diversity.py`)
*Code-similarity admission (simple LOC ceiling / novel / sticky); pool capped at K; eviction by
redundancy not score; random per-trajectory selection.*
- ✅ `server/test_seed_diversity.py` (incl. near-duplicate rejection).
- ⚠️ **Extend**: pool-cap eviction picks the **most-redundant** (never lowest-score); sticky seed
  survives; selection is uniform-random across the pool (statistical check over many draws).
- 🔬 Live: `/diversity` page + seed pool; frontier explorers seed it → diverse admits, dupes rejected.
- **Pass:** admission/eviction/selection behave per spec; score never drives eviction.

### F5 — Hyperparameter search (HPO)
*Server gate-state + storage; client `scripts/hpo.py` (per-track random search, candidate scoring,
firing gate `floor < candidate < parent` direction-aware, constant extraction; C5 = multi-file extraction).*
- ✅ `scripts/test_hpo_gate.py`, `scripts/test_hyperparameter_extract.py`,
  `server/test_role_multifile_hpo.py` (`has_tuned` signal).
- ⚠️ **Extend**: per-track random search picks within bounds; candidate scoring vs default;
  **multi-file constant extraction** (constants pulled from a non-entry module); gate direction on
  a `min` challenge; tuned per-track config published unconditionally (feasibility-gated only).
- 🔬 Live: an exploiter that improves → HPO fires; observe `has_tuned` + tuned configs on dashboard;
  inspect `<worktree>/hpo_results/hpo_runs.jsonl`.
- **Pass:** gate fires only when in-band; extraction multi-file-aware; tuned config scored + published.
- 🚩 **Merge-blocker:** the temporary `hpo_runs.jsonl` logging is marked "remove after testing"
  in the branch summary — confirm it's removed (or gated) before merge.

### F6 — Mainnet inactive seeding (C1–C4 + build_ptx + reshape)
*All challenges + multi-file/multi-`.cu`; idempotency guard; benchmark-on-first-adoption; mainnet→swarm
reshape converter; `build_ptx.py` aligned to mainnet.*
- ✅ `scripts/test_reshape_mainnet.py`, `server/test_seed_inactive_multifile.py`,
  `server/test_seed_benchmark_on_adoption.py`, `server/test_inactive_pool_negative_gate.py`.
- ✅ **C3-verified twice**: mainnet-tooling run of `sigma_freud_v8` (prebuilt ptx) and a full
  **swarm-side source build** (reshaped, build_ptx.py + cargo, L40) — 2/2 feasible, score 278,792.
- ⚠️ Reshape against **real** mainnet source: neuralnet `neural_extrem_v3` → correctly **skips with
  ERROR** (hook-API incompatible); knapsack/sat/vrp → passthrough; hypergraph/vsearch → multi-`.cu` OK.
- ⬜ **Live create-time**: `setup.py create … --seed-inactive-pool` on a throwaway swarm →
  seed lands in `inactive_algorithms` per challenge; neuralnet prints ERROR-skip; re-run create =
  no duplicate (idempotency); drive a swarm to stagnation → adopted seed is **benchmarked first**
  (a `seed_baseline` iteration sets the floor).
- **Pass:** per-challenge seeding correct; neuralnet skip loud; idempotent; benchmark-on-adoption fires.

### F7 — Server supporting changes
- ✅ negative-deposit gate (`test_inactive_pool_negative_gate.py`).
- ⬜ **hint counting** — leaderboard counts **consumed** hints, not offered (the offer-vs-consumed
  bug). Add/extend a server test: offer N, consume M<N, assert leaderboard shows M.
- ⬜ `inactive_minutes` default **20→60** — assert default config value.
- **Pass:** gate blocks negatives; leaderboard counts consumed only; default is 60.

### F8 — Dashboard
*Benchmark chart log/linear toggle + 2-D pan/zoom; responsive leaderboard / mobile; ideas tree; diversity matrix.*
- ⚠️ `cd dashboard && npm test` (vitest).
- 🔬 Manual visual on live pages: `/benchmark` (toggle + pan/zoom), `/leaderboard` (mobile width),
  `/ideas.html` (ideas/inspiration tree), `/diversity`, `/trajectories.html`.
- **Pass:** vitest green; interactions work; no console errors; mobile layout sane.

### F9 — Challenge: neuralnet tracks
*`n_hidden=4,7,10,14,18` matched to TIG.*
- ⬜ Config assertion (`server/challenges.py` track_keys).
- 🔬 GPU benchmark only if a neuralnet swarm is available (needs CUDA/C3).
- 🚩 Orthogonal known issues exist (test-set leak, frozen-layer regression) — out of scope but note.

### F10 — Live-test hardening
- ✅ `scripts/test_code_sanitize.py` (confusables→ASCII, charset guard).
- ⬜ **Add**: `_try_compile_fix` runtime→compile recovery path does **not** crash (arg-count regression);
  `_safe_write` CRLF→LF + `errors="replace"`; `validate_code` rejects stray non-ASCII.
- **Pass:** no loop-killing crashes on the recovery path; writes normalized; corruption caught.

---

## Layer 2 — server integration (in-process, route-level)
Drive `server.create_iteration` / `server.get_state` directly (the existing tests' pattern) to cover
cross-cutting flows without a network:
- multi-file publish → `best_algorithm_files` served back; role tag persisted; HPO `has_tuned`.
- adoption: stagnate → `adopted_inactive` with `needs_benchmark` for unscored seeds (✅ covered).
- negative/ infeasible never enters pool; consumed-hint accounting.

---

## Layer 3 — live end-to-end on the production swarm (real agents)
**Authorized:** user said to use the live swarm + start agents. Run **bounded** (`--max-iterations`),
clearly-labeled test agents, from a `multifile-sr-roles-seed-hpo` checkout. Available here:
Docker ✅, `claude` CLI ✅ (so `claude-code` / `claude-code-agentic` work via CLI login).

Agent matrix (each `--max-iterations 3–5`, observe dashboard between):

| # | role | provider | edit_mode | What it proves |
|---|---|---|---|---|
| A1 | explorer | `claude-code` | full | multi-file emergence, seed-pool seeding, role tag |
| A2 | exploiter | `claude-code` | search_replace (forced for exploiter) | SR-only localized edits, no full rewrite |
| A3 | explorer | `claude-code-agentic` | (tooled) | agentic worktree editing, multi-file write-back |
| A4 | exploiter | `claude-code` | search_replace | run longer to trip the HPO gate → tuned configs |

Observation surfaces (per iteration):
- `/api/state` → `best_algorithm_files` (multi-file), `best_score` climbing.
- `/trajectories.html` → trajectory score history, adoption/reset events.
- `/ideas.html` → inspiration/ideas tree (hint offer vs consume).
- `/diversity` → seed-pool admission + inspiration matrix.
- `/leaderboard` → per-agent runs/improvements, **role**, consumed-hint count.
- agent stdout + `<worktree>/hpo_results/hpo_runs.jsonl`.

Live checks mapped to features: A1→F1,F3,F4; A2→F2,F3; A3→F1,F2(agentic); A4→F5; all→F10 (no crashes).

---

## Layer 4 — seeding + GPU/C3 (mostly done)
- ✅ build_ptx alignment + multi-`.cu` swarm build E2E on L40 (`sigma_freud_v8`, 278,792).
- ⬜ create-time seeding on a throwaway swarm (F6 live bullet) — needs `setup.py create` + admin key.
- Optional: re-run `c3_tig_bench.py --challenge vector_search --download autovector_f` to confirm the
  generalized tool on a second GPU challenge.

---

## Layer 5 — dashboard (manual + vitest) — see F8.

## Layer 6 — regression / safety sweep
- Negative-deposit gate, infeasible-floor trap, compile-fix recovery crash, sanitize on corrupted output,
  idempotency guard, benchmark-on-adoption. Mostly ✅ in Layer 1; confirm none regressed post-merge-preview.

---

## Execution order & ownership
1. **Now, offline (me):** Layer 0 + write the ⬜ gap unit tests (F1, F3, F7, F10) + Layer 2. Fast, deterministic.
2. **Now, live (me):** Layer 3 A1+A2 bounded runs against production, observe dashboard. Then A3, A4.
3. **Needs host/user:** Layer 4 create-time seeding (admin key + throwaway swarm); Layer 5 manual visual;
   F9 neuralnet GPU; final `staging` merge-preview sign-off.

## Merge-blockers / risks to clear first
- 🚩 Remove temporary `hpo_runs.jsonl` logging (F5).
- 🚩 Confirm consumed-vs-offered hint counting fixed (F7).
- ⚠️ Multi-`.cu` *authoring* still uses a single `kernels.cu` LLM separator (known follow-up; not a
  blocker for the CPU live swarm, but is for GPU-challenge agent editing).
- ⚠️ neuralnet auto-seed impossible (incompatible API) — by design skips; ensure no swarm relies on it.
- Clean `staging` merge (no conflicts); all 14 self-running tests + new gap tests green.
