# Explorer vs Exploiter speed gap — in-depth analysis plan

**Goal:** Quantify *why* explorer iterations are slower than exploiter iterations
at both **code mutation** (LLM time) and **benchmarking** (compile + run time),
then identify levers that speed up the explorer **without removing its
exploration behaviour** (novelty, bootstrapping, inspiration-driven pivots).

This document is an *analysis* plan, not an implementation plan. No solver or
orchestration code changes until the measurements below confirm where the time
actually goes.

---

## STATUS (implementation tracker)

| Phase | What | Status | Artifact / branch |
|---|---|---|---|
| 1 | Instrument the loop | **DONE** — built, ran live | `phase1-iter-instrumentation` (`iter_metrics.py`, telemetry in `run_loop.py`/`benchmark.py`) |
| 2 | Decompose the gap | **DONE (tooling)** — needs ≥200-iter data for weight | `scripts/analyze_iter_timing.py`; validated on the 2 live lines (F5) |
| 3·F1 | Gate `algorithm` behind `solver` | **DONE (shippable)** | `f1-gate-algorithm-solver-feature` — 5 CPU challenges, −64% build |
| 3·E2 | sccache measurement | **DONE (measured)** | see F6 below |
| 3·E3 | Two-stage feasibility-probe timeout | **DONE (prototype, opt-in)** | `e3-feasibility-probe-timeout` (`benchmark.py`, `test_probe_timeout.py`) |
| 3·E1 | Decouple edit-mode from role | **knobs exist; needs raw-API A/B run** | `edit_mode` opt-in already in `_use_search_replace`; analyze with Phase 2 + gate with Phase 4 |
| 3·E4 | Draft model for routine turns | **config-only; needs A/B run** | per-agent `model` in fleet.config.json |
| 4 | Exploration guardrail | **DONE (tooling)** | `scripts/analyze_exploration.py` (novelty / leapfrog / acceptance `gate`) |
| 5 | Synthesis | **DONE** | `explorer-speedup-final-report.md` |

"DONE (tooling)" = the analysis/prototype code is implemented and self-tested; the
remaining work is *running it on a sustained live dataset*, which the live VRP@630s
cycle (17–39 min/iteration) makes a multi-hour job, not an in-session one. E1/E4 are
experiments whose knobs already exist — they need execution + budget, then the Phase 2
analyzer and Phase 4 gate score the result.

---

## 0. Background — what the role actually changes (already established)

The role flag changes **no benchmark parameter**. Instance counts, per-instance
timeouts, and worker counts are fixed in `scripts/benchmark.py` and read from
`.swarm-cache.json`; they are role-agnostic. The speed gap is therefore a
second-order effect of how code is *produced*. Confirmed role differences:

| Lever | Explorer | Exploiter | File |
|---|---|---|---|
| Edit mechanism | full-file rewrite (single-file default) | search/replace always | `run_loop.py:320-341` (`_use_search_replace`, exploiter branch line 337) |
| Cold start | bootstraps from stub | seeded working code; skips if only stub | `server.py:195-244` (`seed_agent_strategy`, line 217); `run_loop.py:1825-1833` |
| Prompt guidance | "novel, structurally-different, ambitious rewrites" | "ONE localized change, ≤15% lines" | `prompts.py:68-84` (`_role_guidance`) |
| Inspiration / peer code | yes (on stagnation) | no | `prompts.py` inspiration formatting (~135-203, 1504-1528) |
| Strategy-tag nudge | yes | no | `prompts.py:87-95` (`_niche_nudge`) |
| Default model tier | frontier (opus/sonnet-4/gpt-5) | standard (haiku/flash/mini) | `server/tiers.py` (`role_for_tier`, `classify_tier`) |

**Leading hypotheses for the gap (to be confirmed/ranked by this plan):**

- **H1 (mutation):** Output-token volume dominates LLM latency; full-file rewrites
  emit far more output tokens than search/replace blocks → explorer code-gen calls
  are 5–20× slower per call.
- **H2 (mutation):** Explorers run a slower/larger model by default (tier→role
  mapping), compounding H1.
- **H3 (benchmark):** Explorer solutions are more often slow/infeasible and hit the
  **full per-instance timeout wall**, where exploiter refinements finish early.
