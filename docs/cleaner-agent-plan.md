# Cleaner Agent — Design Plan (code-bloat reduction)

## Motivation — a real incident, with numbers

On fable-swarm (2026-07-03), opus-exploiter's job_scheduling best grew to
**2.5 MB across 6 files**. Consequences, all observed:

- Every search/replace call failed with "Prompt is too long (~1.03M tokens,
  limit 1M)" — the S/R prompt inlines the file map, and claude-code adds
  ~400k tokens of its own overhead. The agent burned iterations in a
  fail ×3 → forced-full-rewrite cycle, and the blind rewrite of `mod.rs`
  dropped an import and broke the build.
- A manual dedup showed the bloat was almost entirely **duplication**:
  `track_t46.rs` was a byte-identical copy of `track_t48.rs` *and was never
  dispatched at all*; t44/t45/t47 were 99–100% line-identical forks whose real
  diffs were confined to one module each. Pure mechanical merging cut
  2.5 MB → 1.01 MB with **zero behavior change** (verified by region-exact
  diffs + benchmark parity).

The S/R prompt now subsets files under a char budget
(`_sr_prompt_file_subset`, `sr_prompt_char_budget` = 600k chars), which stops
the hard failure — but subsetting is a tourniquet: an agent editing code it
can only partially see still degrades. The durable fix is keeping the code
small. That is the cleaner's job.

The bar mirrors the HPO plan: **cheap, bounded, honest**. Cleaning costs a
full benchmark (15 min – hours depending on the solver), so it must be rare,
targeted, and gated.

## Trigger — size threshold with hysteresis, not token-limit, not blanket-periodic

Three candidate triggers were considered:

| Trigger | Verdict |
|---|---|
| Reactive: on a token-limit error | Too late — the agent is already degraded, and the S/R subset fix means the error may never fire even while bloat quietly grows. |
| Periodic: clean every trajectory every N iterations | Wasteful — each clean costs a full benchmark, and most trajectories are small. A 30 KB algorithm needs no cleaner. |
| **Size threshold (chosen)** | The size check is free every iteration (`sum(len(v) for v in file_map.values())`), so it *is* periodic monitoring — but it only ever spends a benchmark when bloat actually exists. |

Rules:

- **Fire** when total file-map chars > `cleaner_trigger_chars` (default
  500_000 — below `sr_prompt_char_budget`, so cleaning happens *before* the
  agent starts editing blind).
- **Hysteresis**: a clean only counts as a success if it lands ≤
  `cleaner_target_pct` (default 60%) of the trigger, so a trajectory doesn't
  oscillate just above/below the line.
- **Cooldown**: at most one clean attempt per trajectory per
  `cleaner_cooldown_iters` (default 15) iterations, successful or not. A
  failed clean (gate rejected) must not retry next iteration — the code
  hasn't changed, the outcome won't either.
- **Skip when doomed**: don't clean a trajectory that is
  `stagnation_limit - 1` deep — it's about to be reset/abandoned; the
  benchmark would be wasted.
- **Prophylaxis** (cheap, ships first): once size > 80% of trigger, append a
  one-line warning to the hypothesis/S-R system prompts — "the codebase is
  N chars; prefer edits that reduce duplication; do not clone modules/files."
  Bloat prevented is a benchmark never spent.

## The cleaner iteration (run_loop.py hook)

Hooks in the main loop right after the state fetch / worktree seed, before
hypothesis generation. When the trigger fires, the iteration becomes a
cleaner iteration instead of a mutation iteration:

```
seed worktree with trajectory best (unchanged)
  → deterministic pre-pass                 (no LLM — see below)
  → LLM cleaning pass                      (tiered by provider — see below)
  → cargo check gate                       (existing compile-fix loop, unchanged)
  → benchmark (test seed, default HPs)     (existing run_benchmark, heartbeats on)
  → ACCEPT if:
      feasible on all tracks
      AND direction-aware score within cleaner_score_delta_pct of parent
      AND new_size ≤ cleaner_target_pct × old_size
  → publish with iteration_type="refactor" (see server section)
  → on reject: discard worktree changes, log, start cooldown
```

