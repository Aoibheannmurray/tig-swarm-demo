# Algorithm-format ↔ mainnet parity plan

## Goal

Make swarm algorithms **byte-identical in format** to TIG mainnet algorithms, so a
single algorithm file compiles unchanged in **both**:

1. the swarm's own build (`cargo --features solver,<ch>`), and
2. the tig-docker slot used by the `tig` benchmark backend — local **and** C3
   (the baked `tig-bench-<ch>` images).

Concretely: drop `use super::*;` from every algorithm/seed, adopt the mainnet
import + `help()` + `solve_challenge` shape, and **retire the reshape/prompt swap
logic** that currently rewrites between the two formats. After this, mainnet
algorithms can be dropped into the swarm (and vice-versa) with zero munging.

## Why this is safe — the enabler is already in place

`src/lib.rs` already provides everything the mainnet import line needs:

- `extern crate self as tig_challenges;` → `use tig_challenges::<ch>::*;` resolves
  inside the swarm crate (to `crate::<ch>`), and to the real challenge crate inside
  tig-algorithms. Same line, both places.
- crate-root `pub fn seeded_hasher`, `pub type HashMap`, `pub type HashSet`,
  `QUALITY_PRECISION` — the same symbols the mainnet template imports via
  `use crate::{seeded_hasher, HashMap, HashSet};`.

The entry contract is **already shared**: swarm algorithms already use
`solve_challenge(challenge, save_solution: &dyn Fn(&Solution)->Result<()>, hp)` and
call `save_solution(&Solution{..})`. `src/main_solver.rs` invokes
`challenges::<ch>::algorithm::solve_challenge(..)`. So only two things diverge:
the **import line** (`super::*` vs `tig_challenges::<ch>::*` + `crate::{..}`) and a
missing **`pub fn help()`**.

## Canonical format (from the baked image's `template.rs`)

```rust
use crate::{seeded_hasher, HashMap, HashSet};   // ONLY the symbols actually used
use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};             // only if Hyperparameters is defined
use serde_json::{Map, Value};
use tig_challenges::<ch>::*;

pub struct Hyperparameters { /* optional */ }

pub fn help() { println!("..."); }

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> { /* unchanged logic */ }
```

## Scope

**In scope (7 `solve_challenge` challenges):** satisfiability, vehicle_routing,
knapsack, job_scheduling, energy_arbitrage (CPU) + vector_search, hypergraph (GPU).

**Separate track (Phase 6):** neuralnet_optimizer — `solve_challenge` + training
loop are harness-owned; the agent implements `optimizer_init_state` /
`optimizer_query_at_params`. "Identical to mainnet" here means matching mainnet's
*optimizer-hook* format, a different exercise. Keep the C3 seed-blinding +
boilerplate (anti-cheat, format-independent).

**Explicitly excluded (NOT algorithms — leave their `use super::*` alone):**
`src/energy_arbitrage/mod.rs`, `src/vehicle_routing/challenge.rs`,
`src/vehicle_routing/solution.rs` — challenge-definition internals; their
`super::*` is ordinary module usage.

## Phases

### Phase 0 — Parity audit (no code changes)
- Per in-scope challenge, diff the swarm `src/<ch>/mod.rs` public challenge API
  (`Challenge`/`Solution` fields + methods) against the monorepo
  `tig-challenges/src/<ch>/mod.rs` (extract from the baked images or the CI
  monorepo checkout). Confirm the public surface matches so an identical algorithm
  compiles unchanged; record any rename/missing-method as a **blocker**.
- Determine the crate's unused-import lint level (`cargo check` warn vs deny) to
  decide "import only what's used" vs "full block".
- Deliverable: a per-challenge parity table (OK / gap).

### Phase 1 — Rewrite in-scope algorithm files (`src/<ch>/algorithm/mod.rs`)
Checked-in today with `super::*`: satisfiability, knapsack, job_scheduling,
energy_arbitrage. (vehicle_routing/vector_search/hypergraph `algorithm/mod.rs` are
gitignored → handled via seeds in Phase 2.)
Per file: (1) replace `use super::*;` with `use tig_challenges::<ch>::*;` plus
`use crate::{seeded_hasher, HashMap, HashSet};` for whatever is actually used;
(2) add `pub fn help() {}` if absent (knapsack, job_scheduling, energy_arbitrage);
(3) leave logic untouched.
**Verify each** before moving on: `cargo check --features solver,<ch>` (swarm) AND
inject into the baked tig image → `build_algorithm swarm_algo` (tig slot). Both
must compile; run one nonce to confirm feasible.