- **H4 (benchmark+mutation):** Explorers trigger the compile-fix / runtime-fix loops
  more often; each retry = another full `cargo build -r` of 3 binaries (no sccache)
  **plus** another LLM round-trip.
- **H5 (mutation):** Larger explorer input context (inspiration blocks, prior
  hypotheses) raises prefill time and provokes larger rewrites.
- **H6 (benchmark):** Ambitious structural rewrites cause larger incremental
  recompiles than localized edits.

The plan's job is to attribute the wall-clock gap across H1–H6 with data.

---

## Phase 1 — Instrument the loop (no behaviour change)

Add structured per-phase timing + size metrics to one JSONL line per iteration.
Emit, do **not** alter control flow.

**Where to instrument (`scripts/run_loop.py`):**
- Wrap each LLM call (`_generate_code`, `_generate_code_search_replace`, hypothesis,
  compile-fix `run_loop.py:2046`, runtime-fix `run_loop.py:2065`, re-describe, HPO,
  tacit) with start/end timestamps.
- Capture per call: `phase`, `wall_ms`, `input_tokens`, `output_tokens`, `model`,
  `edit_mode` (full vs search_replace), `n_repair_rounds`.
- Wrap the benchmark call (`run_loop.py:2038-2051`) and inside `benchmark.py` capture
  per-instance: `cargo_build_ms` (split solver/evaluator/generator), `solver_ms`,
  `evaluator_ms`, `timed_out` (already flagged ~`benchmark.py:1190`), `feasible`.
- Record iteration-level: `role`, `tier`, `model`, `challenge`, `bootstrap`,
  `is_new_best`, `compile_fix_attempts`, `runtime_fix_attempts`,
  `total_cargo_builds_this_iter`.

**Token source:** API backends report usage; agentic (claude-code/codex) does **not**
(`run_loop.py:2136-2139` treats cost as $0). Flag agentic iterations as
`tokens_unknown=true` and analyse them separately (use wall-clock only).

**Output:** append to `reports/iter_timing.jsonl`. One line per iteration; one nested
array per LLM call and per benchmark instance.

**Exit criteria:** ≥200 iterations logged per role per challenge, across at least
one CPU challenge and one GPU challenge (GPU isolates compile-vs-run differently —
sequential, `benchmark.py:1414`).

---

## Phase 2 — Decompose the wall-clock gap

From `reports/iter_timing.jsonl`, build a per-role breakdown of mean/median
iteration wall-clock into:

```
iteration_wall =
    hypothesis_llm
  + code_gen_llm              (full-file vs search_replace)
  + compile_fix_llm + builds
  + runtime_fix_llm + builds
  + benchmark_build
  + benchmark_run            (split: finished-early vs timed-out instances)
  + HPO + tacit + redescribe
```

**Key cuts to produce:**
1. **Mutation gap attribution:** explorer vs exploiter `code_gen_llm` time, regressed
   against `output_tokens` and `model`. Tests **H1** (tokens) vs **H2** (model). Run a
   partial: hold model constant (force a frontier model on a handful of exploiters and
   a standard model on a handful of explorers via `fleet.config.json`) to separate the
   two.
2. **Benchmark gap attribution:** explorer vs exploiter `benchmark_run` split by
   `timed_out`. Tests **H3** — compute fraction of explorer instances that hit the
   timeout wall and the wall-clock that represents.
3. **Retry tax:** mean `compile_fix_attempts` + `runtime_fix_attempts` and the cargo
   rebuilds they cause, per role. Tests **H4**.
4. **Context size:** explorer vs exploiter `input_tokens` and correlation with
   `code_gen` output size. Tests **H5/H6**.

**Deliverable:** a stacked-bar chart (per role) of where the seconds go, plus a ranked
table of H1–H6 by share of the gap.

---

## Phase 3 — Targeted micro-experiments (offline, no swarm needed)

Run these on a fixed seed solver to isolate variables, using `c3_tig_bench.py` /
`benchmark.py` directly.

