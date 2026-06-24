"""LLM prompt construction and response parsing for the swarm loop.

All build_*_prompt functions live here, plus hypothesis parsing.
"""

from __future__ import annotations

import json
import re

from challenge_files import is_stub_code, read_tacit_knowledge


# ── Tacit-knowledge distillation switch ────────────────────────────
#
# Single source of truth for which code path owns the tacit-knowledge
# distillation step. Both `build_agentic_user_prompt` (in-band) and
# run_loop._should_distill_tacit (driver-mediated) read this flag.
#
# False (default): agentic providers (claude-code-agentic, codex-agentic)
#   handle distillation themselves via the in-band prompt block. Cheaper
#   — no extra LLM call — but the format is whatever the agent writes.
# True: the driver runs a separate distillation call for every provider,
#   including agentic ones; the in-band prompt block is suppressed.
#   Uniform output format at the cost of one extra LLM call per
#   trajectory reset.
DRIVER_DISTILL_FOR_AGENTIC = False


def _tacit_write_enabled(config: dict) -> bool:
    """Per-agent kill switch for tacit-knowledge writing (mirrors
    `run_loop.tacit_write_enabled`). Defaults to True when `tacit_write`
    is absent. Accepts JSON booleans and the string forms
    "false"/"0"/"no"/"off" (case-insensitive)."""
    value = config.get("tacit_write", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


# ── Strategy tags ──────────────────────────────────────────────────


DEFAULT_STRATEGY_TAGS = [
    "construction", "local_search", "metaheuristic",
    "constraint_relaxation", "decomposition", "hybrid",
    "data_structure", "other",
]


def get_strategy_tags(config: dict) -> list[str]:
    challenge = config.get("challenge", "")
    available = config.get("available_challenges") or {}
    sub = available.get(challenge) or {}
    tags = sub.get("strategy_tags") or []
    return tags if tags else DEFAULT_STRATEGY_TAGS


# ── Role guidance ──────────────────────────────────────────────────
#
# Role is contributor-owned (explorer by default; exploiter opt-in). It only
# changes the *guidance* injected into the per-iteration prompts — never the
# stable rules in CLAUDE.md/AGENTS.md. Explorers are nudged toward novel,
# ambitious work; exploiters are steered toward one small localized edit. This
# is guidance only — the driver no longer enforces it with a similarity gate.


def _role_guidance(role: str) -> str:
    """One short role steer for the hypothesis/code SYSTEM prompts."""
    if role == "exploiter":
        return (
            "ROLE — EXPLOITER (localized edits only): You are refining an "
            "existing working algorithm, NOT rewriting it. Make ONE small, "
            "localized change (a parameter, a single block, one function body). "
            "Preserve the overall structure, control flow, and function set of "
            "the code shown to you. Do NOT rewrite from scratch or change the "
            "algorithmic approach. If you would touch more than ~15% of the "
            "lines, narrow the change."
        )
    return (
        "ROLE — EXPLORER: Bias toward NOVEL, structurally-different strategies "
        "the swarm has not tried. Ambitious rewrites are welcome if you believe "
        "they can leapfrog the current best."
    )


def _niche_nudge(role: str, assigned_tag: str | None) -> str:
    """Soft strategy-tag suggestion (explorers only). Never forced."""
    if role == "exploiter" or not assigned_tag:
        return ""
    return (
        f"The '{assigned_tag}' strategy family looks under-explored right now — "
        f"consider proposing within it, but pick a different STRATEGY_TAG if you "
        f"have a stronger idea."
    )


# ── Hypothesis prompts ─────────────────────────────────────────────


def build_hypothesis_system_prompt(
    challenge_md: str, config: dict, *, is_bootstrap: bool = False,
    role: str = "explorer", assigned_tag: str | None = None,
) -> str:
    challenge = config.get("challenge", "unknown")
    tags = ", ".join(get_strategy_tags(config))
    if is_bootstrap:
        job = (
            "propose an initial algorithm strategy. The current code is a "
            "stub — you need a complete working approach, not a tweak."
        )
    else:
        job = "propose ONE specific change to try."
    # Bootstrap is an explorer-only path (exploiters are guarded out client-side
    # and seeded with working code), so the role steer never conflicts with it.
    extra = "\n\n" + _role_guidance(role)
    niche = _niche_nudge(role, assigned_tag)
    if niche:
        extra += "\n\n" + niche
    return f"""\
You are planning an improvement to a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

Your job: {job} Do NOT write code — just describe the idea.{extra}

Respond in EXACTLY this format (4 lines, nothing else):

TITLE: <short title of what to change, under 80 chars>
DESCRIPTION: <2-3 sentence description of the change and reasoning>
STRATEGY_TAG: <one of: {tags}>
NOTES: <brief interpretation of your approach>"""


def _format_inspiration(state: dict, is_gpu: bool, headline: str) -> list[str]:
    insp = state.get("inspiration_code", "")
    if not insp:
        return []
    out = [f"\n{headline}\n```rust\n{insp}\n```"]
    if is_gpu:
        insp_kernel = state.get("inspiration_kernel_code", "")
        if insp_kernel:
            out.append(f"\nInspiration CUDA kernels:\n```cuda\n{insp_kernel}\n```")
    return out


def build_hypothesis_user_prompt(
    state: dict, config: dict, *, role: str = "explorer",
    assigned_tag: str | None = None,
) -> str:
    parts: list[str] = []
    is_gpu = bool(config.get("is_gpu"))
    niche = _niche_nudge(role, assigned_tag)
    if niche:
        parts.append(niche)

    code = state.get("best_algorithm_code") or ""
    bootstrap = is_stub_code(code)
    if bootstrap and _is_optimizer_hook_challenge(config):
        parts.append(
            "Current algorithm (mod.rs) is a stub — propose an initial "
            "implementation strategy from scratch. The stub below shows the "
            "optimizer hooks your strategy must implement; `solve_challenge` and "
            "the training loop are harness-owned and not part of this file.\n"
            f"```rust\n{code}\n```"
        )
    elif bootstrap:
        parts.append(
            "Current algorithm (mod.rs) is a stub — propose an initial "
            "implementation strategy from scratch. The stub below shows the "
            "exact `solve_challenge` signature any implementation must keep.\n"
            f"```rust\n{code}\n```"
        )
    else:
        parts.append(f"Current algorithm (mod.rs):\n```rust\n{code}\n```")
        if is_gpu:
            kernel_code = state.get("best_kernel_code") or ""
            if kernel_code:
                parts.append(f"\nCurrent CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```")

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = [f"\n{len(prior)} strategies already tried on this code — try something STRUCTURALLY DIFFERENT:"]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            lines.append(f"  - [{tag}] {title}")
        parts.append("\n".join(lines))

    hint = state.get("stagnation_hint")
    if hint == "inspiration":
        parts.extend(_format_inspiration(
            state, is_gpu,
            "Study this approach for ideas (adapt ideas, do NOT copy wholesale):",
        ))
    elif hint == "tacit_knowledge":
        tk = read_tacit_knowledge().strip()
        if tk:
            parts.append(f"\nPersonal strategy hints:\n{tk}")
        else:
            parts.extend(_format_inspiration(
                state, is_gpu, "Study this approach for ideas:",
            ))

    reset = state.get("trajectory_reset")
    if reset:
        rtype = reset.get("type", "")
        if rtype == "adopted_inactive":
            msg = (
                "\nYou are starting from another agent's previous algorithm. "
                "Study the code above and propose an improvement to build on it."
            )
            prior = reset.get("prior_score")
            if prior is not None:
                msg += (
                    f" This code is already this trajectory's best "
                    f"(score {prior}) and is your floor — your change must beat it."
                )
            parts.append(msg)
        else:
            parts.append(
                "\nYou are starting from the template algorithm. "
                "Propose an initial strategy to improve it."
            )

    if role == "exploiter":
        parts.append(
            "\nPropose ONE small, localized improvement to the existing code "
            "above — a single parameter or block, not a rewrite."
        )
    else:
        parts.append("\nPropose one specific improvement to try.")
    return "\n".join(parts)


# ── Tacit-knowledge distillation prompts ───────────────────────────


_TACIT_DISTILL_SYSTEM = (
    "You are distilling a generalisable, cross-problem lesson from a "
    "series of failed optimisation attempts.\n\n"
    "A swarm agent has been working on a single algorithm trajectory and "
    "is about to abandon it because none of its recent attempts improved "
    "the score. Your job is to look at what was tried, what failed, and "
    "write ONE short, transferable insight that would help a future "
    "agent — possibly working on a completely different optimisation "
    "problem — avoid the same dead-end.\n\n"
    "Output requirements:\n"
    "- Exactly one line, prefixed `- LLM: `.\n"
    "- Under 30 words.\n"
    "- No code, no scores, no instance IDs, no challenge names.\n"
    "- Focus on FAILURE PATTERNS: \"X doesn't beat Y when Z\", "
    "\"diagnostic A signals B is wrong\", \"C looks promising but loses "
    "to D\".\n"
    "- Abstract structural properties (constraint tightness, problem "
    "size, solution-space topology) — not problem-specific terminology.\n"
    "- If no genuinely new and transferable lesson has emerged that "
    "isn't already in the existing notes, output exactly: SKIP"
)


def build_tacit_distillation_prompts(
    state: dict, config: dict, current_code: str, existing_tacit: str,
) -> tuple[str, str]:
    """Build (system, user) prompts for the tacit-knowledge distillation
    call. Fired by the driver after the iteration that's about to trigger
    a trajectory reset — at which point `state["prior_hypotheses"]` holds
    the trajectory's accumulated failed attempts, which is exactly the
    material we want to distill from."""
    traj_best = state.get("current_trajectory_best")
    global_best = state.get("best_score")
    runs = state.get("my_runs", 0)
    improvements = state.get("my_improvements", 0)
    stagnation = state.get("my_runs_since_improvement", 0)

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = ["Hypotheses tried against this code (most recent first):"]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            score = h.get("score")
            desc = (h.get("description") or "").strip()
            score_part = f" — score {score}" if score is not None else ""
            lines.append(f"  - [{tag}] {title}{score_part}")
            if desc:
                lines.append(f"    {desc}")
        hypotheses_block = "\n".join(lines)
    else:
        hypotheses_block = (
            "Hypotheses tried against this code: (none recorded — "
            "trajectory has insufficient material; you will likely need "
            "to output SKIP.)"
        )

    existing_llm = [
        ln for ln in (existing_tacit or "").splitlines()
        if ln.startswith("- LLM:")
    ]
    if existing_llm:
        existing_block = (
            "Existing distilled lessons (do NOT duplicate these):\n"
            + "\n".join(f"  {ln}" for ln in existing_llm[-20:])
        )
    else:
        existing_block = "Existing distilled lessons: (none yet)"

    code_block = (
        "Current algorithm (for structural reference; do NOT quote it "
        f"in your output):\n```rust\n{current_code}\n```"
        if current_code else "Current algorithm: (none on disk)"
    )

    user = (
        "Trajectory summary\n"
        f"- Best score on this trajectory: {traj_best}\n"
        f"- Global best: {global_best}\n"
        f"- Runs / improvements / stagnation: {runs} / {improvements} / {stagnation}\n"
        "\n"
        f"{hypotheses_block}\n"
        "\n"
        f"{existing_block}\n"
        "\n"
        f"{code_block}\n"
        "\n"
        "Now: write one `- LLM: ` line distilling a transferable lesson "
        "from the failed hypotheses above, or output SKIP if nothing new "
        "and transferable has emerged."
    )
    return _TACIT_DISTILL_SYSTEM, user


def parse_tacit_distillation(response: str) -> str | None:
    """Extract a `- LLM: …` line from the model's response, or None if it
    indicated SKIP or produced nothing usable. Trims surrounding
    whitespace and rejects any output that doesn't start with the
    `- LLM:` prefix on its first non-empty line."""
    if not response:
        return None
    for line in response.strip().splitlines():
        s = line.strip()
        if not s:
            continue
        if s.upper() == "SKIP":
            return None
        if s.startswith("- LLM:"):
            return s
        # First non-empty line wasn't what we asked for — reject.
        return None
    return None


# ── Code generation prompts ────────────────────────────────────────

# Rust guardrails appended to every code-generation / compile-fix system
# prompt. These target the compile failures we see most from LLM output:
# stray crate imports, signature drift, panics, and type mismatches. Kept
# short so it costs little on capable models.
RUST_RULES = """\

RUST RULES (the output is compiled as-is — it MUST build):
- Available crates: `std` plus `anyhow`, `rand`, `serde`, `serde_json` (already
  in Cargo.toml). Do NOT add any OTHER crate (no rayon, itertools, ndarray, …)
  and do NOT edit `[dependencies]`.
- KEEP the `use super::*;` import and the other `use` lines the starting file
  relies on (e.g. `use anyhow::...;`) — dropping a needed import makes the file
  fail to compile with `E0425: cannot find type ... in this scope`.
- Keep the EXACT signatures of the harness entry-point function(s) you were
  given and don't rename them: for MOST challenges that is `fn solve_challenge(`
  (call the provided `save_solution` closure to record solutions); for
  `neuralnet_optimizer` it is the three `pub fn optimizer_*` hooks — there you
  must NOT define `solve_challenge` or call `save_solution` (both are
  harness-owned). Write only the function(s) your challenge actually uses.
- No `unsafe`, no `async`, no spawning threads.
- Don't leave `todo!()`, `unimplemented!()`, or `panic!()` in a normal path.
- Avoid `.unwrap()`/`.expect()` on values that can be `None`/`Err`; handle the
  empty case. Guard indexing (`slice[i]`) so it can't go out of bounds.
- Mind integer types: index/length math is `usize`; cast explicitly with `as`
  rather than mixing `usize`/`u32`/`i64` in one expression.
- A method call ALWAYS needs parentheses, even with a turbofish:
  `rng.gen_range(0..n)`, `x.powi(2)`, `v.len()` — never `rng.gen::<f64>` or
  `x.powi::<i32>` on their own (that is `E0425: field expressions cannot have
  generic arguments`).
- If you need randomness it MUST be seeded so runs are deterministic. Use
  exactly: `use rand::{rngs::StdRng, Rng, SeedableRng};` then
  `let mut rng = StdRng::from_seed(challenge.seed);` (the seed is `[u8; 32]`).
  Draw with `rng.gen_range(0.0..1.0)` / `rng.gen_range(0..n)`. Do NOT invent your
  own randomness or (ab)use hashing for it — no `RandomState`, `hash_one`,
  `DefaultHasher`, or clock/time seeds (those don't compile or aren't
  deterministic). If you don't need randomness, don't import `rand` at all.
- When the borrow checker would object, clone the data instead of leaving
  code that won't compile."""

# Extra, more prescriptive guidance injected only when an agent sets
# `detailed_prompts: true` (typically smaller / cheaper models whose raw Rust
# tends not to compile). Capable models don't need this verbosity.
RUST_RULES_DETAILED = """\

EXTRA RULES FOR A CLEAN COMPILE (smaller models — follow ALL of these):

RULE 1 - NO DUPLICATE STRUCTS:
  Each struct (e.g. a Hyperparameters config) is defined ONCE. Modify the
  existing definition in place; never add a second
  `pub struct <Name> { ... }` with a name that already exists.

RULE 2 - BORROW CHECKER:
  Copy a value out before mutating the collection it came from.
  BAD:  let item = vec.choose(&mut rng).unwrap(); vec.retain(...);
  GOOD: let item = *vec.choose(&mut rng).unwrap(); vec.retain(...);
  Alternative: remove by index with `let x = vec.remove(idx);`. When the
  borrow checker objects, clone the data rather than leaving code that won't
  build.

RULE 3 - TRAIT IMPORTS:
  Import every trait whose methods you call, with the other `use` lines at the
  top of the file — e.g. `use rand::prelude::{SliceRandom, IteratorRandom};`
  for `.choose()` / `.shuffle()`. A missing trait import is a compile error.

RULE 4 - BRACE BALANCE:
  Before returning, verify every `{`, `(`, and `[` has a matching close.

RULE 5 - COUNT SYMMETRIC PAIRS ONCE:
  When summing over a symmetric matrix, iterate unordered pairs only:
  `for i in 0..n { for j in (i+1)..n { /* use m[i][j] */ } }` — double-counting
  silently doubles the objective.

RULE 6 - USE i64 FOR ACCUMULATORS:
  Sum into an i64 to avoid u32/i32 overflow and to allow negative deltas:
  `let mut total: i64 = 0; total += value as i64;`

RULE 7 - DEFINE BEFORE USE:
  Every variable must be declared before its first use within its scope; don't
  reference a binding from a sibling or inner block that isn't visible there.

GENERAL COMPILE HYGIENE:
- Before finishing, mentally run `cargo check`: every variable is used or
  prefixed with `_`; every `match` is exhaustive; every branch returns the
  same type; no semicolon dropped where a value is expected.
- Prefer iterator methods you are sure of (`.iter()`, `.enumerate()`,
  `.map()`, `.filter()`, `.sum()`, `.min()/.max()`) over hand-written index
  loops; when you do index, derive the bound from `.len()`.
- Annotate numeric literals when the type is ambiguous (`0usize`, `1.0f64`).
  Use `as f64` / `as usize` for casts; never rely on implicit coercion. Calling
  a method on a bare literal needs the type pinned: write `2.0_f64.sqrt()` or
  `(n as f64).powi(2)`, never `2.0.sqrt()` (that is `E0689: can't call method on
  ambiguous numeric type`).
- Reading config from parsed JSON: a `Map<String, Value>` is NOT a `Value`, so
  do NOT `serde_json::from_value(...)` the whole map (that is a type error) —
  pull keys individually with a default. `Value::as_f64()` returns an `f64`, so
  the `.unwrap_or(...)` argument must be an `f64` literal and you cast to `f32`
  only AFTER `unwrap_or`: `m.get("lr").and_then(|v| v.as_f64()).unwrap_or(0.001)
  as f32` — passing an `f32` to `.unwrap_or` here is `expected f64, found f32`.
- Declare `let mut x = ...` whenever you later mutate `x` or call a `&mut`
  method on it; "cannot borrow as mutable, not declared as mutable" just means a
  missing `mut`.
- Any struct YOU define that you `.clone()`, sort, or push into a `BinaryHeap`
  must carry the right derives: `#[derive(Clone)]` (and additionally
  `#[derive(PartialEq, Eq, PartialOrd, Ord)]` for a `BinaryHeap`). Without
  `#[derive(Clone)]`, `x.clone()` silently clones a `&reference` instead and the
  next mutation fails with `E0596: cannot borrow ... as mutable`.
- A trait method only works if the trait is in scope. If rustc says `method not
  found ... trait X ... is implemented but not in scope`, add the `use` it
  suggests — do NOT rewrite the call into something else.
- Don't introduce new generics, trait bounds, lifetimes, or macros unless the
  starting code already uses them — they are a common source of errors.
- Reuse the data structures already imported via `use super::*;`; don't invent
  types, constants, or functions that aren't defined. A `crate::...::NAME` path
  (or a bare name) that isn't actually declared is `E0425: cannot find
  value/function`. If you need a threshold or hyperparameter, define it as a
  local `const` in your own file (e.g. `const MIN_LOSS_DELTA: f32 = 1e-4;`)
  rather than referencing one you assume the module exports.
- Indexing a `Vec`/slice MOVES the element when it isn't `Copy` — that's
  `E0507: cannot move out of ... behind a shared reference`. Bind by reference
  (`let x = &v[i];`) or `.clone()` it; never `*v[i]` or destructure-by-value out
  of a borrowed container (e.g. `let (_, s) = *states[k].iter().max()...;`).
- Prefer small, incremental edits over a full-file rewrite: most compile
  failures come from rewriting the whole file and letting signatures or imports
  drift. Change the algorithm bodies and leave the boilerplate intact.
- Keep the change focused: modify the algorithm logic, not the function
  surface, so the result still slots into the existing module."""


# Appended to the Rust guardrails for GPU challenges. This repo pins a SPECIFIC
# cudarc fork (tig-foundation/cudarc, branch runtime-fuel/cudnn-cublas) whose API
# differs from the older cudarc that most models were trained on. Weaker models
# reach for `htod_copy` / `stream.module()` / `clone_into_vec` (old API) and the
# build fails with E0599/E0609. Pin the exact current API here.
CUDA_API_RULES = """\

CUDA / cudarc API (this repo's fork — use EXACTLY these, not older cudarc names):
- Load a kernel from the `module` parameter, NOT the stream: `let f =
  module.load_function("kernel_name")?;`. `stream` has no `.module()` and no
  `.dev` field.
- Host -> device (upload a Vec/slice): `let d = stream.memcpy_stod(&host_vec)?;`
  — NOT `htod_copy`, `copy_into_slice`, `htod_sync_copy`, or `stream.dev.*`.
- Host -> device INTO AN EXISTING buffer (overwrite, no realloc):
  `stream.memcpy_htod(&host_vec, &mut d_dst)?;` — argument order is HOST slice
  FIRST, the `&mut` device buffer SECOND. Reversing them (`memcpy_htod(&mut
  d_dst, &host_vec)`) gives `E0277: HostSlice not implemented for CudaSlice` and
  `E0308: types differ in mutability`. Use `memcpy_stod` to get a NEW buffer;
  use `memcpy_htod` only to refill one you already allocated.
- Device -> host (download to a Vec): `let v: Vec<f32> = stream.memcpy_dtov(&d)?;`
  — NOT `clone_into_vec`, `clone_slice_to_host`, or `dtoh_sync_copy`.
- Device -> device (copy between two device buffers): `stream.memcpy_dtod(&src,
  &mut dst)?;` — NOT `dtod_copy`, `copy`, or `clone`. (`stream.clone()` only
  bumps the Arc refcount; it does NOT copy GPU data.)
- Allocate a zeroed device buffer: `let mut d = stream.alloc_zeros::<f32>(n)?;`.
- Launch (must be inside `unsafe`):
      let cfg = LaunchConfig { grid_dim: (g,1,1), block_dim: (t,1,1), shared_mem_bytes: 0 };
      unsafe { stream.launch_builder(&f).arg(&d_in).arg(&mut d_out).arg(&n).launch(cfg)? };
- `LaunchConfig` has EXACTLY three fields — `grid_dim`, `block_dim`,
  `shared_mem_bytes`. There is no `block` or `shared_mem` field and no
  `grid_dim(...)`/`block_dim(...)` associated fn; for a 1-D element-wise launch
  you may instead write `LaunchConfig::for_num_elems(n as u32)`.
- Kernel `.arg(...)` accepts ONLY: `&CudaSlice<T>`, `&mut CudaSlice<T>`, or `&T`
  for a read-only scalar (e.g. `&(n as i32)`). You may NOT pass `&mut f32` /
  `&mut i32` (a mutable scalar reference) — that is the E0277
  `PushKernelArg<&mut f32>` error. To get a scalar OUT of a kernel, allocate a
  1-element slice `let mut d_x = stream.alloc_zeros::<f32>(1)?;`, pass
  `.arg(&mut d_x)`, then read it back with `stream.memcpy_dtov(&d_x)?[0]`.
  A `Vec<CudaSlice<T>>` or `&[CudaSlice<T>]` is NOT a valid arg either — that is
  the E0277 `DeviceRepr is not implemented for Vec<CudaSlice<...>>` error. Pass
  each `CudaSlice` in its own `.arg(...)`, or keep the data in ONE flat
  `CudaSlice` rather than a Vec-of-slices.
- To operate on N parameter tensors, do NOT extract raw device pointers
  (`device_ptr`, `device_ptr_mut`, `as_device_ptr`, `as_kernel_param`) and pack
  them into a `Vec<u64>`/pointer array — that is not how this fork passes buffers
  (those are E0599 / trait-not-in-scope). Instead LOOP over the tensors and
  launch the elementwise kernel ONCE PER TENSOR, passing each `&CudaSlice`
  straight to `.arg(...)`, exactly like the SGD seed:
      for grad in gradients {
          let mut update = stream.alloc_zeros::<f32>(grad.len())?;
          let cfg = LaunchConfig::for_num_elems(grad.len() as u32);
          unsafe { stream.launch_builder(&kernel)
              .arg(grad).arg(&(grad.len() as u32)).arg(&lr).arg(&mut update).launch(cfg)?; }
          updates.push(update);
      }
- A single `.launch_builder(...)` chain may NOT borrow the same buffer both
  immutably and mutably — `.arg(&state.x[i]).arg(&mut state.x[i])` is the E0502
  "cannot borrow as mutable because it is also borrowed as immutable" error.
  Allocate a separate output buffer and write there, or read the input into a
  host Vec first; never alias one slice as both an in and an out arg. The same
  rule applies to `stream.memcpy_dtod(&v[a], &mut v[b])` when both elements come
  from the SAME `Vec`/slice — that is also E0502; copy via a temporary buffer
  (or a host Vec round-trip) instead of borrowing two elements of one Vec at
  once.
- Kernel function names in `module.load_function("...")` must match the
  `extern "C" __global__` names in kernels.cu exactly. Every variable the kernel
  reads must be a declared parameter — a bare name like `second_moments` that
  isn't a parameter is an nvcc "identifier is undefined" error.
- A kernel parameter you WRITE to must be a non-const pointer (`float* out`), not
  `const float*` — assigning to an element of a `const` pointer (`out[idx] = x;`)
  is the nvcc "expression must be a modifiable lvalue" error. Mark only the
  read-only inputs `const`."""


EVOLUTION_GUIDANCE = """\

This is an evolutionary search environment — code is mutated over many
iterations. Favour code that is:
- Modular: construction, refinement, local search, perturbation as separate fns.
- Mutatable: key decisions in named params/consts so later iterations can tune them.
- Robust: handle every instance size and parameter/budget range, not one case.
- Adaptive: detect instance characteristics and adjust strategy accordingly.
- Not overfitted to a single scenario.

Priorities, in order: 1) feasibility (never violate constraints) 2) solution
quality (maximise the objective) 3) stability across instances 4) runtime
efficiency (leave headroom for more refinement iterations)."""


# Challenges where `solve_challenge` + the training loop are harness-owned and
# the agent edits ONLY the optimizer hooks (kept in sync with challenge_files.py
# `_OPTIMIZER_HOOK_CHALLENGES`).
_OPTIMIZER_HOOK_CHALLENGES = {"neuralnet_optimizer"}


def _is_optimizer_hook_challenge(config: dict) -> bool:
    return config.get("challenge") in _OPTIMIZER_HOOK_CHALLENGES


# Override note appended to the Rust guardrails for optimizer-hook challenges,
# where the generic "keep the solve_challenge signature" advice doesn't apply.
_OPTIMIZER_HOOK_RULES = (
    "\n- THIS CHALLENGE: `solve_challenge` and the training loop are harness-owned "
    "and are NOT in your file — do not add or call them. Implement ONLY the "
    "optimizer hooks `optimizer_init_state`, `optimizer_query_at_params`, and "
    "`optimizer_step`; keep them `pub fn` with their exact signatures. `Map`/`Value` "
    "are only needed if you add `Hyperparameters` fields."
)


# Full optimizer-hook contract — injected into the code-generation system prompt
# and the agentic CLAUDE.md/AGENTS.md for optimizer-hook challenges. This is the
# detailed, signature-level guidance the agent needs now that `solve_challenge`
# is harness-owned (it replaces the equivalent depth the old solve_challenge
# guidance used to carry).
OPTIMIZER_HOOK_CONTRACT = """\

## Optimizer contract (neuralnet_optimizer) — follow EXACTLY

`solve_challenge` and the training loop are HARNESS-OWNED and are NOT in your
file. Do NOT define, call, or reference `solve_challenge` or `training_loop`.
You implement ONLY the three optimizer hooks (plus `Hyperparameters`, `help`,
your `OptimizerState`, and any helper functions / CUDA kernels). The hooks MUST
be `pub fn` with these EXACT signatures — the harness calls them by name, so a
signature/name/visibility change is a compile error:

    pub fn optimizer_init_state(
        seed: [u8; 32],
        param_sizes: &[usize],
        stream: Arc<CudaStream>,
        module: Arc<CudaModule>,
        prop: &cudaDeviceProp,
    ) -> Result<Box<dyn OptimizerStateTrait>>

    pub fn optimizer_query_at_params(
        optimizer_state: &dyn OptimizerStateTrait,
        model_params: &[CudaSlice<f32>],
        epoch: usize,
        train_loss: Option<f32>,
        val_loss: Option<f32>,
        stream: Arc<CudaStream>,
        module: Arc<CudaModule>,
        prop: &cudaDeviceProp,
    ) -> Result<Option<Vec<CudaSlice<f32>>>>

    pub fn optimizer_step(
        optimizer_state: &mut dyn OptimizerStateTrait,
        model_params: &[CudaSlice<f32>],
        gradients: &[CudaSlice<f32>],
        epoch: usize,
        train_loss: Option<f32>,
        val_loss: Option<f32>,
        stream: Arc<CudaStream>,
        module: Arc<CudaModule>,
        prop: &cudaDeviceProp,
    ) -> Result<Vec<CudaSlice<f32>>>

State — define ONE struct that implements the provided `OptimizerStateTrait`
(in scope via `use super::*;`) and derives Clone:

    #[derive(Clone)]
    struct OptimizerState { /* lr, step count, momentum/variance buffers, ... */ }
    impl OptimizerStateTrait for OptimizerState {
        fn as_any(&self) -> &dyn std::any::Any { self }
        fn as_any_mut(&mut self) -> &mut dyn std::any::Any { self }
        fn box_clone(&self) -> Box<dyn OptimizerStateTrait> { Box::new(self.clone()) }
    }

`OptimizerStateTrait` requires `Send + Sync`, so every field of `OptimizerState`
must itself be `Send + Sync`. Do NOT put `RefCell`, `Cell`, or `Rc` in it
(`RefCell<...> cannot be shared between threads safely` / `Sync is not
implemented`), and never call `.borrow()`/`.borrow_mut()` on your state. Use
plain owned fields (`f32`, `usize`, `bool`, `Vec<CudaSlice<f32>>`, …). The
tempting reason to reach for `RefCell` is that `optimizer_query_at_params` takes
`&dyn` state (READ-ONLY) — resist it: do ALL state mutation (step counters, slow
/ momentum / EMA buffers, flags) in `optimizer_step`, which gets `&mut` state.
If you feel you must update a field during `query`, that is the signal to move
that update into `optimizer_step` instead. Only reach for `Mutex`/`RwLock` if
you genuinely must, and never `Rc`/`Cell`.

Recover it inside the hooks with
`optimizer_state.as_any().downcast_ref::<OptimizerState>().unwrap()` (use
`as_any_mut().downcast_mut::<OptimizerState>()` in `optimizer_step`).

Semantics (get these right — they are the usual source of wrong output):
  - optimizer_init_state: called ONCE before training. `param_sizes` has one
    entry per parameter tensor, in order; size any buffers from it. Return the
    boxed state. Do NOT use `seed` to reconstruct the dataset.
  - optimizer_query_at_params: return `Ok(None)` for a standard optimizer.
    Return `Some(modified_params)` only for lookahead / parameter-prediction
    (the loop computes gradients at those params, then restores the originals).
  - optimizer_step: return a Vec with ONE CudaSlice per parameter tensor — the
    DELTA to ADD to the weights (the harness does `params += delta`), in the
    SAME length and order as `gradients`. Example (SGD): `delta[i] = -lr*grad[i]`.
    The harness applies the deltas and AUTOMATICALLY skips frozen layers — do
    not special-case, read, or overwrite frozen layers, and never write into
    `model_params` in place (they are copies; in-place writes do nothing and
    are treated as tampering).
  - You never see the test set; the harness computes the scored test loss.

The RETURN TYPE is part of each hook's signature — `optimizer_step` returns
`Result<Vec<CudaSlice<f32>>>`, NOT `Result<()>`. If the build says `expected fn
pointer, found fn item`, one of your hooks has the wrong signature (usually the
return type); fix the hook to match exactly — do not cast or wrap it.
"""


def _rust_rules_block(config: dict) -> str:
    """The Rust guardrails to append to a code prompt — base rules always,
    plus the detailed checklist when the agent opted in via
    `detailed_prompts`."""
    block = RUST_RULES
    if config.get("detailed_prompts"):
        block += "\n" + RUST_RULES_DETAILED
    if config.get("is_gpu"):
        block += CUDA_API_RULES
    if _is_optimizer_hook_challenge(config):
        block += _OPTIMIZER_HOOK_RULES
    return block


def build_code_system_prompt(
    challenge_md: str, config: dict, *, role: str = "explorer",
) -> str:
    challenge = config.get("challenge", "unknown")
    is_gpu = bool(config.get("is_gpu"))
    timeout = config.get("timeout", 30)
    opt_hooks = _is_optimizer_hook_challenge(config)
    if opt_hooks:
        time_guidance = (
            f"\nPer-instance time budget: {timeout} seconds — the harness-owned training "
            f"loop is killed at this hard deadline and the best checkpoint it saved is "
            f"evaluated. Keep your optimizer hooks fast so more epochs fit in the budget; "
            f"the harness calls save_solution for you (you do NOT call it)."
        )
    else:
        time_guidance = (
            f"\nPer-instance time budget: {timeout} seconds. Your solver process is killed "
            f"after this hard deadline. Call save_solution() early with your first feasible solution, then "
            f"keep improving and re-saving — the last saved solution is evaluated. If no "
            f"solution was saved when the deadline hits, the instance counts as infeasible."
        )
    # For exploiters, inject the localized-edit rule between the time budget and
    # the output-format rules so it's read before they start writing.
    if role == "exploiter":
        time_guidance += "\n\n" + _role_guidance(role)
    rust_rules = _rust_rules_block(config)
    opt_contract = OPTIMIZER_HOOK_CONTRACT if opt_hooks else ""
    if opt_hooks:
        entry_rule = (
            "- `solve_challenge` and the training loop are FIXED by the harness — do NOT "
            "define or modify them. Implement ONLY the optimizer hooks: `pub fn "
            "optimizer_init_state`, `pub fn optimizer_query_at_params`, `pub fn "
            "optimizer_step` (keep them `pub fn` with their exact signatures), plus "
            "`Hyperparameters`, `help`, and any helpers you need. See the optimizer "
            "contract below for the exact signatures."
        )
    else:
        entry_rule = "- `fn solve_challenge(` MUST appear in your Rust output — do NOT omit it."
    if is_gpu:
        return f"""\
You are optimizing a Rust+CUDA algorithm for the "{challenge}" GPU challenge.

{challenge_md}
{time_guidance}

IMPORTANT RULES:
- `use super::*;` must remain as the first import in the Rust file.
{entry_rule}
- Return BOTH files: the complete Rust source AND the complete CUDA kernel source.
- Separate them with a line containing exactly: // --- kernels.cu ---
- The Rust file comes FIRST, then the separator, then the CUDA file.
- No explanation, no markdown fences — just the two raw source files with the separator.
- Kernel function names in mod.rs (module.get_function("...")) must match the extern "C" __global__ function names in kernels.cu.{opt_contract}{rust_rules}{EVOLUTION_GUIDANCE}"""
    return f"""\
You are optimizing a Rust algorithm for the "{challenge}" challenge.

{challenge_md}
{time_guidance}

OUTPUT FORMAT (strict):
Your response will be written verbatim to mod.rs and compiled. The very first
character of your response MUST be `u` from `use super::*;`. No preamble, no
prose, no markdown fences (```), no commentary before or after the code.
`use super::*;` must remain as the first import.{opt_contract}{rust_rules}{EVOLUTION_GUIDANCE}"""


def build_code_user_prompt(
    state: dict, hypothesis: dict, config: dict, *, role: str = "explorer",
) -> str:
    parts: list[str] = []
    is_gpu = bool(config.get("is_gpu"))
    opt_hooks = _is_optimizer_hook_challenge(config)

    code = state.get("best_algorithm_code") or ""
    # Exploiters never bootstrap (the driver guards stub + exploiter), so treat
    # their starting code as real even on the off chance a stub slips through.
    bootstrap = is_stub_code(code) and role != "exploiter"
    if bootstrap and opt_hooks:
        parts.append(
            "Current algorithm (mod.rs) is a stub — replace it with a complete "
            "implementation. `solve_challenge` and the training loop are harness-owned "
            "(not in this file): implement only the optimizer hooks shown below, "
            "keeping them `pub fn` with their exact signatures.\n"
            f"```rust\n{code}\n```"
        )
    elif bootstrap:
        parts.append(
            "Current algorithm (mod.rs) is a stub — replace it with a complete "
            "implementation. Keep the exact `solve_challenge` signature shown "
            "below (parameter names and types), and call the `save_solution` "
            "closure parameter whenever you find an improved solution.\n"
            f"```rust\n{code}\n```"
        )
    else:
        parts.append(f"Current algorithm (mod.rs):\n```rust\n{code}\n```")

    if is_gpu:
        kernel_code = state.get("best_kernel_code") or ""
        if kernel_code:
            parts.append(f"\nCurrent CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```")
        else:
            parts.append(
                "\nNo CUDA kernel file yet — write any custom GPU kernels you need."
            )
        first_file_desc = (
            "with the optimizer hooks" if opt_hooks else "with fn solve_challenge"
        )
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            f"\nThe Rust file ({first_file_desc}) comes first, then the separator, then the CUDA file."
        )

    title = hypothesis.get("title", "")
    description = hypothesis.get("description", "")
    verb = "Implement this strategy" if bootstrap else "Apply this change"
    parts.append(f"\n{verb}:\n{title}\n{description}")

    if role == "exploiter":
        parts.append(
            "\nApply ONE localized change only — preserve the rest of the code "
            "and return the COMPLETE file (still starting with `use super::*;`)."
        )

    return "\n".join(parts)


