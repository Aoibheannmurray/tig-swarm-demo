# Hyperparameter Search — Design & Implementation Plan

## Motivation

The performance of an algorithm depends heavily on its hyperparameters, but
**changing hyperparameters is not a job for the LLM**. The LLM's job is to write
*algorithm structure* (a creative, discrete search); choosing numeric values is
a *tuning* job better done by a cheap, reproducible, unbiased search routine.

So an algorithm should be judged on its score when run with its **best set of
hyperparameters**, found by search — not on whatever constants the LLM happened
to inline. The logical flow:

```
agent writes algorithm → identify which constants are tunable hyperparameters
                       → run a hyperparameter search over them
                       → score the algorithm using the winning configuration
```

The bar is **"decent, not optimal."** We want a good-enough config cheaply, not
the global optimum. Every design choice below favours simplicity and bounded
cost over squeezing out the last few percent.

## Goal at a glance

- **Separate** algorithm mutation (LLM) from hyperparameter tuning (search).
- **Cheap by default**: only *promising candidates in mature trajectories* are
  tuned (a cascade). Untuned iterations cost exactly what they cost today.
- **Honest scoring**: tune on instances that are *not* the ones we score on.
- **Reproducible**: the winning config is stored with the algorithm and used to
  guide the next mutation's tuning.

## Per-iteration flow

```
mutate algorithm
  → benchmark on TEST seed, no hyperparameters        → default_score   (unchanged from today)
  → GATE:  trajectory has ≥ min_improvements improvements
           AND default_score ≥ the *default_score* of the (min_improvements)-th-previous
               improvement       (default-vs-default — never its tuned score; see "The band")
           AND feasible
     ├─ NO  → publish { code: mutated, score: default_score, hyperparameters: null }
     └─ YES → extraction LLM call
                  · suggests which constants become hyperparameters + ranges
                  · suggests `num_suggested_configs` concrete configs
                  · rewrites the algo to read each param from the Map (defaults = original consts)
                  · parent's full per-track winning map injected HERE (every track
                    combination; not in the mutation prompt)
              → compile the variant
              → search: suggested configs + random configs + the default config,
                        each benchmarked ONCE on the HPO seed (non-test instances),
                        recording its per-track score breakdown (track_scores)
              → winning config PER TRACK = the config scoring best on that track.
                        (The default config {} is in the set, so each track's winner is
                         never worse than that track's default on the HPO seed.)
                        There is no single global winner — only a winner per track.
              → benchmark variant on TEST seed, applying each track's winning config to
                        that track's instances → tuned_score (existing cross-track aggregate)
              → keep the per-track configs only if tuned_score strictly beats default_score
                        on the TEST seed; otherwise revert to default (publish at default)
              → publish { code: variant, score: tuned_score,
                          hyperparameters: { <track>: <winning config>, … } | null }
```

## The gate (cascade)

Tuning is expensive (extraction call + many benchmarks), so it is gated. A
candidate is tuned only if **all** of:

1. **Maturity** — the trajectory has had at least `min_improvements`
   improvements. Short / early trajectories are never tuned.
2. **Band** — the candidate's `default_score` is at least as good as the
   **`default_score` of** the `min_improvements`-th-previous improvement (i.e.
   it is within the recent competitive band; it may lag the frontier by up to
   `min_improvements` steps). The comparison is **default-vs-default**: we band
   the candidate's untuned score against earlier improvements' *untuned* scores,
   never their tuned scores — see "The band is default-vs-default" below.
3. **Feasible** — infeasible runs are never tuned (and never count as
   improvements).

### Why this shape

- The only knob is a **count** (`min_improvements`), which is unitless and
  transfers across challenges unchanged — unlike any threshold expressed in raw
  score units, which breaks because scores are baseline-relative, can be
  negative (e.g. neuralnet baseline ≈ −2.29M), and have different dynamic
  ranges per challenge.