- **E1 — edit-mode A/B at constant role/model.** Take one explorer config; run N
  iterations in `edit_mode: full` vs `edit_mode: search_replace` (the opt-in already
  exists, `run_loop.py:341`). Measure code-gen wall + output tokens + resulting score
  delta. **Question:** how much explore-quality (score improvement, novelty) is lost if
  explorers refine via search/replace? This is the central trade-off.
- **E2 — sccache/ccache on the cargo path.** Wrap `build()` (`benchmark.py:206-225`)
  with `RUSTC_WRAPPER=sccache`; measure `cargo_build_ms` cold vs warm, per challenge.
  Quantifies the H4/H6 ceiling. Pure speedup, zero behaviour change — likely the
  cheapest win.
- **E3 — feasibility-probe timeout.** Prototype a two-stage benchmark: short timeout
  for a first feasibility probe, full budget only once feasible. Measure
  `benchmark_run` reduction on a deliberately-slow solver. Tests the H3 lever without
  changing scoring (final score still uses full budget).
- **E4 — draft-model for routine turns.** Run an explorer with a cheap model for
  code-gen but frontier for hypothesis/inspiration turns. Measure wall-clock vs score
  trajectory. Tests whether H2 can be reclaimed without losing idea quality.

Each experiment reports: Δ wall-clock, Δ output tokens, Δ score-improvement-rate,
Δ novelty (see Phase 4 metric).

---

## Phase 4 — Guardrail: prove exploration is preserved

Any speedup is only acceptable if explorer **novelty** and **leapfrog rate** hold.
Define and track:

- **Novelty:** mean pairwise code distance (AST or normalized-diff) between an
  explorer's accepted edits and (a) its own lineage and (b) the swarm best. Falling
  novelty = the speedup turned the explorer into an exploiter.
- **Leapfrog rate:** fraction of explorer iterations that produce a *structurally
  different* new swarm best (vs incremental). Source: `hypotheses.role` already stored
  (`server/db.py:45`) + strategy-tag distribution.
- **Bootstrap success:** cold-start → first-feasible iteration count, unchanged.

Acceptance rule for any Phase-3 lever: ship only if Δ wall-clock < 0 **and** novelty
and leapfrog rate are within noise of baseline.

---

## Phase 5 — Synthesis & recommendation

Produce a short report:
1. The attributed gap (Phase 2 chart) — the definitive "why".
2. Ranked levers by (speedup × safety), where safety = Phase-4 guardrail impact.
3. A go/no-go per lever. Expected ordering a priori (to be confirmed):
   - **sccache (E2):** ship — pure win, no behaviour change.
   - **feasibility-probe timeout (E3):** likely ship — caps timeout-wall tax.
   - **decoupled edit-mode (E1):** ship *iff* novelty holds — the highest-leverage but
     highest-risk change to "explore functionality".
   - **draft-model (E4):** config recommendation, contributor-owned.

---

---

## FINDINGS LOG (live — updated as the plan runs)

### F1 — Compile tax is the benchmark gap, and ~65% of it is wasted work (measured)

Environment: cargo 1.89, 8 cores, warm `target/` (1.1G). Challenge: satisfiability.

| Build scenario | Wall time |
|---|---|
| Warm no-op (nothing changed) | **0.47s** |
| Edit-triggered rebuild, 3 binaries | **7.7–8.3s** |
| — tig_solver | ~2.5s |
| — tig_evaluator | ~2.4s |
| — tig_generator | ~2.7s |

**Root cause:** `pub mod algorithm;` (`src/<challenge>/mod.rs`, e.g. satisfiability:109)
is **not feature-gated**, so the algorithm module compiles under all three feature
sets (`solver` / `evaluator` / `generator`). An agent only ever edits
`src/<challenge>/algorithm/mod.rs`, but that edit invalidates the crate for all three
binaries — even though the evaluator and generator produce byte-identical output
regardless of the solver algorithm.

**Proven fix (measured, then reverted):** gate the module —
```rust
#[cfg(feature = "solver")]
pub mod algorithm;
```
After gating, an algorithm edit rebuilds only the solver; evaluator/generator drop to
no-ops:

| Build (gated) | Wall time |
|---|---|
| tig_solver | 2.53s |
| tig_evaluator | **0.13s** |
| tig_generator | **0.14s** |
| **total** | **2.8s (vs 7.7s) — −64%** |