Delta semantics (direction-aware, like the HPO band):
- max-direction: accept if `score ≥ parent_score × (1 − delta)`
- min-direction: accept if `score ≤ parent_score × (1 + delta)`

`cleaner_score_delta_pct` default **2%**: these solvers are time-budgeted and
anytime, so identical code has run-to-run variance; the 2026-07-03 parity
runs differed by ~0.2% on quiet hardware, but contended hardware needs
headroom. The delta is a *noise allowance*, not a quality budget — the
prompt demands behavior preservation; the delta absorbs measurement noise.

## Edit mechanism — tiered by provider capability

**Full-file rewrite is never the mechanism.** Output limits (~32–64k tokens)
make regenerating even one 500 KB file impossible — that's what broke
`mod.rs` in the incident. The tiers:

### Tier 0 — deterministic pre-pass (no LLM; always runs first)

Today's manual dedup got a 60% reduction with zero LLM involvement, and every
step below is compiler- or diff-verified:

1. **Unreachable-file removal**: parse `mod X;` declarations from the entry
   file transitively; delete `.rs` files never declared (t46 would have been
   caught by dispatch-reachability: declared but never called — catch that
   via the dead-code pass instead).
2. **Identical / near-identical file merge**: pairwise `difflib` ratio over
   the file map; ≥ 99.5% pairs collapse to one module + rewired `mod`/paths
   (t46≡t48 was 100% minus a header comment).
3. **Dead-code strip** (follow-up, not yet implemented): `cargo check` with
   `#[warn(dead_code)]` surfaced; parse the warnings, delete flagged items,
   re-check, iterate to fixpoint. Each round is compiler-verified.

Python-only, lives in `scripts/cleaner_prepass.py` (steps 1–2 implemented,
self-running test `scripts/test_cleaner_prepass.py`). This tier alone may
satisfy the acceptance gate — then no LLM call happens at all.

### Tier 1 — agentic providers (claude-code / codex)

Run a specialized agentic session in the worktree (reusing
`agentic_backends` plumbing): system prompt = "reduce size, preserve
behavior exactly; hoist duplicated functions into shared modules; do not
change constants, algorithms, or the hyperparameter Map plumbing." The agent
has file tools and `cargo check`, so context limits and output limits are
both non-issues — it reads/edits incrementally. This is the natural fit:
dedup is a *navigation-heavy* task (diff, grep, move), exactly what agentic
mode does and single-shot calls don't.

### Tier 2 — big-context API models (no agentic tools)

Chunked S/R, clone-detection assisted. The orchestrator (Python) does the
finding; the LLM only does the merging:

1. Python detects clone pairs *within* the budget: function-level similarity
   (split files at `fn` boundaries, token-shingle similarity — a mini clone
   detector, no LLM).
2. For each clone pair/group, one S/R call whose context is **only the
   cloned functions** (hundreds of lines, not the codebase): "merge into one
   parameterized function + emit S/R blocks patching the call sites shown."
3. Apply, `cargo check`, bounded repair rounds (reuse `_SR_REPAIR_ROUNDS`
   machinery), revert the batch on persistent failure, next pair.

### Tier 3 — small LLMs via API (small context AND small output)

Same as Tier 2 with tighter scoping — and this is the honest answer to
"what if I'm only using small models": **they are fine, because the design
never requires whole-codebase reasoning.** Tier 0 does the bulk mechanical
work with no LLM; the clone detector (Python) does the finding; a small
model only ever sees one clone pair at a time (a few hundred lines in, a
few dozen S/R lines out — comfortably inside a 32k context). If even that
fails validation, the cleaner simply stops after Tier 0 and takes whatever
reduction the pre-pass achieved. Degraded, never broken.

## Server & publish changes (the one real piece of plumbing)

Today `create_iteration` adopts code as trajectory best only when
`beats_trajectory_best` (server.py ~1325). A clean scores ~equal by design,
so it needs its own path:

- `POST /api/iterations` accepts `iteration_type: "refactor"`.
- On an accepted refactor the server **replaces the trajectory-best code /
  files map but keeps the recorded best score** (`max(old_best, refactor
  score)` for gating). Keeping the old score prevents ratchet erosion — a
  −1.9% refactor must not lower the bar the next mutation has to beat.