# ── Search/replace (soft edit) prompts ─────────────────────────────
#
# Used in API (non-agentic) mode for multi-file algorithms, exploiters, and any
# agent with `edit_mode: search_replace`. Instead of returning whole files the
# model emits targeted SEARCH/REPLACE blocks (parsed by scripts/search_replace).
# Far cheaper on output tokens; the apply step is whitespace-tolerant.

SEARCH_REPLACE_FORMAT = """\
OUTPUT FORMAT (strict) — emit ONLY search/replace blocks, no prose, no fences:

<<<<<<< SEARCH <relpath>
<exact lines copied from the current file to find>
=======
<the replacement lines>
>>>>>>> REPLACE

Rules:
- `<relpath>` is the file to edit (e.g. `mod.rs`, `helpers.rs`). Always include it.
- The SEARCH text must be copied verbatim from the file shown below and be large
  enough to match EXACTLY ONE place — include a few surrounding lines for context
  if a snippet would otherwise be ambiguous.
- Emit one block per distinct edit; you may emit several blocks.
- Change ONLY what your hypothesis requires; leave everything else untouched.
- Keep `use super::*;` intact and do not change the `solve_challenge` signature
  (or, for optimizer-hook challenges, the hook signatures)."""


def _format_files_for_prompt(files: dict) -> str:
    parts = []
    for path in sorted(files):
        lang = "cuda" if path.endswith((".cu", ".cuh")) else "rust"
        parts.append(f"### {path}\n```{lang}\n{files[path]}\n```")
    return "\n\n".join(parts)