This is a **pure-win, zero-behaviour-change** optimization that helps *every* agent and
*both* roles ~5s/iteration. Explorers benefit more because each compile-fix retry
(`run_loop.py:2046`) pays the full build cost again.

**Caveat — not a blanket change.** Most challenges reference `algorithm` only from the
solver entry (`main_solver.rs:46`, `required-features = ["solver"]`), so gating is safe.
But `neuralnet_optimizer/mod.rs:396-398` (and the hypergraph/vector_search re-exports at
mod top-level) reference `algorithm::` items *outside* a solver gate — those need the
items hoisted/guarded before gating. Per-challenge audit required; satisfiability,
knapsack, energy_arbitrage, job_scheduling, vehicle_routing look straightforward.

**sccache (E2) re-ranked:** for a *leaf-crate* edit (what agents do) sccache does **not**
help — the changed solver crate can't be cache-hit and sccache disables incremental.
It only helps cold builds / dependency-graph churn. The gating fix supersedes it for this
workload. Keep sccache as a secondary lever for cold worktree spin-up.

### F2 — Mutation gap (H1) is structural and *widens* as algorithms mature (measured)

Output-token volume dominates LLM wall-clock. An explorer full-file rewrite regenerates
the **entire** algorithm file; an exploiter search/replace emits one block (~150–500 tok).

| Challenge | algorithm/mod.rs lines | ~output tokens (full rewrite) | exploiter block | ratio |
|---|---|---|---|---|
| knapsack | 64 | ~660 | ~150–500 | ~1.5–4× |
| energy_arbitrage | 208 | ~1.9k | ~150–500 | ~4–12× |
| neuralnet_optimizer | 341 | ~3.1k | ~150–500 | ~6–20× |
| job_scheduling | 399 | ~3.8k | ~150–500 | ~8–25× |
| satisfiability | 1054 | ~11.3k | ~150–500 | **~22–75×** |

At a frontier model's ~50–80 tok/s, satisfiability's ~11.3k output tokens is **~140–225s
of pure generation per explorer code-gen call**, vs single-digit seconds for an exploiter
block. **The gap grows with algorithm maturity** — the better/longer the current best, the
more an explorer must regenerate, while the exploiter's per-edit cost stays flat. This is
a compounding structural penalty, independent of and stacked on top of the model-tier
penalty (H2: explorers default to slower frontier models via `tiers.role_for_tier`).

**Implication for the speedup levers:** decoupling edit-mode from role (E1) — letting
explorers refine via search/replace on non-pivot turns and reserve full rewrites for
genuine structural pivots — directly attacks the largest, fastest-growing cost. This is
the highest-leverage change, gated on the Phase-4 novelty guardrail.

### F3 — The gating fix (F1) maps *exactly* onto the 5 CPU challenges, all safe (audited)

Per-challenge audit of `algorithm` references in each `src/<challenge>/mod.rs`, cross-checked
against the GPU dispatch (`main_gpu_benchmark.rs:397` calls `challenges::<name>::solve_challenge`
at mod-level) and the solver dispatch (`main_solver.rs:46` calls `…::algorithm::solve_challenge`,
`required-features=["solver"]`):

| Challenge | GPU? | mod.rs refs `algorithm` outside solver? | Gating verdict |
|---|---|---|---|
| satisfiability | CPU | no (`pub mod algorithm;` only) | **SAFE — drop-in** |
| vehicle_routing | CPU | no | **SAFE — drop-in** |
| knapsack | CPU | no | **SAFE — drop-in** |
| job_scheduling | CPU | no | **SAFE — drop-in** |
| energy_arbitrage | CPU | no | **SAFE — drop-in** |
| hypergraph | GPU | yes — `pub use algorithm::solve_challenge` (mod top) | excluded (see below) |
| vector_search | GPU | yes — `pub use algorithm::solve_challenge` (mod top) | excluded |
| neuralnet_optimizer | GPU | yes — mod-level `solve_challenge` wrapper calls `algorithm::optimizer_*` | excluded |