- The band reads a **separate list of default scores** — one entry per
  improvement, recorded at the moment that improvement was published (its
  no-hyperparameters score). The check is
  `default_score ≥ default_scores[-min_improvements]`. This is independent of the
  *published* score (which is the tuned score when HPO ran), so an ancestor's
  tuning never moves the bar — see "The band is default-vs-default".
- The band **auto-scales** to each challenge: big improvement steps → wide
  tolerance; plateau → tight tolerance (spend tuning while there's momentum, get
  stingy when progress flattens).

### Known, accepted trade-offs

- **False negatives**: a candidate with a poor default but high tuning
  sensitivity (bad untuned, great tuned) is skipped. Acceptable at the
  "decent/cheap" bar.
- **Short/hard trajectories never tune**: a trajectory that never reaches
  `min_improvements` is scored entirely at default config — including hard,
  below-baseline challenges that plateau early. Conscious trade for simplicity.
  Future escape hatch: "≥ min_improvements **or** at publish" (one-line change).
### The band is default-vs-default

The maturity **counter** and the **band** read two different things, and keeping
them separate is what makes the gate honest:

- The **counter** (gate condition 1) counts *improvements* — every publish whose
  response says `is_new_best`. The published score that triggered it may be a
  tuned score; that's fine, the counter only cares that an improvement happened.
- The **band** (gate condition 2) compares *untuned* scores only. Each
  improvement records its `default_score` (the no-hyperparameters score it was
  benchmarked at before the gate ran) in a parallel list, and the band checks
  `default_score ≥ default_scores[-min_improvements]`. It never looks at a tuned
  score.

Why this matters: if the band compared against the *published* score, then any
ancestor that tuned would raise the bar for its descendants (a tuned score is
≥ its default by construction), so the gate would get stricter purely because an
earlier candidate happened to be tuned — an effect unrelated to the new
candidate's own quality. Default-vs-default removes that coupling: the band moves
only when raw, untuned algorithm quality moves. Storing the extra number is free
(we already compute `default_score` every iteration).

## Scoring & seeds

- **Default score** — run the mutated algorithm on the **TEST seed** with no
  hyperparameters. This is exactly today's benchmark; no extra LLM call.
- **Hyperparameter search** — run on a **different (non-test) seed** so tuning
  never sees the scored instances. No leakage.
- **Final score** — re-benchmark the winning config on the **TEST seed**. This
  is the score published for the algorithm.