def build_search_replace_system_prompt(
    challenge_md: str, config: dict, *, role: str = "explorer",
) -> str:
    challenge = config.get("challenge", "unknown")
    timeout = config.get("timeout", 30)
    role_steer = ("\n\n" + _role_guidance(role)) if role == "exploiter" else ""
    rust_rules = _rust_rules_block(config)
    opt_contract = OPTIMIZER_HOOK_CONTRACT if _is_optimizer_hook_challenge(config) else ""
    return f"""\
You are optimizing a Rust algorithm for the "{challenge}" challenge by making
targeted edits to its source files.

{challenge_md}

Per-instance time budget: {timeout} seconds.{role_steer}

{SEARCH_REPLACE_FORMAT}{opt_contract}{rust_rules}{EVOLUTION_GUIDANCE}"""


def build_search_replace_user_prompt(
    files: dict, hypothesis: dict, config: dict, *, role: str = "explorer",
) -> str:
    title = hypothesis.get("title", "")
    description = hypothesis.get("description", "")
    parts = [
        "Current algorithm source files:\n",
        _format_files_for_prompt(files),
        f"\nApply this change as search/replace blocks:\n{title}\n{description}",
    ]
    if role == "exploiter":
        parts.append(
            "\nMake ONE small, localized change — the fewest blocks that "
            "implement the hypothesis. Do not restructure the code."
        )
    return "\n".join(parts)