This is a clean result: **the 3-binary CPU build waste exists only on the 5 CPU challenges, and
all 5 are exactly the ones safe to gate** with `#[cfg(feature = "solver")]`. The 3 GPU challenges
build a *single* `tig_gpu_benchmark` binary (no solver/evaluator/generator triple), so the waste
doesn't apply to them — and they're the only ones with mod-level `algorithm` references (needed by
the GPU dispatch), which would force an `any(feature="solver", feature="gpu_benchmark")` gate. Leave
them alone. Net: a per-challenge change on 5 files, each independently verifiable with the
build-timing harness above.

### F4 — Timeout-wall (H3) confirmed: external SIGKILL, not cooperative budgeting (measured + code)

`main_solver.rs:run_solve` calls `algorithm::solve_challenge(&instance, &save_solution_fn,
hyperparameters)` — **no time budget is passed to the solver.** The timeout is enforced *externally*
by `benchmark.py` via `subprocess.run(..., timeout=…)`, i.e. by **SIGKILL at the wall**. A solver
that is algorithmically slow (or whose inner `loop {}` doesn't converge — common in novel explorer
rewrites) runs to the *full* per-instance budget before being killed, producing no/partial result.

Per-challenge budgets (`server/challenges.py`, confirmed against live worktree `.swarm-cache.json`):

| Challenge (CPU) | per-instance timeout | instances (typ) | worst-case bench wall (4 workers, all hit wall) |
|---|---|---|---|
| energy_arbitrage | 30s (default) | 10 | ~90s |
| knapsack | 60s | 10–50 | ~3–13 min |
| job_scheduling | 260s | 10 | ~13 min |
| vehicle_routing | 260–630s (host-set) | 10 | ~13–32 min |
| satisfiability | 300s | 10 | ~15 min |

A *fast, feasible* solver (what an exploiter refines) finishes each instance in seconds → whole
benchmark in tens of seconds. A *slow/novel* solver (what an explorer produces) can sit at the wall
→ a **10–50× benchmark-time multiplier**, and it bites explorers far more often. This is the
benchmark-side half of the explorer penalty; the compile retries (F1) are the other half.

**Note / candidate lever (design):** because the budget is enforced by SIGKILL rather than passed
in, the solver can't self-limit. Passing the deadline into `solve_challenge` (cooperative anytime
return) would let a slow explorer solver *return its best-so-far* at the budget instead of being
killed with nothing — turning wasted wall-clock into a scored result. Larger change; flag for design
review, not part of the safe set.

### F5 — Live instrumented run (Phase 1 enabled on the real swarm)

Instrumentation built on branch `phase1-iter-instrumentation`, cherry-picked into the two
live fleet worktrees, run with `SWARM_ITER_METRICS=1`, 1 iteration each, on the live
challenge **vehicle_routing @ 630s/instance**. Both agents are `claude-code` / opus-4-7
(claude-code reports **no tokens**, so token fields are 0 — wall-clock is the signal).

**Exploiter (swiper-no-exploiting) — completed line:**

| field | value |
|---|---|
| `iteration_wall_s` | **1028.8s (~17 min)** |
| edit path | search/replace (localized "add 2-opt* swap") |
| code-gen LLM call | **240.4s** |
| hypothesis / hyperparam-extract LLM | 7.2s / 23.4s |
| **LLM total** | **271s** |
| main benchmark | build 1.9s, solver **0.89s sum**, **0 timeouts, 10/10 feasible** |
| **non-LLM remainder** | **~758s (~12.6 min) = the HPO sweep (13 configs)** |

Key live finding: the exploiter's *mutation + main benchmark were cheap* (271s LLM + ~2s
benchmark). The iteration was dominated by the **HPO sweep — 13 benchmark configs, several
of which walled at 630s**. So for a *productive* exploiter (one that improves and trips the
HPO gate), the dominant cost is HPO benchmarking, not the edit. New lever surface: HPO trial
budget / early-abort on walling trials.

**Both completed — measured side-by-side (1 iteration each, same challenge/model/backend):**