- A refactor iteration counts as **neither** an improvement (no momentum
  bump, no `improvements` increment, no HPO-gate credit) **nor** stagnation
  (`runs_since_improvement` unchanged). It is bookkeeping, not search.
- Dashboard: render as a distinct marker on the trajectory (size annotation,
  no score movement).

Client side, `publish.py` forwards the flag; `run_loop` sends it only from
the cleaner path.

## Config knobs

Host-tunable via fleet.config.json, same passthrough as the `hpo_*` keys
(`run_fleet._FLEET_WIDE_DEFAULT_KEYS` / run_loop's `_hpo_key` sync loop):

| Key | Default | Meaning |
|---|---|---|
| `cleaner_trigger_chars` | 500_000 | Total file-map chars that arm a cleaner iteration. |
| `cleaner_target_pct` | 60 | Clean must land ≤ this % of pre-clean size to be accepted. |
| `cleaner_score_delta_pct` | 2 | Direction-aware score-noise allowance vs the parent. |
| `cleaner_cooldown_iters` | 15 | Min iterations between clean attempts per trajectory. |
| `cleaner_llm_tier` | auto | `auto` (by provider) / `prepass_only` / `agentic` / `chunked_sr`. |

## Invariants — where it silently breaks

- **HPO plumbing must survive.** Cleaned code must still read the
  hyperparameters `Map` with in-code defaults (the HPO plan's invariant).
  The cleaner prompt forbids touching it; `validate_code` + the benchmark's
  default-HP run enforce it.
- **The anchor import survives**: `use tig_challenges::<ch>::*;` — existing
  `validate_code` check applies to the cleaned map too.
- **Never publish a rejected clean.** The reject path must restore the
  worktree from the seeded best (same discard the compile-fix path uses).
- **Delta is not compoundable.** Two consecutive −2% cleans = −4% real loss.
  The kept-best-score rule plus cooldown bounds this: a second clean is
  compared against the *same* preserved parent score, not the drifted one.
- **The benchmark heartbeat fix must be live** (2026-07-03): a cleaner
  iteration is benchmark-heavy; without heartbeats the trajectory TTL reaps
  it mid-clean and the refactor publishes onto a fresh trajectory — exactly
  the churn bug.

## Rollout order

1. **Prophylactic size warning in prompts** (few lines, zero risk) +
   **Tier 0 pre-pass as a standalone script** — validates the approach on
   real bloated maps offline (the 2026-07-03 dedup is the reference case).
2. **Server/publish `refactor` path** — small, testable
   (`server/test_*.py` hermetic style: refactor keeps score, no
   improvement/stagnation side effects).
3. **Cleaner iteration in run_loop** wired to Tier 0 only.
4. **LLM tiers** (agentic first — it's the least code, reusing
   `agentic_backends`; chunked-S/R clone merging last, it's the most).

Steps 1–3 already capture the incident class we've actually seen (duplicated
files/modules); step 4 is for bloat that mechanical passes can't find.

## Implementation status (2026-07-03)

Rollout steps 1–3 are implemented:

- Prompt size warning: `_cleaner_size_warning` in run_loop.py, appended to
  the hypothesis and S/R user prompts above 80% of the trigger.
- Tier 0 pre-pass: `scripts/cleaner_prepass.py` (duplicate-file merge +
  unreachable-file removal; dead-code strip deferred) +
  `scripts/test_cleaner_prepass.py`.
- Refactor publish path: `iteration_type="refactor"` on POST /api/iterations
  (server.py / models.py) + `server/test_refactor_iteration.py`;
  `publish_results(iteration_type=...)` client-side.
- Cleaner iteration: `_run_cleaner_iteration` in run_loop.py, hooked after
  the seed-bench block; knobs passed through fleet.config.json
  (run_fleet.py key lists + run_loop config sync).

Not yet implemented: Tier 0 dead-code strip, and the LLM tiers (1–3) —
`cleaner_llm_tier` is reserved but unused.