def build_search_replace_repair_prompt(
    files: dict, misses_text: str, config: dict,
) -> str:
    return (
        "Some of your search/replace blocks did not match the current files and "
        "were skipped. Re-emit ONLY the failed edits, copying the SEARCH text "
        "EXACTLY from the files below (enough lines to be unique).\n\n"
        "Failed blocks:\n" + misses_text +
        "\n\nCurrent files:\n" + _format_files_for_prompt(files) +
        "\n\n" + SEARCH_REPLACE_FORMAT
    )


# ── Hyperparameter extraction prompts (Phase 3) ────────────────────
#
# A separate LLM call, made only when a candidate passes the tuning gate (see
# docs/hyperparameter-search-plan.md). It reads the *final, compiled* mutated
# algorithm (ALL its files) and (1) decides which constants become tunable
# hyperparameters with a search range, (2) suggests a few concrete configs, and
# (3) makes each chosen constant read from the `Map` argument with the in-code
# value as default, so an empty Map reproduces the default-score behaviour
# exactly. The rewrite is expressed as SEARCH/REPLACE edits (multi-file aware),
# or omitted entirely when the algorithm already applies the Map to a config.
#
# Response layout: a JSON spec, then (only if code edits are needed) the
# separator line below, then SEARCH/REPLACE blocks.
HYPERPARAM_EDITS_SEP = "// --- hyperparameter edits ---"


