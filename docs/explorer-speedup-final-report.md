# Explorer vs Exploiter: why the explorer is slower, and how to speed it up

**Final report.** Companion to `explorer-speedup-analysis-plan.md` (which holds the live
FINDINGS LOG F1–F5 and the running plan). This document is the standalone synthesis:
the question, the method, the evidence (offline + live on the real swarm), and the
recommendation.

---

## 1. The question

The exploiter mutates code and benchmarks much faster than the explorer. **Why — exactly —
and can we speed the explorer up without removing its exploration behaviour** (novelty,
bootstrapping, inspiration-driven structural pivots)?

**Answer in one line:** the role flag changes *no* benchmark or model parameter directly;
the gap is entirely a second-order consequence of *what kind of code each role produces*.
Explorers generate large, novel, often-broken, often-slow code; that code costs more to
generate, more to compile (with retries), and **far** more to benchmark (slow solvers run
to the timeout wall). Exploiters refine small, working, fast code and pay none of those
taxes — but trip the HPO sweep when they improve.

The exploration that matters lives in the **prompt, seeding, and inspiration** — not in the
full-file rewrite mechanism or the slow-code benchmark. So the explorer can be sped up
substantially while staying an explorer.

---

## 2. Method

- **Code audit** of role differentiation (`run_loop.py`, `prompts.py`, `server/*`,
  `benchmark.py`) to find every place the roles diverge.
- **Offline micro-measurements** on this host (cargo 1.89, 8 cores, warm `target/`): compile
  tax, the feature-gating fix, per-challenge timeout budgets, algorithm file sizes.
- **Phase 1 instrumentation** (opt-in, `SWARM_ITER_METRICS=1`): per-phase LLM wall time +
  tokens, per-binary build time, per-instance run time / timeout counts → one JSONL line
  per iteration.
- **Live run on the real swarm**: instrumentation cherry-picked into the two fleet agents
  (`dora-the-explorer` / explorer, `swiper-no-exploiting` / exploiter), one iteration each on
  the live challenge **vehicle_routing @ 630s/instance**, model opus-4-7, `claude-code` backend.

---

## 3. The mechanism — where the two roles diverge (from the code)

| Lever | Explorer | Exploiter | Source |
|---|---|---|---|
| Edit mechanism | full-file rewrite (single-file default) | search/replace always | `run_loop.py:_use_search_replace` |
| Cold start | bootstraps from stub | seeded working code; skips if only stub | `server.py:seed_agent_strategy` |
| Prompt steer | "novel, structurally-different, ambitious rewrites" | "ONE localized change, ≤15% lines" | `prompts.py:_role_guidance` |
| Inspiration / peer code | yes (on stagnation) | no | `prompts.py` inspiration block |
| Default model tier | frontier (opus/sonnet-4/gpt-5) | standard (haiku/flash/mini) | `server/tiers.py` |

No role-conditioned benchmark knobs exist: instance counts, timeouts, and worker counts are
fixed in `benchmark.py` / `.swarm-cache.json`. The speed gap is downstream of the rows above.

---

## 4. Evidence

### 4a. Offline (measured, reverted)

- **Compile tax & the wasted ~65%.** A single agent edit recompiles all three binaries in
  **7.7s** (vs 0.47s no-op). The agent edits only `src/<ch>/algorithm/mod.rs`, but
  `pub mod algorithm;` is **not feature-gated**, so the edit needlessly recompiles the
  evaluator and generator. Gating it `#[cfg(feature="solver")]` → **2.8s (−64%)**, evaluator
  & generator drop to 0.13s no-ops. **Pure win, no behaviour change.** Audited safe on exactly
  the **5 CPU challenges** (satisfiability, vehicle_routing, knapsack, job_scheduling,
  energy_arbitrage); the 3 GPU challenges use a single binary and are excluded by design.