### Phase 2 — Rewrite fallback seeds (`initial_algorithms/*/seeds/*.rs`)
`super::*` today: satisfiability/local_search, vehicle_routing/construction,
job_scheduling/greedy, energy_arbitrage/greedy. (knapsack/hypergraph/vector_search
seeds already conform — verify.) Same transform + `help()`. These are what agents
start from, so they must be tig-ready. Verify each in both builds.

### Phase 3 — Prompts & generated guides (stop teaching `super::*`)
- `scripts/prompts.py`: the "Keep `use super::*;` intact" directive (~line 853) →
  "Keep the `use tig_challenges::<ch>::*;` import, the `solve_challenge` signature,
  and `help()`." Reconcile with the already-correct lines 357-372 into one
  consistent instruction. Remove any remaining `super::*` guidance.
- `scripts/agentic_backends.py` (350, 566): already says `tig_challenges::` —
  expand the generated worktree guide to show the **full** mainnet template
  (imports + `help()` + signature) so agents author identically.
- Grep all of `scripts/` and any prompt/template `.md` for `super::*` and update.

### Phase 4 — Retire the reshape/swap logic
- `scripts/challenge_files.py`: delete `_add_super_star_anchor` (re-inserts
  `use super::*;`). Audit `reshape_mainnet_for_swarm` and remove every swarm-ward
  rewrite made unnecessary; keep only still-needed bits (multi-file packaging,
  kernel handling, and neuralnet's structural relocation until Phase 6).
- `setup.py` `seed_inactive_pool_from_mainnet`: for the 7 in-scope challenges,
  seeding is now pass-through (no reshape). Simplify; keep reshape only for
  neuralnet. Cross-check [[mainnet_inactive_seeding]].
- Confirm **no** staging-time import swap was ever added to `c3_compute.py` /
  `benchmark.py` (we chose source-fix over swap — keep it that way).

### Phase 5 — Verification matrix + regression guard
For each in-scope challenge: (a) swarm custom benchmark compiles + runs → feasible;
(b) tig backend on the baked image compiles + runs → feasible. CPU baked images
exist; GPU built locally or via CI.
Add `scripts/test_algorithm_format.py` (self-running, repo convention): asserts no
`use super::*;` in `src/<ch>/algorithm/mod.rs` + `initial_algorithms/*/seeds/*.rs`
and that `pub fn help(` is present. Run the existing `test_*.py` suites.

### Phase 6 — neuralnet parity (separate)
Align `src/neuralnet_optimizer/{mod.rs, algorithm/mod.rs}` imports with mainnet
(drop `super::*`, use `tig_challenges::neuralnet_optimizer::*` + `crate::*`) and
match the mainnet optimizer-hook signatures. Preserve C3 seed-blinding + boilerplate.
Regression-check [[neuralnet_test_leak]] and [[frozen_layer_check_regression]].

## Risks / watch-items
- **Unused-import lint**: if the crate denies warnings, the full import block breaks
  on unused symbols → import minimally per file (Phase 0 decides).
- **`super::*` pulled swarm-internal helpers** (baselines, private fns) some
  algorithms/seeds may rely on → per-file compile catches it; add explicit
  `use crate::…` as needed.
- **GPU kernels**: vector_search/hypergraph ship `kernels.cu`; ensure the import
  change doesn't disturb the `.cu` / `build_ptx` path.
- **Challenge API drift**: any swarm-vs-monorepo type gap found in Phase 0 is a real
  blocker needing challenge-definition alignment (larger).

## Rollout
Phase 0 → 1 → 2 (verify each challenge end-to-end before the next) → 3 → 4 → 5 → 6.
Commit per challenge (repo's component-commit habit). The separately-requested
**3 GPU baked images** slot into Phase 5's GPU verification.