def build_hyperparameter_system_prompt(challenge_md: str, config: dict) -> str:
    challenge = config.get("challenge", "unknown")
    rust_rules = _rust_rules_block(config)
    return f"""\
You are tuning a Rust algorithm for the "{challenge}" challenge. The algorithm
has already been written (possibly across several files); your job is NOT to
change its logic, but to expose its most impactful magic-number constants as
searchable hyperparameters so a search can find a good configuration.

Do TWO things:

1. Choose 2-5 constants that are genuinely worth tuning (a learning rate, a
   temperature, a restart count, a threshold, a population size, …). Prefer
   FEWER — a smaller search space is searched better. For each, give a sensible
   search `range` that brackets plausibly-good values, a `scale` ("log" for
   multiplicative quantities like learning rates / temperatures, "linear"
   otherwise), a `type` ("float", "int", or "categorical"), and the `default`
   equal to the constant's CURRENT in-code value.

2. Make each chosen hyperparameter read from the
   `hyperparameters: &Option<Map<String, Value>>` argument, falling back to its
   current in-code value when the key is absent. This MUST be behaviour-
   preserving: an empty or `None` map must reproduce today's behaviour EXACTLY.

   SCOPE RULE — the Map is a function ARGUMENT, in scope ONLY at the entry
   (`solve_challenge`, or the optimizer hooks). You CANNOT read it from a
   module-level `const`/`static` or from a helper that isn't given it. So:
   - If the algorithm ALREADY threads the Map into a config/params object
     (e.g. `Config::initialize(hyperparameters, …)` that merges Map keys onto a
     struct), and your chosen names are existing FIELDS of that object, emit NO
     code edits at all — the spec alone suffices (the existing merge applies
     them). Make every hyperparameter `name` exactly such a field name.
   - Otherwise, edit the code so the values enter AT THE ENTRY and reach their
     use site: read each from the Map into a local at the top of the entry and
     thread it down (e.g. override a config object's fields right after it is
     constructed), or — for a constant read directly across modules — set a
     process-global `OnceLock<…>` at the top of the entry from the Map and read
     it via an accessor whose fallback equals the old constant. (Exactly one
     instance runs per process, so a process-global set once at the entry is
     safe.) Keep edits minimal and behaviour-preserving.

OUTPUT FORMAT (strict):
First, a single JSON object (you may wrap it in a ```json fence):
{{
  "hyperparameters": [
    {{"name": "learning_rate", "type": "float", "range": [0.0001, 0.1], "scale": "log", "default": 0.01}},
    {{"name": "n_restarts", "type": "int", "range": [1, 16], "scale": "linear", "default": 4}}
  ],
  "suggested_configs": [
    {{"learning_rate": 0.01, "n_restarts": 4}},
    {{"learning_rate": 0.03, "n_restarts": 8}}
  ]
}}
Every key used in a suggested config MUST be a declared hyperparameter `name`,
and every declared hyperparameter MUST appear in every suggested config.

THEN:
- If the spec alone is enough (the algorithm already applies the Map to those
  fields), output NOTHING after the JSON.
- Otherwise, on its own line emit exactly this separator:
{HYPERPARAM_EDITS_SEP}
  followed by one or more SEARCH/REPLACE blocks (across any files) in this
  format — the SEARCH text must be copied verbatim from the files shown and
  match exactly one place:

<<<<<<< SEARCH <relpath>
<original lines>
=======
<replacement lines>
>>>>>>> REPLACE

Do not output whole files or markdown fences after the separator — only blocks.{rust_rules}"""