- **Which score feeds what** — the **counter** advances on any `is_new_best`
  publish (default or tuned, doesn't matter). The **band** compares against each
  improvement's stored `default_score` only (never the tuned score). See "The
  band is default-vs-default".

## Hyperparameter search

- **Extraction LLM call** (separate from mutation): reads the *final, compiled*
  mutated source and emits structured JSON —
  1. the hyperparameter list `{ name, type, range, scale }`,
  2. `num_suggested_configs` concrete configs to try,
  3. a **behavior-preserving rewrite** of the algorithm that reads each
     hyperparameter from the `Map`, using the original in-code `const` as the
     default. An empty Map must reproduce the default-score behaviour exactly.
  - The **parent's full per-track winning map is injected into this prompt** —
    every `{track_key: config}` combination, not a union or representative pick —
    so the extractor sees how the winning values varied across tracks and can
    propose track-aware suggestions. Injected only here, never in the mutation
    prompt.
- **Search**: evaluate a total of `N` configs (host-tunable, default **13**),
  composed of `num_suggested_configs` LLM-suggested configs + the default config
  + random draws from the ranges for the remainder
  (`N - num_suggested_configs - 1`). Including the default config is cheap
  insurance that the tuned score can never be worse than the default score.
  Each config is benchmarked **once** across all tracks; the benchmark already
  returns a per-track breakdown (`track_scores`), so the search records that
  breakdown rather than only the collapsed cross-track score.
- **Pick a winner *per track***, not one global winner. For each scored track,
  select the config with the best score *on that track* (existing direction-aware
  / feasibility-gated comparison, applied to the track's score). Different tracks
  may end up with different configs — that's the point. Because the default
  config `{}` is in the set, each track's winner is never worse than that track's
  default *on the HPO seed*, so the per-track aggregate dominates any single
  global config for free (same benchmark budget). The result is a
  `{ track_key: config }` map.
- **Score the winner** by benchmarking the variant on the **test seed** with each
  track's winning config applied to that track's instances, then aggregating with
  the existing cross-track shifted geometric mean. This is the published
  `tuned_score`. The whole per-track map is kept or reverted as one unit (one
  binary decision on the test seed — see Invariants), so the published score is
  never worse than the default.

## Host-configurable parameters

These live in `fleet.config.json` (host-level). The two called out by the host
are `min_improvements` and `num_suggested_configs`; the rest are exposed for
completeness with sensible defaults.

| Parameter | Default | Meaning |
|---|---|---|
| `min_improvements` | **4** | Maturity threshold for the gate **and** the band window (the candidate must beat the `min_improvements`-th-previous best improvement). Applies after the first tune. |
| `first_tune_improvements` | **10** | Higher maturity bar for the FIRST tune of a trajectory (`hpo_first_tune_improvements`): no HPO fires until the trajectory has this many improvements. Subsequent tunes use `min_improvements`. |
| `num_suggested_configs` | **5** | How many concrete configs the extraction LLM proposes. The rest of the budget is random search. |
| `N` (`search_budget`) | **13** | Total number of hyperparameter searches (configs evaluated) per tune = `num_suggested_configs` suggested + 1 default + `N - num_suggested_configs - 1` random. |
| `tuning_instances_per_track` | 5 | Instances generated per scored track for the search. |
| `hpo_seed` | non-`"test"` | Seed used to generate the (non-test) tuning instances. |

## Invariants (these are where it silently breaks)

- **`default_score` == no-hyperparameters score** — the variant's defaults must
  equal the original consts, so the gate and search stay apples-to-apples.
- **`tuned_score ≥ default_score`** — the default config `{}` is in the search
  set for *every* track, so each track's winner is no worse than its default and
  the per-track aggregate is no worse than the all-default aggregate *on the HPO
  seed*. But search and final scoring run on *different* seeds (no leakage), so
  the guarantee doesn't transfer. The implementation therefore scores the full
  per-track winner on the test seed and **keeps it only if the aggregate tuned
  score strictly beats `default_score`** there — a single, global keep/revert
  decision on the whole `{track: config}` map. Doing the keep/revert globally
  (one binary decision) rather than per-track keeps the test-seed selection
  pressure to exactly one bit, matching today's single-winner design; per-track
  keep/revert would make `T` separate decisions on the scored seed and is
  deliberately avoided.
- **HPO never touches the test instances** — the tuning seed's generated
  instances are cached under a *separate, seed-scoped* path.
- **Feasibility gate preserved** — infeasible never counts as an improvement or
  a winner (already enforced server-side).

## Implementation components & edit sites

References are `file:line` from the current tree.

### 1. `scripts/benchmark.py` — hyperparameters + seed passthrough
- `benchmark.py:303` — append `--hyperparameters <json>` to the solver
  invocation when a config is provided (always omitted today). The Rust side
  already parses it (`src/main_solver.rs:63-67`) and threads it into
  `solve_challenge(..., hyperparameters)` (`main_solver.rs:46-50`).
- `benchmark.py:256` — the generator already accepts `--seed`; expose it as a
  CLI/env arg to `benchmark.py` instead of the pinned `"test"`.
- **Cache isolation**: put the seed in the generated-instance cache path
  (`datasets/<challenge>/generated/<seed>/<track>/`) so the HPO seed does not
  overwrite the test instances.
- **Per-track config for final scoring**: search uses one uniform
  `--hyperparameters` per run, but the final tuned-score benchmark applies a
  *different* config per track. Implemented by `_track_hyperparameters`: when
  `TIG_HYPERPARAMETERS` is a `{track_key: config}` map (values are dicts) the
  per-instance config is selected by track; a flat config still applies to every
  track. One benchmark run, existing cross-track geo-mean aggregation.

### 2. `scripts/hpo.py` (new) — the search
- Inputs: challenge, variant build, search space, suggested configs,
  `search_budget`, `hpo_seed`.
- Build config set: suggested + random draws + the default config.
- Benchmark each via `run_benchmark` on the HPO seed with `--hyperparameters`.
- Read each trial's **`track_scores`** (per-track means), not just the collapsed
  aggregate, and return a **winner per track** (`winning_configs = {track_key:
  config}`, via `_per_track_winners`) by direction-aware comparison on each
  track's score (the per-track mean already bakes in feasibility via the clamp).
  `winning_config` (the aggregate best) is retained for logging only.

### 3. `scripts/prompts.py` (+ backend) — extraction call
- New prompt builder beside `build_code_user_prompt` (`prompts.py:756`). Input:
  mutated source + the parent's **full per-track winning map** (every
  `{track_key: config}` combination). Output: param list + suggested configs +
  behavior-preserving variant (defaults = consts). Inject the parent map here only.
- Compile the variant (reuse `_benchmark_with_compile_fix` from `run_loop.py`).

### 4. `scripts/run_loop.py` — gate + orchestration
- Insert between the default benchmark (`run_loop.py:1327-1336`) and
  `publish_results` (`~1512`).
- **Improvement history**: read from the server, not tracked locally — `/api/state`
  returns `improvement_scores` from `db.get_recent_improvement_scores`, which is
  **default-vs-default** (`COALESCE(default_score, score)` over `beats_trajectory_best`
  rows). The agent sends `default_score` on every publish so the server can store
  it. The *count* (maturity) is `len(improvement_scores)`; the band floor is
  `improvement_scores[-min_improvements]`. See "The band is default-vs-default".
- **Gate**: `len(improvement_scores) ≥ min_improvements and feasible and
  default_score ≥ improvement_scores[-min_improvements]` (`_hpo_gate_open`).
- **If eligible**: component 3 → component 2 → final test-seed benchmark applying
  each track's winning config → publish variant + tuned score + the per-track
  config map.
- **Else**: publish the mutated algorithm at default, `hyperparameters: null`.
- (Robust alternative to local tracking: a `GET /api/trajectory/{id}/improvements`
  endpoint over `is_new_best` rows. Start local; add the endpoint only if
  trajectories get resumed across processes.)

### 5. `server/server.py` + state — persistence
- The publish payload carries `hyperparameters` (a `{track_key: config}` map or
  `null`) and `default_score` (`IterationCreate`, `models.py`).
- `default_score` is stored on the `experiments` row (new column) and read back by
  `get_recent_improvement_scores` for the band.
- `upsert_trajectory_best` stores the per-track config map beside
  `algorithm_code` / `score`; trajectory state exposes it as
  `best_hyperparameters` for the extraction prompt (parent → child).

## Build order

Each phase is independently testable; phases 1–2 give a working **manual** HPO
before any LLM or loop changes land.

1. **Component 1** — hyperparameters + seed passthrough + cache isolation.
   Verify by hand-running `benchmark.py` with a manual config and a non-test seed.
2. **Component 2** — standalone `hpo.py`, tested against a hand-written variant.
3. **Component 3** — extraction prompt + variant compile.
4. **Component 4** — gate + orchestration + local improvement tracking.
5. **Component 5** — publish / store / inject the winning config.

## Design refinements — ✅ IMPLEMENTED

Both refinements are now in the code:

- **Default-vs-default band** — the agent sends its `default_score` on every
  publish (`scripts/server.py` `publish_results`, `IterationCreate.default_score`);
  the server stores it on `experiments` (new `default_score` column, `_add_column`
  migration) and `get_recent_improvement_scores` returns
  `COALESCE(default_score, score)`, so the band an ancestor's tuning never raises.
  Untuned rows and legacy clients fall back to the published score. Resolves the
  former "mixed improvement history" wart.
- **Per-track winning configs** — `hpo.search` reads each trial's `track_scores`
  and returns `winning_configs = {track_key: config}` (a winner per track) via
  `_per_track_winners`; `run_loop._maybe_tune_hyperparameters` scores that map on
  the test seed (one global keep/revert) and publishes it; `benchmark.py`
  (`_track_hyperparameters`) selects each track's config per instance from the
  map. Same benchmark budget — a recording/selection change, not more runs.
  Storage/payload (`hyperparameters`) already carried an arbitrary JSON dict, so
  the per-track map needed no schema change. Covered by the `hpo.py` self-test
  (per-track divergence assertion).

## Implementation status

All five phases plus both refinements above are implemented:

- **Phase 1** — `benchmark.py` passes `--hyperparameters` to the solver and
  honours a `TIG_BENCH_SEED` override (seed-scoped instance cache); both hooks
  are forwarded across the Docker boundary and the C3 job env (`c3_compute.py`).
- **Phase 2** — `scripts/hpo.py` (random search: default + suggested + random,
  **per-track winner selection** from each trial's `track_scores`; unit-tested via
  a self-test that asserts the per-track winners diverge).
- **Phase 3** — extraction prompt + parser in `prompts.py`
  (`build_hyperparameter_{system,user}_prompt`, `parse_hyperparameter_response`);
  the parent's **per-track** winning map is injected to guide suggestions.
- **Phase 4** — gate + orchestration in `run_loop.py`
  (`_hpo_gate_open`, `_maybe_tune_hyperparameters`). The improvement band reads
  the server's `get_recent_improvement_scores`, which is **default-vs-default**
  (each improvement's `default_score`, sent on publish and stored server-side).
- **Phase 5** — `hyperparameters` (now a per-track `{track_key: config}` map) +
  `default_score` on the publish payload + `IterationCreate`. The config map is
  stored on `trajectory_bests` and exposed as `best_hyperparameters` in agent
  state; `default_score` is stored on `experiments` and feeds the band. Host
  knobs flow from `fleet.config.json` → `agent.config.json` → `config`.

## Known limitations

Three things gate *whether* a candidate can be tuned. They are independent of
each other and, importantly, **independent of the compute backend** — see the
backend note at the end.

### 1. Agentic LLM providers — ✅ FIXED (see Fix 1 below)

> **Status: implemented.** Agentic providers now tune via a second agent pass.
> The text below explains the original blocker; the fix is in "Fix 1".

**Why (original blocker).** The extraction step (Phase 3) needs a single, structured completion —
one prompt in, one response out (the JSON spec + the rewritten `mod.rs`), parsed
by `parse_hyperparameter_response`. The non-agentic providers go through
`llm_backends.call_llm`, which dispatches to a normal request/response API
(`anthropic`, `openai`, `google`, …, plus the `claude-code` CLI one-shot).

The agentic providers are a fundamentally different execution model: they run as
a **tool-driven headless agent inside a sandboxed git worktree**
(`agentic_backends.py`), editing files over many turns rather than returning a
single completion. `call_llm` has no case for them — it would raise
`ValueError: Unknown provider`. There is simply no "ask once, get a parseable
artifact back" path to hook the extraction into.

**Behaviour.** `_maybe_tune_hyperparameters` detects `args.provider in
_AGENTIC_PROVIDERS` and returns early — the candidate is published at its default
score, exactly as before. Fails safe, no error spam.

**To support it later:** route extraction through the agentic backend itself
(have the agent write the variant + a spec file in its worktree, then read those
back), instead of calling `call_llm`.

### 2. GPU challenges — ✅ FIXED (see Fix 2 below)

> **Status: implemented.** The GPU solver now accepts `--hyperparameters` and
> the gate no longer skips GPU challenges. The text below explains the original
> blocker; the fix is in "Fix 2". (Build validation requires the CUDA toolchain,
> so it must be confirmed in the GPU Docker image — not buildable on a host
> without `nvcc`.)

**Why (original blocker).** The CPU solver binary (`tig_solver`, `src/main_solver.rs`) already
parses `--hyperparameters <json>` and threads it into `solve_challenge`. The GPU
challenges use a *different* binary, invoked by `run_gpu_instance` as
`[binary, challenge, track_key, --seed, --index, --timeout, --ptx]` — it has
**no `--hyperparameters` flag**, and the GPU solver `main` never parses or
forwards a hyperparameters `Map`. So although the GPU `solve_challenge`
signatures *include* a `hyperparameters` argument, nothing on the GPU CLI can
supply a value for it.

**Behaviour.** `benchmark.py` prints a warning and ignores `TIG_HYPERPARAMETERS`
on the GPU path, and `_hpo_gate_open` returns `False` for `is_gpu` challenges, so
the gate never opens. (Note the **seed override still works for GPU** — the seed
flows into `run_gpu_instance` — it's only hyperparameter *values* that don't.)

**To support it later:** add a `--hyperparameters` arg to the GPU solver `main`
that parses the JSON into a `Map` and passes it to `solve_challenge`, mirroring
`src/main_solver.rs`; then drop the `is_gpu` guard in `_hpo_gate_open`.

### 3. Improvement history — ✅ server-side (survives restarts)

The band/count gate needs the last `min_improvements` improvement scores. These
are served by `db.get_recent_improvement_scores`, which queries the `experiments`
rows with `beats_trajectory_best = 1` for the trajectory (keyed by `trajectory_id`,
which is preserved across adoption and process restarts) and returns each one's
**`default_score`** (`COALESCE(default_score, score)`) — the default-vs-default
band. `run_loop` reads them from `/api/state` (`state["improvement_scores"]`); no
process-local accumulation, so a restart or an adopted trajectory inherits the
real history.

### Compute backend: local Docker **and** C3 both work

HPO is agnostic to *where* the benchmark runs. `run_benchmark` forwards `seed`
and `hyperparameters` to **both** paths:

- **local Docker** — `_run_benchmark_local` sets `TIG_BENCH_SEED` /
  `TIG_HYPERPARAMETERS` on the subprocess env, which `benchmark.py` forwards
  across the Docker boundary (`_reexec_in_docker`).
- **C3 cloud compute** — `run_benchmark_c3` → `_write_c3_project` exports the same
  two variables in the generated C3 job script, so the remote benchmark tunes /
  scores identically.

So the three limitations above are about the **LLM provider** (agentic) and the
**challenge type** (GPU), *not* the compute provider. A CPU challenge tunes the
same whether it runs on local Docker or on C3. The only interaction: C3 is
typically chosen for *GPU* challenges, and those are gated off by limitation #2 —
so in practice "C3 + CPU challenge" tunes, "C3 + GPU challenge" does not (because
it's GPU, not because it's C3).

## Fix plans for the limitations

### Fix 1 — HPO for agentic providers (a second agent pass) — ✅ IMPLEMENTED

Implemented as described below. Code: `build_hyperparameter_agentic_prompt`
(`prompts.py`); `reset_hyperparameter_spec` / `read_hyperparameter_spec` +
`HYPERPARAMS_RELPATH` (`agentic_sandbox.py`); `extraction=` widening in
`_build_sandbox_settings` / `prepare` + `_HYPERPARAMS_RELPATH`
(`agentic_backends.py`); `_extract_hyperparameters_agentic` /
`_extract_hyperparameters_api` dispatch in `_maybe_tune_hyperparameters`
(`run_loop.py`, with `backend`/`workdir` plumbed through). Codex runs under
`workspace-write` so it needs no widening; Claude Code gets the extra `Edit`
allow for the spec file.

The non-agentic extraction is a single `call_llm` returning a JSON spec + a
rewritten `mod.rs`. Agentic backends don't have that path — but they *do* have a
worktree they can edit over multiple turns. So the fix is exactly the idea of a
**second agentic call after the mutation + benchmark**: when the gate opens,
launch the backend again with an extraction task instead of returning early.

Concrete steps:

1. **Extraction task prompt (worktree-style, not system/user).** Tell the agent
   to: read the current `mod.rs`; pick 2–5 impactful constants; **edit `mod.rs`
   in place** so each is read from the `hyperparameters: &Option<Map<String,
   Value>>` argument with the current in-code value as the default
   (behaviour-preserving — an empty/`None` map must reproduce today's output);
   and **write the spec** to `.swarm/hyperparameters.json` as
   `{"hyperparameters": [...], "suggested_configs": [...]}` (same schema
   `hpo.py` / `_validate_hyperparameter_spec` already expect). Finish with a
   `cargo check`.
2. **Widen the sandbox for that pass.** `_build_sandbox_settings`
   (`agentic_backends.py:114`) currently denies `Write(**)` and only allows
   editing the algorithm + hypothesis files. Add an extraction mode (a flag on
   `prepare`) that also permits writing `.swarm/hyperparameters.json`, so the
   agent can drop the spec file.
3. **Branch in `_maybe_tune_hyperparameters`.** Replace the early-return for
   `args.provider in _AGENTIC_PROVIDERS` with a call to a new
   `_extract_hyperparameters_agentic(backend, workdir, …)` that:
   `backend.iterate(workdir, extraction_prompt, model=…, timeout_s=…)` →
   read the edited `mod.rs` back via `_read_worktree_files` (that's the variant)
   → parse `.swarm/hyperparameters.json` and run it through
   `_validate_hyperparameter_spec` → copy the variant into the main checkout
   with `files.write` (mirrors the normal agentic flow at `run_loop.py:1341`).
4. **Shared tail, unchanged.** From there the existing path runs: compile-check
   the variant, `hpo.search` on the HPO seed, re-score the winner on the test
   seed, keep-or-revert. No new search logic.
5. **Plumbing.** Pass `backend` and `workdir` into `_maybe_tune_hyperparameters`
   (they exist in `main()` for agentic mode but aren't currently forwarded).

Caveats: extraction becomes a second full agentic launch (extra cost + latency),
but it only fires when the gate opens, which is already rare. Make sure the
extraction-mode `prepare()` settings are applied for that launch and the normal
edit settings are restored afterwards.

### Fix 2 — HPO for GPU challenges (parse `--hyperparameters` in the GPU solver) — ✅ IMPLEMENTED

Implemented as described below. Code: `--hyperparameters` arg + parse + threaded
through `run_instance` (replacing the hard-coded `&None`) in
`src/main_gpu_benchmark.rs`; `hyperparameters` passthrough in `run_gpu_instance`
and removal of the GPU warning in `scripts/benchmark.py`; `is_gpu` guard dropped
from `_hpo_gate_open` (`run_loop.py`). Build validation must run in the GPU
Docker image (host has no `nvcc`).

The GPU `solve_challenge` signatures already take a `hyperparameters` argument
(e.g. `initial_algorithms/vector_search.rs:28`); only the GPU *binary* fails to
supply one. Wire it through, mirroring `src/main_solver.rs`.

Concrete steps:

1. **`src/main_gpu_benchmark.rs` (Rust).**
   - Add `arg!(--hyperparameters [HYPERPARAMETERS] "JSON string for solver
     hyperparameters").value_parser(value_parser!(String))` to the `Command`
     (alongside the args at lines 11–32).
   - In `main()` (line 468), parse it exactly as `src/main_solver.rs:63-67`:
     `.get_one::<String>("hyperparameters").map(|s| serde_json::from_str(s))
     .transpose().map_err(…)` → `Option<Map<String, Value>>`. Ensure
     `serde_json::{Map, Value}` are imported.
   - Thread the parsed value into the dispatch macro and replace the hard-coded
     `&None` at line 394 with `&hyperparameters`.
2. **`scripts/benchmark.py`.**
   - `run_gpu_instance` (≈ line 554): add a `hyperparameters: str | None = None`
     parameter and append `--hyperparameters <json>` to the binary invocation
     when set.
   - In `main()`'s GPU branch: pass `hyperparameters` into `run_gpu_instance`
     and delete the "not supported on the GPU solver path" warning.
3. **`scripts/run_loop.py`.** Drop the `is_gpu` guard in `_hpo_gate_open` so GPU
   challenges become eligible. (No instance-cache work needed: GPU instances are
   generated in-binary from `--seed`, so the non-test HPO seed already yields
   fresh, leakage-free tuning instances — the seed override already flows to
   `run_gpu_instance`.)
4. **Validation.** Build the GPU binary
   (`cargo build -r --bin tig_gpu_benchmark --features …`), smoke-test with a
   hand-written config, confirm a non-empty map changes behaviour and an empty
   map reproduces the defaults.

Note: GPU challenges still need the per-algorithm extraction step to expose
constants — that's the *same* extraction as CPU (and, for agentic GPU agents,
needs Fix 1 too). Fix 2 only removes the binary/gate blockers.

## Open questions / future

- **Publish-time tuning** for trajectories that never reach `min_improvements`
  (the "or at publish" escape hatch) — deferred for simplicity.
- **Clean default-vs-default band** — **implemented** (each improvement's default
  score is stored server-side and the band reads it). See "The band is
  default-vs-default" and "Design refinements — IMPLEMENTED".
- **Per-track winning configs** — **implemented** (a winner per track rather than
  one global config). See the "Hyperparameter search" section and "Design
  refinements — IMPLEMENTED".
- **Smarter search** (Bayesian/TPE, multi-fidelity pruning, seed-averaging for
  noise) — explicitly out of scope at the "decent" bar; revisit only if random
  search proves insufficient.

[deeper-seeker]   [BENCH] Build retry 2/2 — asking LLM to fix…
[deeper-seeker]   Fix changed the code (similarity to broken: 92.0%) — re-benchmarking.
[deeper-seeker]   [BENCH] Score: 169672  Feasible: True
[deeper-seeker]           Track n_items=1000,budget=10: 169672
[deeper-seeker]   Code changed during error recovery (post-fix similarity 80%) — re-describing hypothesis ...
[deeper-seeker]   Updated hypothesis: [metaheuristic] Tabu search with random perturbation and DP/cluster restarts
[deeper-seeker]   [HPO] gate open — tuning (N=13, suggested=5, seed='hpo')
[deeper-seeker]   [HPO] hyperparameters: ['alpha', 'tenure_multiplier', 'dp_fixed_point_iters', 'cluster_divisor']
[deeper-seeker]   [HPO] evaluating 13 configs on seed 'hpo' (N=13, suggested=5)
[deeper-seeker]   [HPO] 1/13 score=167464.36 feasible=True default
[deeper-seeker]   [HPO] 2/13 score=167441.4 feasible=True {"alpha": 0.0, "tenure_multiplier": 1.0, "dp_fixed_point_iters": 3, "cluster_divisor": 20.0}
[deeper-seeker]   [HPO] 3/13 score=178477.62 feasible=True {"alpha": 0.1, "tenure_multiplier": 1.5, "dp_fixed_point_iters": 5, "cluster_divisor": 15.0}
[deeper-seeker]   [HPO] 4/13 score=164208.34 feasible=True {"alpha": 0.05, "tenure_multiplier": 0.8, "dp_fixed_point_iters": 2, "cluster_divisor": 30.0}
[deeper-seeker]   [HPO] 5/13 score=172928.94 feasible=True {"alpha": 0.2, "tenure_multiplier": 2.0, "dp_fixed_point_iters": 4, "cluster_divisor": 10.0}
[deeper-seeker]   [HPO] 6/13 score=173950.98 feasible=True {"alpha": 0.15, "tenure_multiplier": 1.2, "dp_fixed_point_iters": 3, "cluster_divisor": 25.0}
[deeper-seeker]   [HPO] 7/13 score=175306.54 feasible=True {"alpha": 0.14945299569960604, "tenure_multiplier": 1.2582186470712815, "dp_fixed_point_iters": 5, "cluster_divisor": 9.417843125668465}