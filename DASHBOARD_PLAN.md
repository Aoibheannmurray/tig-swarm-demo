# Dashboard cleanup & fixes plan

Status: planning (approved scope). Target deployment: the GPU swarm dashboard
(`gpu-bigger-test-production.up.railway.app`). All work is in `dashboard/` plus
a small Rust export for the neural-net loss curve.

## Architecture reality check

There is **one** dashboard codebase, not separate CPU and GPU dashboards. Every
challenge — CPU (satisfiability, knapsack, vehicle_routing, job_scheduling) and
GPU (hypergraph, neuralnet_optimizer, vector_search, energy_arbitrage) —
registers through the single `CHALLENGES` table in
`dashboard/src/challenges/registry.ts` and renders via `DisplayPanelBase`
(`dashboard/src/challenges/base.ts`). The base class is genuinely modular:
shared history/replay/instance-nav/new-best-flash/resize plumbing.

The "GPU feels less modular" smell is **stylistic divergence in three files**,
not an architectural split: the GPU panels each reinvented their bottom
stat-bar markup and CSS (`.hg-*`, `.nn-*`, `.vs-*`, ~300 lines in `style.css`)
instead of reusing the shared `.solution-score` / `.knapsack-value-box`
classes the CPU panels share. Normalising that is Workstream A and de-risks #3.

---

## Workstream A — Modularity foundation (do first; unblocks #3 and #4)

**A1. Shared bottom-stat-bar + score-popover container.**
- Add a reusable scaffold helper to `base.ts`, e.g.
  `statBarScaffold(stats: {label: string; id: string}[], scoreId: string)`,
  emitting the same structure CPU panels use, with the score wrapped in a
  shared positioned container that carries `data-track-score`.
- Migrate `hypergraph.ts`, `neuralnet_optimizer.ts`, `vector_search.ts` onto it.
- Delete the duplicated `.hg-*` / `.nn-*` / `.vs-*` stat-bar rules in
  `style.css` in favour of the shared classes. Keep viz-specific rules
  (galaxy halos, network diagram, etc.).
- Outcome: ~250 fewer CSS lines, one popover container to reason about, GPU
  panels structurally identical to CPU ones.

---

## Workstream B — Scoring-direction in shared code  ✅ DONE (scope corrected)

**Correction (important):** an earlier draft of this section claimed three
direction bugs (base.ts delta, replay.ts improvement, leaderboard sort) on the
theory that VRP and job_scheduling are "min" challenges. **They are not.**
`server/challenges.py` sets `scoring_direction="max"` for **all eight**
challenges — the dashboard score is the TIG **baseline-relative quality score**,
which is higher-is-better for every challenge (native min metrics like VRP
distance / makespan are converted to a max quality score upstream). So:

- **B1 `base.ts › formatScoreDelta`** (assumed max) — **not a bug**; max is
  correct for all challenges. Reverted (no change shipped). The stale "VRP
  overrides this to flip sign" comment describes a mechanism no panel uses; left
  as-is for now (candidate for a separate comment-only cleanup).
- **B3 `leaderboard.ts › DEFAULT_DIR = desc`** (assumed max) — **not a bug**;
  desc is correct. Reverted.
- **B2 `lib/replay.ts`** (assumed **min**, computed `prev - current`) — **the one
  real bug**: wrong for *every* challenge since all are max, so the replay's
  "% improvement" banner + per-step deltas read negative/zero for genuine
  progress. **Fixed** to `current - prev` with simple inline math (no
  direction helper — there are no min challenges, so direction-awareness would
  be dead code).

`feed.ts` was already correct (trusts the server-computed, direction-normalized
`delta_vs_best_pct`). No shared direction helper was added — the codebase keeps
its existing `isMin`/`isBetter` (unused by these panels) for any future need.

**Verification:** press `R` on the dashboard to run the replay; the banner and
per-step deltas now show positive % for improvements. Final diff: `replay.ts`
only.

---

## Workstream C — Comment & dead-code cleanup (clean + modular)

A quality pass so the source reads cleanly, pairing with A1's structural
de-duplication. **Behaviour-preserving only** — no logic changes.