def build_hyperparameter_user_prompt(
    files: dict, config: dict,
    parent_hyperparameters: dict | None = None,
    num_suggested_configs: int = 5,
) -> str:
    parts: list[str] = []
    parts.append("Current algorithm source files:\n")
    parts.append(_format_files_for_prompt(files))
    if parent_hyperparameters:
        parts.append(
            "\nThe parent algorithm in this trajectory was tuned. Its winning "
            "configuration is a map from track key to that track's best config "
            "(different tracks may have favoured different values):\n"
            f"```json\n{json.dumps(parent_hyperparameters, indent=2)}\n```\n"
            "Use it to inform your choice of hyperparameters, ranges, and "
            "suggested configs where the code is similar — paying attention to "
            "how the winning values varied across tracks — but only expose "
            "constants that actually exist in the algorithm above."
        )
    parts.append(
        f"\nPropose the hyperparameters and exactly {num_suggested_configs} "
        "suggested configs as JSON. If the algorithm already applies the Map to "
        "your chosen fields, output JSON only; otherwise add the separator and "
        "SEARCH/REPLACE blocks."
    )
    return "\n".join(parts)


_HYPERPARAM_SPEC_RELPATH = ".swarm/hyperparameters.json"


def build_hyperparameter_agentic_prompt(
    config: dict,
    parent_hyperparameters: dict | None = None,
    num_suggested_configs: int = 5,
) -> str:
    """Extraction task for the agentic backends (Fix 1).

    Unlike the API path (one structured completion parsed by
    `parse_hyperparameter_response`), the agent edits the algorithm file in its
    worktree directly and drops the spec as a JSON file we read back.
    """
    algo_path = config.get("algorithm_path", "the algorithm file")
    algo_dir = algo_path.rsplit("/", 1)[0] if "/" in algo_path else "the algorithm directory"
    parent_block = ""
    if parent_hyperparameters:
        parent_block = (
            "\nThe parent algorithm in this trajectory was tuned; its winning "
            "configuration is a per-track map (track key -> that track's best "
            f"config):\n{json.dumps(parent_hyperparameters)}\n"
            "Use it to inform your choices where the code is similar.\n"
        )
    return f"""\
This is a hyperparameter-extraction task, NOT a new optimization. Do not change \
the algorithm's logic.

Edit the algorithm's source (files under `{algo_dir}`; the entry is \
`{algo_path}`) so its most impactful magic-number constants become tunable \
hyperparameters read from the `hyperparameters: &Option<Map<String, Value>>` \
argument, each falling back to its CURRENT in-code value when the key is absent. \
The rewrite MUST be behaviour-preserving: with an empty or None map the \
algorithm must behave EXACTLY as it does now.

SCOPE RULE: the Map is a function argument, in scope ONLY at the entry \
(`solve_challenge` / the optimizer hooks) — you cannot read it from a \
module-level const. If the algorithm already threads the Map into a config (e.g. \
`Config::initialize(hyperparameters, …)`) and your chosen names are existing \
fields of that config, you need NO code edits — just write the spec. Otherwise \
inject at the entry and thread the values to their use sites (override a config \
object's fields after construction, or set a process-global `OnceLock` at the \
entry and read it via an accessor whose fallback equals the old const; one \
instance runs per process, so that is safe).

Choose 2-5 constants worth tuning (a learning rate, temperature, restart count, \
threshold, population size, …) — prefer fewer; a smaller search space is \
searched better.
{parent_block}
Then write the search specification to `{_HYPERPARAM_SPEC_RELPATH}` as JSON:
{{
  "hyperparameters": [
    {{"name": "learning_rate", "type": "float", "range": [0.0001, 0.1], "scale": "log", "default": 0.01}},
    {{"name": "n_restarts", "type": "int", "range": [1, 16], "scale": "linear", "default": 4}}
  ],
  "suggested_configs": [
    {{"learning_rate": 0.01, "n_restarts": 4}}
  ]
}}
Use "scale": "log" for multiplicative quantities (learning rates, temperatures), \
"linear" otherwise; "type" is "float", "int", or "categorical" (categorical uses \
"choices": [...] instead of "range"). Provide exactly {num_suggested_configs} \
suggested configs. Every key in a suggested config MUST be a declared \
hyperparameter name, and every declared hyperparameter MUST appear in every \
suggested config, with `default` equal to the constant's current in-code value.

Finally, run `cargo check` to confirm the edited code compiles. Only edit source \
files under `{algo_dir}` and write `{_HYPERPARAM_SPEC_RELPATH}`."""


def _strip_code_fence(text: str, lang: str = "") -> str:
    """Strip a leading/trailing markdown code fence if present."""
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _validate_hyperparameter_spec(spec: dict) -> str:
    """Return an error string if the spec is malformed, else ""."""
    hps = spec.get("hyperparameters")
    if not isinstance(hps, list) or not hps:
        return "spec.hyperparameters must be a non-empty list"
    names: set[str] = set()
    for hp in hps:
        if not isinstance(hp, dict) or "name" not in hp:
            return "each hyperparameter needs a 'name'"
        name = hp["name"]
        names.add(name)
        ptype = hp.get("type", "float")
        if ptype == "categorical":
            if not isinstance(hp.get("choices"), list) or not hp["choices"]:
                return f"categorical {name!r} needs a non-empty 'choices' list"
        else:
            rng = hp.get("range")
            if not (isinstance(rng, list) and len(rng) == 2):
                return f"{name!r} needs a [min, max] 'range'"
            if hp.get("scale") == "log" and rng[0] <= 0:
                return f"log-scaled {name!r} needs a positive range minimum"
    configs = spec.get("suggested_configs")
    if not isinstance(configs, list):
        return "spec.suggested_configs must be a list"
    for cfg in configs:
        if not isinstance(cfg, dict):
            return "each suggested config must be an object"
        for key in cfg:
            if key not in names:
                return f"suggested config uses unknown hyperparameter {key!r}"
    return ""