- **Timeout-wall is punitive.** The solver is passed **no time budget**
  (`main_solver.rs:run_solve`); the timeout is enforced externally by **SIGKILL**
  (`benchmark.py subprocess timeout`). Slow code runs to the full per-instance budget
  (260–630s on the big CPU challenges) before being killed. A fast solver finishes in seconds.

- **Output-token gap (model prediction).** Full-file rewrite regenerates the whole algorithm;
  search/replace emits one block. Ratio ranges ~1.5–4× (knapsack, 64 lines) to **~22–75×**
  (satisfiability, 1054 lines), and *widens* as algorithms mature.

### 4b. Live (measured on the real swarm — the decisive data)

One iteration each, identical challenge/model/backend:

| | Explorer (dora) | Exploiter (swiper) | ratio |
|---|---|---|---|
| **Iteration wall** | **2313.8s (~39 min)** | **1028.8s (~17 min)** | **2.25×** |
| Code-gen (LLM) | 265.5s | 240.4s | ~1.1× (≈ equal) |
| Compile-fix (LLM) | **155.6s** | — | explorer-only |
| Cold build solver / eval / gen | 6.0 / **4.8 / 4.8s** | (warm last build) | |
| Solver run — max / sum | **615s / 6150s** | **0.11s / 0.89s** | **~5600× slower** |
| Benchmark share of iteration | **~1845s (≈80%)** | ~2s main + ~758s HPO | |
| Feasible / new best | true / false | true / false | |

(`claude-code` reports no tokens, so token columns are 0 — wall-clock is the signal.)

**What the live numbers establish:**

1. **The explorer's own slow code dominates — not generation.** Its feasible-but-novel rewrite
   ran **~5600× slower** than the exploiter's code (615s vs 0.11s per instance), so benchmarking
   it consumed **~80% of a 39-minute iteration**, running right up to the 630s wall. This is the
   timeout-wall, quantified.
2. **Compile-fix tax is real: 156s** — the rewrite failed to compile, costing a fix call the
   exploiter never paid.
3. **The F1 cold-build waste is visible live:** evaluator 4.8s + generator 4.8s rebuilt despite
   only the algorithm changing — **~9.7s/iteration** the gating fix removes.
4. **Backend nuance — the output-token gap is *masked* under `claude-code`.** Code-gen wall was
   *nearly equal* (265s vs 240s), not the 20–75× the token model predicts, because the agentic
   CLI's wall is dominated by its own tool-loop, not raw token streaming. **The edit-mode/
   output-token lever only pays on a raw-API backend (anthropic/openai).**

---

## 5. Can we speed the explorer without losing exploration? Yes.

Exploration is carried by the prompt steer, stub bootstrap, inspiration, and strategy-tag
nudges — none of which a speed lever below touches. Ranked by (impact × safety) **for the live
claude-code/agentic swarm measured here**:

| Lever | Mechanism | Impact (measured/est.) | Risk to exploration | Status |
|---|---|---|---|---|
| **F4 — cooperative deadline / cap slow-code benchmark** | pass the budget into `solve_challenge` so a slow solver returns best-so-far instead of being SIGKILLed at the wall; or a short feasibility-probe timeout | would reclaim most of the explorer's **~1845s/iter** | none (final score unchanged) | design change — recommended first |
| **H4 — reduce compile-fix retries** | tighter rewrite constraints / cargo-check-before-benchmark for explorers | the **156s** fix call + a cold rebuild | low | needs design |
| **F1 — gate `algorithm` behind `solver`** | stop recompiling evaluator/generator on an algorithm edit | **−64% compile, ~9.7s/iter** | **none** (identical output) | ready to ship; audited safe on 5 CPU challenges |
| **HPO trial budget / early-abort** | cap or early-kill walling HPO trials | the exploiter's **~758s** HPO sweep | none | new lever surfaced by the live run |
| **E1 — decouple edit-mode from role** | explorers refine via search/replace, full rewrite only for genuine pivots | the **22–75× output-token** gap — **but masked under agentic backends**; top lever for **raw-API** swarms | medium (needs novelty guardrail, Phase 4) | needs raw-API A/B |
| **E4 — draft model for routine explorer turns** | cheap model for normal turns, frontier for pivots/inspiration | the frontier-tier tax | low–medium | config recommendation |
| sccache (E2) | compiler cache, shared dir | **cold worktree spin-up −76% (24.5s→5.9s)**; nothing on leaf edits | none | measured; complementary to F1 (fleet spin-up, not per-iter) |