**Remove / correct:**
- **Factually-wrong comments** — e.g. `challenges/base.ts`'s `// VRP overrides
  this to flip sign (lower distance = improvement)` and the matching class-doc
  line: no panel overrides `formatScoreDelta`, and every challenge maximises its
  quality score, so the note is doubly wrong.
- **Comments describing removed behaviour / old field names / pre-refactor
  structure** — anything that no longer matches the code it sits above.
- **Commented-out code blocks** (dead code) and **obsolete TODO/FIXME/XXX**.
- **Redundant comments** that merely restate the line below them.

**Keep (explicitly):** the high-value *why* comments this codebase is good
about — rationale for non-obvious invariants, race/ordering guards, edge-case
handling. The goal is to delete what misleads or adds noise, not to strip
documentation. When a comment is stale *because* of a refactor in A1/#4/#5,
fix it in that same change; this workstream also does a dedicated sweep for the
standalone stale ones (e.g. base.ts) that no other item would touch.

**Verification:** `tsc --noEmit` + `vite build` clean; `git diff` shows only
comment/whitespace deltas for files not otherwise being refactored.

---

## Item 1 — Benchmark per-agent chart crowds on long runs

**Root cause:** `dashboard/src/panels/chart.ts › redrawAgent()` (~lines 506–509)
maps iteration index `0..N-1` onto the fixed container width; a long-running
agent crams hundreds of points into the same pixels with no scroll/overflow.
The benchmark page reuses the same `ChartPanel` as the main dashboard, so the
fix is single-point.

**Fix (approved: scroll + zoom, see Item 2):**
- Enforce a **minimum px-per-point**. When natural width > container width,
  render the SVG at its true width inside a horizontally-scrollable wrapper
  with a **sticky y-axis**, instead of squashing.
- Add a small pure helper (e.g. `chartWidthForPoints(n, container)`) and unit
  test it with vitest.

---

## Item 2 — Draggable axis to zoom into a region

**Enabler:** `d3-zoom` is already a transitive dependency (`d3 ^7.9.0`),
currently unused. The chart is SVG + d3 linear scales and fully rebuilds each
redraw, so zoom state can feed the scales cleanly.

**Fix (approved: both scroll + d3-zoom):**
- Attach a d3-zoom behaviour to the chart that rescales the x-domain (and
  optionally y) and re-renders axes + data.
- Drag-to-pan, wheel/drag to zoom into a region; double-click to reset.
- Coexists with the Item 1 scroll wrapper: zoom adjusts the domain, scroll
  handles overflow at the current zoom. Persist zoom across redraws within a
  tab; reset on tab/challenge switch.

---

## Item 3 — GPU score-click → per-track popover

**Mechanism:** `dashboard/src/panels/stats.ts` binds a click on
`SCORE_VALUE_SELECTOR = ".solution-score-value, [data-track-score]"`, toggles
`solution-score--expanded` on the score's parent, and appends a
`.track-breakdown` popover. Data comes from `/api/state.best_track_scores`.

**Disproved by static analysis (do NOT spend time here):**
- GPU panels DO carry `data-track-score` (hypergraph/neuralnet/vector_search).
- `scripts/benchmark.py › run_gpu_instance()` sets the `track` key on every
  path (success, timeout, non-zero exit, bad JSON), so `aggregate()` produces
  real per-track keys.
- No competing `.solution-score-value` elsewhere, so the click binds correctly.

**Two live-verifiable hypotheses (5-min DevTools check before coding):**
- **(a) Data — prime suspect:** `best_track_scores` is null/empty for GPU
  challenges because the GPU submission / `run_loop` path doesn't forward the
  aggregated `track_scores` into the stored best (even though `aggregate()`
  computes them). Symptom: clicking opens an empty box. Fix: forward
  `track_scores` through the GPU submit path.
- **(b) Layout:** the popover (`position:absolute; bottom:100%`) is
  clipped/mispositioned inside the compact GPU bottom bar. Fix: folds into A1's
  shared, correctly-positioned container.

**Action:** confirm (a) vs (b) live, then apply the matching fix.

---

## Item 4 — Hypergraph visualization

**Status:** confirmed working. `solution_data` shape matches the Rust producer
(`src/main_gpu_benchmark.rs`) exactly; null guards are sound; viewBox comes from
data. No correctness fix required.

**Action:** modularity-only — fold its custom `.hg-bottom-bar` / `.hg-stat-*`
into the A1 shared scaffold. Keep the galaxy render + halo/pulse animations
(legitimately viz-specific).

---

## Item 5 — Neural-net visualization: make it engaging

**Why it's samey:** it varies only two bar widths
(`epochs_used/max_epochs`, `model_loss` vs `noise_floor`) over a static
architecture diagram. The rich signal — per-epoch `train_losses` /
`validation_losses` — is computed in `src/neuralnet_optimizer/mod.rs` but
**never serialized** into `solution_data`.

**Approved direction: P1 + P2 + P3** (house style = SVG + `--t` stagger +
`ease-out` keyframes, as in gantt/knapsack/sat; reuse `lib/animate.ts`).

- **P1 — Training-loss curve draw-in (needs backend export).**
  - Rust: serialize a **downsampled** `train_losses` (+ `validation_losses` if
    available) into the neuralnet `solution_data` in `main_gpu_benchmark.rs`
    (~15 lines + a fixed-length downsample so payload stays small).
  - Frontend: add a `NeuralnetData.loss_curve?: number[]` (and optional
    `val_loss_curve?`) field; render an SVG line that animates in via
    `stroke-dasharray`/`stroke-dashoffset` (like the energy price line).
    Each instance becomes a visually unique curve.
- **P2 — Layer-by-layer node activation (no backend change).** Architecture
  nodes grow/glow left→right with per-layer `--t` stagger; more hidden layers =
  visibly richer sequence.
- **P3 — Epoch milestone flags (no backend change).** Convergence bar fills
  with checkpoint flags (25/50/75/early-stop) appearing in sequence; complements
  P2.

**Graceful degradation:** if `loss_curve` is absent (pre-export rows), fall
back to P2+P3 so the panel still animates.

### Status (implemented)
- **P2 (node activation)** ✅ — arch-diagram nodes scale in left→right, edges
  fade in behind them, trainable layers carry an accent glow.
- **P3 (epoch milestones)** ✅ — 25/50/75/100% flags pop onto the convergence
  bar, lit once that many epochs actually ran.
- **P1 (loss curve)** — frontend ✅: `neuralnet_optimizer.ts` reads optional
  `loss_curve` / `val_loss_curve` from `neuralnet_data` and draws an animated
  sparkline (train line draws in via dashoffset, validation fades in); hidden
  with graceful fallback when absent. `prefers-reduced-motion` respected.
- **P1 backend — DEFERRED (needs a GPU build):** exporting the loss history is
  *not* the "~15 lines" first estimated. The per-epoch `train_losses` /
  `validation_losses` live in the local `training_loop` (src/neuralnet_optimizer/mod.rs),
  not on `Solution`, and `Solution` is defined through the `impl_base64_serde!`
  macro — so adding a field changes the **agent-facing submission wire format**.
  The crate also only compiles with `cudarc/cublas/cudnn` (CUDA required), so it
  can't be type-checked or run in this environment. Spec for the follow-up:
  (1) add `loss_curve: Vec<f32>` (+ optional `val_loss_curve`) to `Solution`
  with a default so existing constructors/deserialisation stay valid;
  (2) have `training_loop` downsample its `train_losses` to ~64 points and store
  them on the returned solution; (3) serialise them into `neuralnet_data` in
  src/main_gpu_benchmark.rs. Must be done on a CUDA box with a round-trip
  serialise/deserialise check so no in-flight agent submissions break. The
  dashboard lights the curve up automatically once the field appears.

---

## Sequencing

1. ~~**B** (scoring-direction)~~ — ✅ done: scope corrected to the single real
   bug (replay direction); B1/B3 were not bugs and were reverted.
2. **C** (comment/dead-code cleanup) — standalone stale-comment sweep now;
   the rest folds into each file as A1/#4/#5 touch it.
3. **A1** (shared scaffold) — foundation.
4. **#4** hypergraph migration (rides on A1).
5. **#3** verify (a) vs (b) live, then fix (popover container from A1).
6. **#1 + #2** together in `chart.ts` (shared by benchmark + main dashboard).
7. **#5** P1 (Rust export + curve) then P2 + P3.

Each step is independently shippable. After each: add/extend vitest for any new
pure helper, and confirm `tsc --noEmit` + `vite build` are clean.