def parse_hyperparameter_response(response: str) -> dict:
    """Parse the extraction response into a spec + search/replace edit text.

    Returns {ok, error, hyperparameters, suggested_configs, edits_text}, where
    `edits_text` is the SEARCH/REPLACE block text after the separator (parsed by
    scripts/search_replace), or "" for the spec-only case (Case 0 — the
    algorithm already applies the Map to the chosen config fields).
    """
    fail = {
        "ok": False, "hyperparameters": [], "suggested_configs": [],
        "edits_text": "",
    }
    if HYPERPARAM_EDITS_SEP in response:
        json_part, _, edits_text = response.partition(HYPERPARAM_EDITS_SEP)
    else:
        json_part, edits_text = response, ""  # spec-only (no code edits)
    json_text = _strip_code_fence(json_part)
    # The JSON may have prose around it; grab the outermost {...}.
    brace = re.search(r"\{.*\}", json_text, re.DOTALL)
    if not brace:
        return {**fail, "error": "no JSON spec object found"}
    try:
        spec = json.loads(brace.group(0))
    except json.JSONDecodeError as e:
        return {**fail, "error": f"invalid JSON spec: {e}"}
    err = _validate_hyperparameter_spec(spec)
    if err:
        return {**fail, "error": err}
    return {
        "ok": True,
        "error": "",
        "hyperparameters": spec["hyperparameters"],
        "suggested_configs": spec.get("suggested_configs", []),
        "edits_text": edits_text.strip(),
    }


# ── Error recovery prompts ─────────────────────────────────────────


def build_runtime_fix_prompt(code: str, bench: dict, kernel_code: str = "", timeout: int = 30) -> str:
    errors = bench.get("errors") or []
    error_lines = "\n".join(f"  - {e}" for e in errors)
    score = bench.get("score", 0)
    feasible = bench.get("feasible", False)
    track_scores = bench.get("track_scores", {})
    track_summary = "\n".join(
        f"  - {track}: {s:.0f}" for track, s in track_scores.items()
    )
    parts = [f"Current algorithm (mod.rs):\n```rust\n{code}\n```\n"]
    if kernel_code:
        parts.append(f"Current CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```\n")
    parts.append(
        f"This code compiled successfully but failed at runtime.\n\n"
        f"Score: {score}  Feasible: {feasible}\n"
        f"Per-track scores:\n{track_summary}\n"
        f"Errors:\n{error_lines}\n\n"
        f"Per-instance time budget: {timeout} seconds. The solver is killed after this deadline.\n\n"
        "How to interpret the errors:\n"
        "- 'no solution saved' = the code crashed, panicked, or returned Err() "
        "before ever calling save_solution(), OR the solver ran out of time "
        f"without saving. Fix: use a time-based loop (std::time::Instant + deadline "
        f"at {timeout}s minus a few seconds margin) and call save_solution() EARLY "
        "with your first feasible solution, then keep improving and re-saving.\n"
        "- Any other error = the code saved a solution but the evaluator "
        "rejected it (constraint violation). Fix: check that your solution "
        "satisfies all feasibility constraints described in the challenge.\n\n"
        "Fix the runtime errors and return the complete source."
    )
    if kernel_code:
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nEnsure kernel function names match between mod.rs and kernels.cu."
        )
    return "\n".join(parts)


def distill_compiler_errors(raw: str, max_chars: int = 4000) -> str:
    """Reduce raw cargo/rustc output to just the actionable error diagnostics.

    The benchmark subprocess wraps the rustc output in swarm-config status
    lines, cargo progress, warnings, and a Python traceback. A weaker model
    fixes far better when handed only the `error[...]` blocks (with their
    file:line and source context) than the full noisy stream. Falls back to the
    raw tail if nothing rustc-shaped is found (e.g. an nvcc failure), so the
    caller never ends up with an empty error section.
    """
    if not raw:
        return raw
    # Drop the Python traceback the benchmark wrapper appends — it's about
    # benchmark.py, not the algorithm under repair.
    head = raw.split("\nTraceback (most recent call last):", 1)[0]
    # rustc prints each diagnostic as a blank-line-separated block. Keep a block
    # only when it opens an `error` (skip `warning:`/`note:` blocks and the
    # cargo progress/status preamble); always keep the `could not compile`
    # summary. Lines like `   |` are non-blank, so a block stays intact.
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in head.splitlines():
        if not ln.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append(cur)
    kept: list[str] = []
    for b in blocks:
        if b[0].startswith("error") or "could not compile" in b[0]:
            kept.extend(b)
            kept.append("")
    distilled = "\n".join(kept).strip()
    if not distilled:
        distilled = head.strip()[-max_chars:]
    return distilled[:max_chars]


def build_compile_fix_system_prompt(config: dict) -> str:
    """Focused system prompt for the compile-fix retry.

    Deliberately omits the challenge spec — making the file build is mechanical,
    not a design task — and instructs a MINIMAL edit, which weaker models handle
    far better than "return the complete rewritten file" (which tends to make
    them echo the broken code unchanged). Still carries the Rust guardrails so
    the fix doesn't re-introduce the dropped-import / external-crate failures.
    """
    is_gpu = bool(config.get("is_gpu"))
    rust_rules = _rust_rules_block(config)
    if is_gpu:
        fmt = (
            "Return BOTH complete files separated by a line containing exactly: "
            "// --- kernels.cu --- (the Rust file first). No markdown fences, no prose."
        )
    else:
        fmt = (
            "Return the COMPLETE corrected mod.rs. The very first character of your "
            "response MUST be `u` from `use super::*;`. No markdown fences, no prose."
        )
    return f"""\
You are fixing Rust compile errors so the file builds. The code is otherwise
intended to work — change ONLY what the listed errors require. Keep every other
line, every `use` import, and the provided harness entry-point signatures
(`solve_challenge`, or the `optimizer_*` hooks) unchanged.

{fmt}{rust_rules}"""


def build_compile_fix_prompt(
    code: str, kernel: str, compiler_errors: str, is_gpu: bool,
) -> str:
    errors = distill_compiler_errors(compiler_errors)
    parts = [f"This Rust file (mod.rs) failed to compile:\n```rust\n{code}\n```\n"]
    if kernel:
        parts.append(f"CUDA kernels (kernels.cu):\n```cuda\n{kernel}\n```\n")
    parts.append(
        f"The compiler reported these errors — fix EXACTLY these and nothing else:\n"
        f"```\n{errors}\n```\n\n"
        "Make the smallest change that resolves every error above. Use the "
        "file:line locations to find each problem. Do NOT rewrite the algorithm, "
        "rename items, drop imports, or alter the provided harness entry-point "
        "signatures (`solve_challenge`, or the `optimizer_*` hooks) — "
        "the rest of the file already works."
    )
    if is_gpu:
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nEnsure kernel function names match between mod.rs and kernels.cu."
        )
    return "\n".join(parts)


def build_redescribe_system_prompt(config: dict) -> str:
    """System prompt for the re-describe pass.

    The normal hypothesis prompt asks the LLM to propose ONE new change;
    here we instead ask it to characterize what the (post-fix) code does,
    so the system prompt has to match the user prompt's intent.
    """
    challenge = config.get("challenge", "unknown")
    tags = ", ".join(get_strategy_tags(config))
    return f"""\
You are reviewing a code change that was made for the "{challenge}" challenge.

Your job: produce an accurate post-hoc description of what the FINAL code does,
in the same 4-line format used for hypotheses. Do NOT propose new changes.

Respond in EXACTLY this format (4 lines, nothing else):

TITLE: <short title of what the final code does, under 80 chars>
DESCRIPTION: <2-3 sentence description of the approach in the final code>
STRATEGY_TAG: <one of: {tags}>
NOTES: <brief notes on how/whether the recovery changed the approach>"""