**The exploration guardrail (must hold for any lever):** novelty (pairwise code distance vs
lineage and swarm-best), leapfrog rate (structurally-new bests), and bootstrap success must stay
within noise of baseline. Ship a lever only if it speeds things up *and* leaves those unchanged.

---

## 6. What was built, and current state

All phases of the plan are implemented as code/tooling, on **separate branches** so each is
independently reviewable; the analysis branch `multifile-sr-roles-seed-hpo` carries none of it.

| Branch | Contents |
|---|---|
| `phase1-iter-instrumentation` | Phase 1 telemetry (`iter_metrics.py`, opt-in `SWARM_ITER_METRICS=1`) + Phase 2 analyzer (`analyze_iter_timing.py`) + Phase 4 guardrail (`analyze_exploration.py`) + the `edit_mode` fix the live run caught |
| `f1-gate-algorithm-solver-feature` | F1 — the 5-line `#[cfg(feature="solver")]` gate on the 5 CPU challenges; cargo-check-verified, −64% build |
| `e3-feasibility-probe-timeout` | E3 — opt-in two-stage timeout in `benchmark.py` (`TIG_PROBE_TIMEOUT`/`TIG_PROBE_MODE`, `gate`+`cap`) + `test_probe_timeout.py` (19 assertions) |

- **Phase 2 analyzer** decomposes `iter_timing.jsonl` into the per-role gap by component and maps
  each to its hypothesis. On the live run it attributes the 2.25× gap as **87% benchmark/HPO (F4),
  12% compile-fix (H4), 1% build (F1), 2% code-gen (H1/H2, masked under agentic)**.
- **Phase 4 guardrail** computes code-distance novelty + per-role leapfrog rate and exposes an
  acceptance `gate` (faster AND novelty/leapfrog held → SHIP). This is the safety check every speed
  lever must pass.
- **E3 prototype** caps the timeout-wall: `gate` mode saves the full budget on non-converging code
  with no score change; `cap` mode trades quality for speed on feasible-but-slow code.
- **F1** is shippable now; both fleet worktrees also carry the Phase 1 telemetry (dormant unless the
  env flag is set).

**What still needs a sustained run (not code — execution + budget):** Phase 2/4 produce
statistically-weighted results only over ≥200 iterations/role, and E1/E4 are A/B experiments whose
knobs already exist (`edit_mode` opt-in; per-agent `model`). The live VRP@630s cycle (17–39 min/iter)
makes these multi-hour jobs. The tooling to *run and score* them is in place.

---

## 7. Recommended next steps

1. **Ship F1** — clean, audited, ~9.7s/iteration for every agent. Merge `f1-gate-algorithm-solver-feature`.
2. **Trial E3 in `gate` mode** on a swarm (`TIG_PROBE_TIMEOUT≈30`) — measure the benchmark-wall
   reduction via the Phase 2 analyzer; it's the lowest-risk slice of the F4 lever (no score change).
   The fuller F4 (cooperative deadline passed into `solve_challenge`) is the larger follow-on.
3. **Gather a raw-API sample** (anthropic/openai) to expose the output-token gap the agentic backend
   masked, then run the **E1** edit-mode A/B and score it through the Phase 4 `gate`.
4. **Scale the sample** — ≥200 iterations/role for distributional weight; the two live lines are a
   clean proof-of-mechanism, not a distribution.

---

*Numbers in §4b are from `reports/iter_timing.jsonl` captured 2026-06-25; §4a from offline cargo
timing on this host. Full per-finding detail and code references in
`explorer-speedup-analysis-plan.md` (FINDINGS LOG F1–F5).*