| | EXPLORER (dora) | EXPLOITER (swiper) | ratio |
|---|---|---|---|
| `iteration_wall_s` | **2313.8 (~39 min)** | **1028.8 (~17 min)** | **2.25×** |
| edit path | full-rewrite | search/replace | |
| LLM total | 443.4s | 271.1s | 1.6× |
| — hypothesis | 14.5s | 7.2s | |
| — code-gen | 265.5s | 240.4s | **~1.1× (≈ equal!)** |
| — compile_fix | **155.6s** | — | explorer-only (H4) |
| — redescribe / hpo-extract | 7.7s | 23.4s | |
| cold build (solver/eval/gen) | **6.0 / 4.8 / 4.8s** | (warm last build) | |
| solver elapsed — sum / max | **6150s / 615s** | **0.89s / 0.11s** | **~5600× slower** |
| bench timeouts / feasible | 0 / 10 | 0 / 10 | |
| benchmark wall (≈) | **~1845s (80% of iter)** | ~2s main + ~758s HPO | |

**What the live numbers reveal (sharper than the offline model):**

1. **The explorer's own slow code is the dominant cost, not code generation.** Its rewrite was
   feasible but ran **615s/instance vs the exploiter's 0.11s — ~5600× slower** — so its
   benchmark consumed **~1845s (80% of the 39-min iteration)**, running right up to the 630s
   wall without technically timing out. This is F4/H3 made concrete: benchmark cost scales with
   solution slowness, and explorer solutions are slow.

2. **Compile-fix tax is real and large (156s).** The rewrite failed to compile → a 156s
   compile-fix LLM call (+ cold rebuild) the exploiter never paid (H4 confirmed).

3. **F1 cold-build waste is visible live.** dora's post-edit build: solver 6.0s, **evaluator
   4.8s, generator 4.8s** — the evaluator/generator rebuilt ~as much as the solver despite only
   the algorithm changing. That's **~9.7s of wasted build** per cold iteration that the F1
   gating fix removes.

4. **Backend nuance — H1 is masked under the claude-code/agentic CLI backend.** Code-gen wall
   was *nearly equal* (265s vs 240s), NOT the 20–75× the output-token model predicts. Because
   `claude-code` runs an agentic CLI whose wall is dominated by its own tool-loop, not raw
   output-token streaming. **H1's output-token gap will only show on a raw API backend
   (anthropic/openai).** Under agentic backends, the explorer penalty is instead dominated by
   compile-fix retries (H4) + slow-code benchmark walling (F4).

**Revised lever priority for this (claude-code/agentic) swarm:** F4 (cooperative deadline /
cap slow-code benchmark — would have saved most of the explorer's ~1845s) and H4 (compile-fix
avoidance) lead; F1 (gating) is a clean ~9.7s/iter win on top; the HPO sweep (~758s) is the
exploiter's main cost and its own new lever. The edit-mode lever (E1) matters most for raw-API
swarms, where it's not masked.

**Instrumentation note — bug caught by the live run & fixed (commit 93a4a55):** the first
exploiter line reported `edit_mode: "full"` though the log clearly took the search/replace
path. Cause: the end-of-iteration record re-derived edit_mode by calling
`_use_search_replace(role, files, config)` with the `ChallengeFiles` object instead of a dict
file_map, tripping the `isinstance(dict)` guard → always "full". Fixed by capturing the
decision at the dispatch site via `iter_metrics.note("edit_mode", …)`. (The explorer's
in-flight line predates the fix but is coincidentally correct — it genuinely was a full
rewrite. The exploiter line above should read search/replace.)

**Caveats for analysis:** (1) `bench_timing.build_ms` reflects the *last* build() in the
iteration, which is warm — the cold post-edit build (where the F1 evaluator/generator waste
shows) is an earlier build; capturing the first build per iteration is a worthwhile
refinement. (2) claude-code provider = no token data; an API provider (anthropic/openai) is
needed to populate the H1 output-token columns.

### F6 — sccache (E2): big on cold/worktree spin-up, irrelevant to leaf edits (measured)

sccache 0.16.0 (aarch64), challenge satisfiability, `tig_solver`, shared `SCCACHE_DIR`:

| Build scenario | Wall | Cache |
|---|---|---|
| cold, no sccache | **24.5s** | — |
| cold, sccache populating (all-miss) | **29.8s** | 0/70 Rust hits (+5s overhead: misses + sccache disables incremental) |
| **cold, sccache warm cache** | **5.9s** | **69/69 Rust crates hit — −76% vs cold no-sccache** |
| leaf edit (touch algorithm), sccache | ~1.2s | changed crate is a miss — sccache adds nothing here |

**Conclusion (confirms the pre-measurement call):** sccache does **not** help the hot path
(an algorithm edit forces the `tig-challenges` crate to recompile — a cache miss by
definition — and sccache *disables* cargo incremental, so it can even hurt). Its win is the
**fresh-worktree first build: 24.5s → 5.9s (−76%)** when a *shared* cache is pre-populated.
For a fleet that spins up N worktrees, pointing every worktree's `SCCACHE_DIR` at one shared
volume makes each cold start ~4× faster. **sccache and F1 are complementary, not competing:**
F1 removes the per-edit evaluator/generator recompile; sccache accelerates the one-time cold
build per worktree. Net ranking unchanged — F1 owns the per-iteration hot path; sccache is a
worthwhile fleet-spinup optimization, not a per-iteration one.

---

## Phase 1 instrumentation — SPEC (proposed, NOT applied; analysis-only)

Held for review. When approved, this is the additive logging (no control-flow change) to capture
live data for H2/H3/H5 that can't be measured offline:

- `run_loop.py`: wrap each LLM call (`_generate_code`, `_generate_code_search_replace`, hypothesis,
  compile-fix `:2046`, runtime-fix `:2065`, re-describe, HPO, tacit) → record
  `{phase, wall_ms, input_tokens, output_tokens, model, edit_mode, repair_rounds}`. API backends
  report usage; agentic backends do **not** (`:2136-2139`) → tag `tokens_unknown=true`, wall-clock only.
- `benchmark.py`: in `build()` (`:206-225`) time each of the 3 cargo builds separately
  (`cargo_build_ms` per binary — this is where F1's win shows up live); in `run_instance` record
  `{solver_ms, evaluator_ms, timed_out, feasible}` per instance (F4's timeout-wall rate).
- Iteration-level: `{role, tier, model, challenge, bootstrap, is_new_best, compile_fix_attempts,
  runtime_fix_attempts}` → one JSONL line to `reports/iter_timing.jsonl`.

Exit criteria: ≥200 iterations/role across ≥1 CPU + 1 GPU challenge, stratified by `(challenge, model)`.

---

## Updated lever ranking (post-measurement)

| Lever | Speedup | Risk to exploration | Status |
|---|---|---|---|
| **F1: gate `algorithm` behind `solver`** (5 CPU challenges) | −64% compile/iter (~5s) on every CPU iteration; more for explorers (retries) | **none** — build output identical | Ready to ship pending review; audited safe |
| **E3: cooperative deadline / two-stage timeout** (F4) | caps the 10–50× timeout-wall tax on slow explorer code | none (final score unchanged) | Design change — needs review |
| **E1: decouple edit-mode from role** (H1) | attacks the 22–75× output-token gap; the largest & fastest-growing cost | **medium** — must prove novelty holds (Phase 4) | Needs live A/B + novelty guardrail |
| **E4: draft model for routine explorer turns** (H2) | reclaims the frontier-model tax on non-pivot turns | low–medium | Config recommendation; needs A/B |
| **E2: sccache** (measured, F6) | **−76% on cold worktree spin-up (24.5s→5.9s)**; nothing on leaf-edit rebuilds | none | Complementary to F1: F1 owns per-iter, sccache owns fleet spin-up (shared `SCCACHE_DIR`) |

---

## Risks / notes

- Agentic backends don't report tokens — keep them in a separate wall-clock-only
  bucket; don't pollute token regressions with zeros.
- Confounds: challenge mix, model mix, and seed-pool state all move per iteration.
  Stratify every comparison by `(challenge, model)` before aggregating.
- Per-agent Docker volumes already isolate `target/` (`benchmark.py:449-466`); sccache
  must use a *shared* cache volume to actually help across agents.
- Don't measure on a noisy live swarm if avoidable — Phase 3 offline runs give clean
  signal; Phase 1/2 live logging is for realism and the timeout-wall (H3) which only
  shows up against real solution quality.