def build_redescribe_hypothesis_prompt(
    original_code: str, final_code: str, original_hypothesis: dict,
    *, original_kernel: str = "", final_kernel: str = "",
) -> str:
    orig_title = original_hypothesis.get("title", "")
    orig_desc = original_hypothesis.get("description", "")
    orig_tag = original_hypothesis.get("strategy_tag", "other")
    parts = [
        f"The original hypothesis was:\n"
        f"  TITLE: {orig_title}\n"
        f"  DESCRIPTION: {orig_desc}\n"
        f"  STRATEGY_TAG: {orig_tag}\n",
        f"Original code (before):\n```rust\n{original_code}\n```\n",
    ]
    if original_kernel:
        parts.append(f"Original CUDA kernels (before):\n```cuda\n{original_kernel}\n```\n")
    parts.append(f"Final code (after fixing runtime errors):\n```rust\n{final_code}\n```\n")
    if final_kernel:
        parts.append(f"Final CUDA kernels (after):\n```cuda\n{final_kernel}\n```\n")
    parts.append(
        "The code was modified to fix runtime errors. Compare the original "
        "hypothesis against the final code. If the error recovery changed the "
        "core approach (e.g. replaced the construction heuristic, added a "
        "fundamentally different fallback strategy, restructured the solver), "
        "update the TITLE, DESCRIPTION, and STRATEGY_TAG to accurately reflect "
        "what the final code actually does. If the fixes were minor (e.g. "
        "bounds checks, error handling wrappers) and the core approach is "
        "unchanged, keep the original hypothesis as-is.\n\n"
        "Respond with the corrected hypothesis."
    )
    return "\n".join(parts)


# ── Hypothesis parsing ─────────────────────────────────────────────


_META_DEFAULTS = {
    "title": "LLM mutation",
    "description": "Automated code improvement",
    "strategy_tag": "other",
    "notes": "",
}


def parse_hypothesis(text: str) -> dict:
    meta = dict(_META_DEFAULTS)
    # Tolerate a None/empty response (e.g. a provider that returned null
    # content) — fall back to the defaults instead of crashing on .strip().
    for line in (text or "").strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if key in meta and value:
                meta[key] = value
    return meta


# ── Agentic mode user prompt ───────────────────────────────────────


def build_agentic_user_prompt(
    state: dict, config: dict, *, role: str = "explorer",
    assigned_tag: str | None = None,
) -> str:
    """Per-iteration prompt for tooled (agentic) Claude Code.

    Stable rules — file scope, hypothesis.json schema, cargo allowlist,
    solver constraints — live in CLAUDE.md. This prompt is just the
    variable state the agent needs to decide what to try this iteration
    (role and niche are per-iteration server state, so they live here, not
    in CLAUDE.md).
    """
    parts: list[str] = []
    is_gpu = bool(config.get("is_gpu"))
    challenge = config.get("challenge", "unknown")

    my_score = state.get("current_trajectory_best")
    global_best = state.get("best_score")
    stagnation = state.get("my_runs_since_improvement", 0)
    runs = state.get("my_runs", 0)
    improvements = state.get("my_improvements", 0)

    parts.append(
        f"## Iteration context\n"
        f"- Challenge: {challenge}{' (GPU)' if is_gpu else ''}\n"
        f"- Your best score: {my_score}\n"
        f"- Global best score: {global_best}\n"
        f"- Runs / improvements / stagnation: {runs} / {improvements} / {stagnation}"
    )

    if role == "exploiter":
        parts.append(
            "\n## Role — exploiter (localized edit only)\n"
            "Make ONE small, localized change to the existing algorithm. "
            "Preserve its structure, control flow, and function set — do NOT "
            "rewrite or restructure. If you'd touch more than ~15% of the "
            "lines, narrow the change."
        )
    else:
        parts.append(
            "\n## Role — explorer\n"
            "Bias toward novel, structurally-different strategies the swarm "
            "hasn't tried; ambitious rewrites are welcome if they can leapfrog "
            "the current best."
        )

    niche = _niche_nudge(role, assigned_tag)
    if niche:
        parts.append("\n## Assigned niche (suggestion)\n" + niche)

    reset = state.get("trajectory_reset")
    if reset:
        rtype = reset.get("type", "")
        if rtype == "adopted_inactive":
            parts.append(
                "\n## Trajectory reset\n"
                "You stagnated and the server handed you another agent's "
                "previously-active algorithm as your new starting point. "
                "Study it (already on disk) and propose an improvement that "
                "builds on its approach."
            )
        else:
            parts.append(
                "\n## Trajectory reset (fresh start)\n"
                "You stagnated and have been reset to the template algorithm. "
                "Propose an initial strategy from scratch."
            )

    # Exploiters are guarded out of the stub path by the driver, but gate the
    # bootstrap block on role too so a stray stub never tells them to rewrite.
    bootstrap = is_stub_code(state.get("best_algorithm_code") or "") and role != "exploiter"
    if bootstrap and _is_optimizer_hook_challenge(config):
        parts.append(
            "\n## Bootstrap iteration\n"
            "The algorithm on disk is a stub. `solve_challenge` and the training "
            "loop are harness-owned and NOT in this file — do not add them. Write "
            "complete implementations of the optimizer hooks (`optimizer_init_state`, "
            "`optimizer_query_at_params`, `optimizer_step`) from scratch, keeping them "
            "`pub fn` with their exact signatures. Read the stub first to lock in those "
            "signatures before writing anything."
        )
    elif bootstrap:
        parts.append(
            "\n## Bootstrap iteration\n"
            "The algorithm on disk is a stub (`unimplemented!()`). Write a "
            "complete `solve_challenge` implementation from scratch — don't "
            "tweak, replace. Read the stub first to lock in the exact "
            "`solve_challenge` signature (parameter names and types) before "
            "writing anything."
        )

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = [
            "\n## Already tried against this exact code (avoid repeating)",
        ]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            lines.append(f"  - [{tag}] {title}")
        parts.append("\n".join(lines))

    hint = state.get("stagnation_hint")
    if hint == "inspiration":
        insp = state.get("inspiration_code", "")
        insp_agent = state.get("inspiration_agent_name", "another agent")
        if insp:
            block = [
                f"\n## Stagnation hint — inspiration from {insp_agent}",
                "Study this peer's current best for *structural* ideas to "
                "adapt. Do NOT copy it wholesale — your job is to evolve "
                "your own lineage, not replace it.",
                "",
                "```rust",
                insp,
                "```",
            ]
            if is_gpu:
                insp_kernel = state.get("inspiration_kernel_code", "")
                if insp_kernel:
                    block += [
                        "",
                        f"Peer CUDA kernels:",
                        "```cuda",
                        insp_kernel,
                        "```",
                    ]
            parts.append("\n".join(block))
    elif hint == "tacit_knowledge":
        parts.append(
            "\n## Stagnation hint — tacit knowledge\n"
            "Check `tacit_knowledge_personal.md` in this worktree (if "
            "present) for strategy hints the contributor wrote down. If "
            "absent, fall back to the inspiration_code block above if any."
        )

    stagnation_limit = int(config.get("stagnation_limit") or 0)
    in_band_distill = (
        _tacit_write_enabled(config)
        and not DRIVER_DISTILL_FOR_AGENTIC
        and stagnation_limit >= 3
        and stagnation == stagnation_limit - 1
    )
    if in_band_distill:
        parts.append(
            "\n## Tacit-knowledge contribution\n"
            "Trigger: this is your LAST iteration before the server resets "
            "your trajectory (you're at `stagnation_limit - 1`). Before "
            "you stop, look back over the attempts in `prior_hypotheses` "
            "above and ask: is there a *generalisable* lesson in what "
            "HASN'T worked? If yes, append ONE short bullet to "
            "`tacit_knowledge_personal.md` (create the file if missing) "
            "under the `## Strategies` heading, prefixed with `- LLM:`.\n"
            "Rules:\n"
            "- Focus on failure: what looked promising and didn't pay off, "
            "or what diagnostic told you a direction was a dead end.\n"
            "- Abstract away from this specific challenge — write it so it "
            "would help a future agent on a *different* optimisation "
            "problem. Good: \"large-neighborhood search underperforms when "
            "the feasible region is narrow.\" Bad: \"tabu length 12 lost "
            "to length 8 on this instance.\"\n"
            "- Under 30 words. No code, no scores, no instance IDs.\n"
            "- Skip silently if nothing genuinely new and transferable has "
            "emerged since the last `- LLM:` entry already in the file."
        )

    parts.append(
        "\n## Your task\n"
        "1. Decide on ONE specific improvement.\n"
        "2. Edit the algorithm file in place to implement it.\n"
        "3. Run `cargo check --features solver," + challenge + "` to confirm it compiles.\n"
        "4. Write `.swarm/hypothesis.json` describing what you did.\n"
        "5. Stop.\n\n"
        "Do not run `scripts/benchmark.py` — the driver will run the "
        "official benchmark after you stop."
    )
    return "\n".join(parts)
