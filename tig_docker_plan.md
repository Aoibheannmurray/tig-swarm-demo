# Plan: Benchmark swarm algorithms with the TIG docker

## Goal

Keep `tig-swarm-demo` a separate repo, but benchmark the algorithms its agents
write using the **TIG docker** — so we get TIG-authentic compilation (the LLVM
fuel/runtime-signature passes) and **fuel-based** scoring instead of the current
wall-clock timeout. The monorepo is a **benchmarking backend** the swarm shells
into; nothing about the swarm's orchestration, server, or agent loop moves into it.

## Why fuel (the reason this is worth doing)

Fuel is **instruction-counted, not wall-clock** — deterministic and
hardware-independent. Same pinned build → same fuel number on any machine. Unlike
today's timeout (where a fast machine and a slow machine disagree), fuel makes
swarm scores directly comparable across contributors — **but only if everyone runs
the identical pinned build**. So strict version pinning is correctness, not just
convenience.

## Integration boundary

Inside the container, benchmarking one algorithm is just:

```bash
CHALLENGE=<challenge> build_algorithm <algo>                 # compile → fuel-instrumented .so/.ptx
CHALLENGE=<challenge> modified_test_algorithm <algo> <track> <hyperparams> --output-json
```

The only coupling is the **algorithm contract**: a Rust file at
`tig-algorithms/src/<challenge>/<algo>/mod.rs`, declared in the challenge's
`mod.rs`, exposing `solve_challenge(challenge, save_solution, &hyperparameters)`.
Agents must write to this contract — a template change, not a repo merge.

## `modified_test_algorithm`

A copy of the monorepo's `scripts/test_algorithm` with **additive** changes only
(do not modify the original). The stock script already runs `tig-runtime` →
`tig-verifier` per nonce in parallel and captures **quality**; it just discards
fuel. Changes:

1. **Capture fuel** — read `fuel_consumed` from the `tig-runtime` `{nonce}.json`
   output (already written to the temp dir).
2. **Capture the runtime exit code** → decode to a `failure_reason` (`87` out of
   fuel · `85` no solution · `86` invalid · `84` runtime error/panic · `82/83` OOM).
3. **`--output-json`** — emit the per-nonce record + cheap per-track aggregates.

**Per-nonce record (finalized):**

```json
{ "quality": <int|null>, "fuel_consumed": <int>, "fuel_limit": 5000000000000,
  "feasible": <bool>, "failure_reason": <string|null> }
```

- `quality` is `null` when not feasible (the swarm decides scoring policy later).
- `fuel_limit` = the challenge's `max_fuel_budget` = **5_000_000_000_000 (5e12)**,
  passed as `--fuel`. Fuel ratio (`fuel_consumed / fuel_limit`) is derived by the
  swarm, not baked in.
- `failure_reason` is the decoded runtime exit code (null when feasible).

**Per-track aggregates:** solve rate, quality stats over solved, fuel-ratio stats
(incl. **max** — a nonce near the cap signals fragility), failure-reason histogram.

`modified_test_algorithm` emits **raw factual records only — no scoring policy**;
the swarm's adapter maps them into `benchmark.json` (`score` / `track_scores` /
`feasible`), so the server / UI / publish flow stays untouched. `test_algorithm`
takes **one track per invocation**, so the swarm loops over tracks and assembles
`track_scores`. Lives in the **swarm repo**, baked into the image (below). Its
parser depends on the TIG runtime/verifier output format → a compatibility surface
to re-test on every version bump.

## Source delivery: custom image (Option B)

The standard TIG `dev` image (`ghcr.io/tig-foundation/tig-monorepo/<challenge>/dev`)
bakes in the toolchain, the LLVM fuel plugin, the build scripts, and prebuilt
`tig-runtime`/`tig-verifier` — but **deletes the source crates** after building
them (`WORKDIR /app` is empty). Compiling a *new* algorithm re-links `tig-binary`,
so the source (`tig-binary`, `tig-challenges`, `tig-algorithms`, manifests) must be
present. The standard image alone cannot build algorithms.

**Decision: bake the source into a custom image (Option B), used both locally and
on C3.** This is driven by C3: C3 runs jobs from a registry image plus a small
uploaded workspace, and **each C3 job is a fresh container with no persistent cargo
cache**. The alternative (mount/upload the source per job) would re-upload the
source and cold-compile all dependencies on every job. Baking solves both, and
using one image everywhere keeps local and C3 identical.

The custom image:

```dockerfile
FROM ghcr.io/tig-foundation/tig-monorepo/<challenge>/dev:<version>
COPY <pinned tig source> /app                    # add back what the base stripped
RUN <warm build>                                 # pre-compile deps (incl. std) into the cargo cache
COPY modified_test_algorithm /usr/local/bin/tig-scripts/
```

It inherits everything the base provides and adds the **source** + **pre-compiled
dependencies** + **`modified_test_algorithm`**.

**Stable vs volatile** — only the volatile half changes per algorithm:

| | Changes when | Where it lives |
|---|---|---|
| Stable: TIG crates, toolchain, compiled deps | TIG version bump only | baked in the image |
| Volatile: the agent's `mod.rs` | every iteration | injected at run time |

**Fixed algorithm slot** — always use one module name/path (e.g.
`tig-algorithms/src/<challenge>/swarm_algo/mod.rs`), declared in `mod.rs` inside the
image. Each iteration just overwrites the file contents — no `mod.rs` edits per run.

## How compilation works

- **Image-build time (once per TIG version):** the heavy compile — std (`-Z
  build-std`), `tig-challenges`, external crates — cached inside the image.
- **Run time (per algorithm):** `build_algorithm` recompiles only the changed
  file + the two crates wrapping it (`tig-algorithms`, `tig-binary`) and runs the
  fuel-injection link (`opt` → `llc` → `clang` → `.so`). Everything beneath is
  cached → fast.

## Build / pull / run counts

| Action | How often |
|---|---|
| **Build** the image | Once per TIG version × challenge × arch, in CI |
| **Pull** the image | Once per machine/worker per version, then cached (local **and** C3) |
| **Run** a benchmark | Every algorithm — no build, no pull |

Benchmarking location doesn't change the build count: the image is built once in
CI; local and C3 both just pull and cache the same artifact.

## Build matrix

Design it **challenge-agnostic and lane-agnostic** — one parameterized
`Dockerfile.bench` and CI matrix that works for any challenge, CPU or GPU. It
generalizes naturally because the custom image is `FROM` the challenge's TIG dev
image, so it **inherits the correct base** (ubuntu for CPU challenges, CUDA for GPU
challenges) automatically — we don't manage bases ourselves.

Matrix dimension = **challenge × arch**; the CPU/GPU split only affects two things:

- **Warm-build step**: GPU challenges (vector_search, hypergraph,
  neuralnet_optimizer) also pre-build the `.ptx` (`build_ptx`, needs `nvcc` — present
  in their CUDA-based dev image). CPU challenges just warm `build_so`.
- **Run-time hardware**: GPU challenges need a real GPU to run/verify — local NVIDIA
  GPU, or a GPU instance on C3 (`c3_hardware`). CPU challenges don't.

**Arch: amd64 only for now.** arm64 is just another matrix entry to add later — no
design change, since the Dockerfile and CI are already parameterized.

### Image size (measured — knapsack CPU, 0.0.6)

| Component | On-disk | Compressed (pull) |
|---|---|---|
| TIG `dev` base (`FROM`) | ~8.6 GB | **4.0 GB** |
| Warm cache delta (B adds: `target/` 810 MB + cargo registry 74 MB) | ~0.9 GB | ~0.4–0.5 GB |
| TIG source | 37 MB | small |
| **Option B CPU image** | **~9.5 GB** | **~4.5 GB** |

GPU lane (vector_search) base is ~8.7 GB compressed → expect ~9 GB compressed /
~19–20 GB on-disk (CUDA `devel` base dominates). All well within normal registry
handling; C3's per-worker cache absorbs the one-time pull. **C3 confirmed: handles
these sizes comfortably.**

**Findings:** (1) size is dominated by the `dev` base (toolchain + LLVM plugin +
`build-std`; CUDA for GPU), **not** the warm cache — which is only ~10% on CPU. For
reference the old `tig-swarm-cpu` image is 1.76 GB, so TIG docker is ~5× larger,
intrinsic to TIG's toolchain. (2) Build timing: **cold ~1m40s, incremental ~34s** —
so the warm cache saves ~1 min/job (modest; the real B wins are no per-job source
upload on C3 + one reproducible artifact). Keep the warm cache: it's cheap relative
to the base, and dropping it wouldn't meaningfully shrink the image.

## Registry

A hosted server storing images; both local and C3 pull from it over the network.

Registry = GitHub Container Registry (`ghcr.io`). The image is **public** in both
phases below (no swarm/agent IP in it — algorithms are injected at run time; base +
source are already public from TIG). Never bake secrets/API keys in.

> ⚠️ **C3 constraint (discovered by testing): C3 only supports Docker Hub images,
> not `ghcr.io`** (`c3 deploy` → "only Docker Hub images are supported; got registry
> ghcr.io"). So **C3-targeted images must live on Docker Hub** (`docker.io/<user>/…`);
> local docker can still use ghcr. **✅ VALIDATED on C3:** mirrored the amd64 TIG dev
> image to `docker.io/danieltiagoadams/tig-dev-knapsack:0.0.6` (mirroring is
> pull→tag→push — no execution, so cross-arch is fine from an arm64 host) and a
> knapsack benchmark ran on a C3 l40 instance → real fuel + positive quality →
> `benchmark.json`. C3 pulled the repo anonymously, so a pushed repo is **public** by
> default. For **production** the warm-cache *custom* image still wants an **amd64 CI
> build → Docker Hub** (GitHub Actions; the dev-image-mirror + upload-source path used
> here is the validation/Option-A fallback, cold-compiling per job).

**Development context:** this work lives on a **local git branch** on the
developer's machine — not pushed to GitHub. So there is **no GitHub Actions CI** yet:
building the image, pushing it, and bumping the version pin are all done **manually
and locally**. The CI machinery described elsewhere in this plan (build-and-push,
Flavor 2 bump, validation gate) applies only **if/when this is pushed to GitHub and
upstreamed**.

**Where it lives — now (local branch):**

- **Local benchmarking:** `docker build` the image on your machine; it's used
  straight from the local docker daemon. **No registry, no push needed.**
- **C3 benchmarking:** C3 can't see your local docker, so the image must be pushed
  to a registry C3 can reach. Build locally, then `docker push` to **your personal
  ghcr namespace** authenticated with a **Personal Access Token** (`write:packages`),
  and mark it public once:

  ```
  ghcr.io/<your-handle>/tig-custom-image/<challenge>:<version>
  ```

  No fork, no shared-repo CI, no owner/admin permissions — you own the namespace.

**Where it lives — if/when upstreamed:** push the branch to GitHub and let CI take
over — re-scope the image to the project org (`ghcr.io/<repo-owner>/tig-custom-image/...`),
published via the shared repo's `GITHUB_TOKEN`. A one-time owner/admin step then
applies (enable Actions package publishing + mark the package public), since the
maintainer is a contributor, not the repo creator.

**Visibility: public.** No swarm/agent IP is in the image (algorithms are injected
at run time, never baked); the base + source are already public from TIG. Public
means zero credential management — C3 and every contributor pull with no auth setup.
(Never bake secrets/API keys into the image regardless.) C3 auto-caches pulls per
worker.

## Per-algorithm flow

1. Agent writes the algorithm.
2. Start a container from the prebuilt image (local `docker run`; C3 pulls it).
3. **Inject the one file** into the fixed slot — local: mount/copy the single file;
   C3: tiny uploaded workspace, runner copies it in.
4. `build_algorithm <algo>` (fast — deps cached).
5. `modified_test_algorithm <algo> <track> --output-json` per track.
6. Adapter reshapes JSON → `benchmark.json`.
7. Container discarded.

Steps 3–7 are all that repeat; no image work in the loop.

## Version pin (single source of truth)

One pinned config in the swarm repo, **pinned by digest** so a tag can't change
content under us:

```yaml
tig_version: "0.0.6"
base_image_digests:
  knapsack/amd64: "sha256:..."
  ...
```

The custom image is always built from this pin, locking base image + source + build
as one unit. Changing the pin is the only way the environment moves. Contributors
get the current pin via `git pull`; the matching image already exists in the
registry (CI built it on merge), so their next run just pulls it.

## Keeping current — manual trigger (Flavor 2)

> Applies to the **upstreamed (GitHub) phase**. While the branch is local, updating
> is manual: bump the pin and rebuild the image locally by hand.

No auto-detection cron. The **maintainer decides when** to adopt and triggers a
one-button `workflow_dispatch` action with the target version as input. The action:

1. Resolves the digests for that version (`crane digest .../dev:<version>`).
2. Edits the pin file (`yq`).
3. Opens a PR (`peter-evans/create-pull-request`, branch `bump/tig-<version>`).

The maintainer supplies the decision + version number; the action supplies the
digests, pin edit, and PR. (Adoption cadence is the maintainer's call; PRs can be
frequent.)

## Validation gate (runs on the bump PR)

Builds the candidate image and checks the surfaces a TIG bump can break:

- **Compiles** — a known-good reference algorithm still `build_algorithm`s.
- **Output parses** — `modified_test_algorithm` still parses runtime/verifier
  output.
- **Valid + fuel recorded** — reference produces a valid solution + a fuel number.
  If reference **fuel changes**, flag loudly: scores recalibrate (a decision, not a
  silent event).

On merge, the build-and-push workflow ships the new image(s).

## Contributor footprint

Contributors still just `git pull tig-swarm-demo` and run. They never clone the
monorepo, never build an image, and keep no loose TIG source on disk — the source
lives inside the pulled image. First run pulls + caches the image; later runs reuse
it.

## Fuel limit

`max_fuel_budget` (per-challenge, on-chain protocol config; `tig-structs/src/config.rs:90`)
is the real `--fuel` value — the `2e9`/`100e9` defaults in `tig-runtime`/`test_algorithm`
are placeholders. **Current value: 5_000_000_000_000 (5e12).** Hardcode in the swarm
config for v1; revisit if protocol config changes (ideally fetch it if an API exists).

## Scoring policy (v1)

How the swarm turns `modified_test_algorithm`'s per-nonce records into a score.
**Keep it simple: per-track score = median of per-nonce quality.**

- **Per-track:** `median` of the per-nonce qualities (replaces today's arithmetic
  mean at `scripts/benchmark.py:1245` — `statistics.median(scores)`).
- **Infeasible nonces:** counted in the median at the infeasible floor
  (`INFEASIBLE_QUALITY = -10M`), as the mean does today. Gives median a
  majority-feasible property. (Barely affects the "best" path: a run is only
  `feasible`/best-eligible with **zero** infeasible nonces, so best candidates'
  medians are over real qualities anyway.)
- **Overall `score`:** unchanged — existing shifted geometric mean across tracks,
  now fed the medians (monotonic combiner, no rework).
- **`feasible` flag, infeasible floor, `benchmark.json` shape:** unchanged. Server
  doesn't re-aggregate, so nothing downstream breaks.
- **Downstream (intended):** `track_scores` feeds `hpo.py` per-track winners + agent
  feedback/dashboard; these now reflect medians (outlier-robust) instead of means.

**Calibration wrinkle (revisit, not blocking):** `tig-verifier` emits *absolute*
quality, but the current pipeline assumes *baseline-relative, ±10M-clamped* quality
(the clamp / geomean-shift / floor constants are tuned to that scale, and the
swarm's own evaluator/normalization is replaced by `tig-verifier`). v1: feed raw TIG
quality into the median and ensure the infeasible floor sits clearly below the real
TIG quality range so feasible always beats infeasible. Recalibrate only if rankings
look off. (Independent of the median choice.)

## Algorithm-contract conformance

**Goal: make the swarm TIG-native** — algorithms are authored and benchmarked
against TIG's contract, so "writes algorithms that work in TIG" is literally true.

- **D1 — Replace (phased).** The TIG-docker path *replaces* the swarm's custom
  benchmarking. Drop the swarm's generator, evaluator, and local `Challenge`/
  `Solution` types. Build behind the existing `compute` switch, validate parity,
  then retire the custom path + `benchmark.py`'s inner build/run logic. (Parity =
  same feasibility + sane ranking; absolute scores *won't* match — TIG quality is
  absolute vs the old baseline-relative. See Scoring policy.)
- **D2 — Types from `tig_challenges`.** Depend on the pinned `tig_challenges` crate
  (one definition, no drift; can reuse TIG's `generate_instance`/`evaluate_solution`).
  **Consequence:** the swarm's dev build (`cargo check --features solver,<challenge>`)
  now needs the pinned TIG crates fetchable — the pinned-source coupling extends to
  local development, not just docker.
- **Signature status:** 7/8 challenges already match `solve_challenge` verbatim
  (CPU 5 + GPU vector_search & hypergraph, incl. `Result<Option<Solution>>` and the
  module/stream/prop params). Structs are field-identical modulo cfg-gated
  *verification-baseline* fields (`greedy_baseline_*`) the solver never reads.
- **neuralnet_optimizer:** *same hook pattern as TIG.* TIG's `solve_challenge` is
  boilerplate calling a provided `training_loop(...)` with three optimizer hooks
  (`optimizer_init_state` / `query_at_params` / `step`); the agent writes the hooks
  + `OptimizerState`. Conform = inject `[TIG boilerplate solve_challenge] +
  [OptimizerState] + [agent's 3 hooks]`. The swarm's extra Solution fields
  (`train_losses`/`validation_losses`) drop out — TIG's `training_loop` builds TIG's
  Solution.

- **Anti-cheat / data hiding (D3 — resolved).** Real TIG catches cheating with
  **downstream checks on submitted algorithms**; the swarm has no such checks, so it
  must prevent cheating *structurally at benchmark time*. We do **not** change how
  instances are generated. Two vectors:
  - *Reading hidden answer fields* (verification baselines, `hidden_seed`) — affects
    all challenges. **Closed for free at compile time:** the TIG algorithm build
    already enables `hide_verification` (`tig-binary/Cargo.toml:19-20` →
    `tig-challenges` with `hide_verification`), making those fields inaccessible. So
    the 7 mechanical challenges can author the full `solve_challenge` safely.
  - *Regenerating/accessing the dataset* (neuralnet) — **`hide_verification` does NOT
    cover this.** The test split is fully public on `challenge.dataset` (pub fields +
    pub `test_*()` accessors, un-gated), `challenge.seed` is public, and
    `generate_instance` is deterministic + open. Worse, `training_loop` passes the
    *real* `challenge.seed` to `optimizer_init_state`
    (`tig-challenges/.../neuralnet_optimizer/mod.rs:376`). So even hooks-only authoring
    isn't enough on its own: the seed is the one reconstruction input a hook still gets.

    **Mitigation = hooks-only authoring + blind the seed.** The hooks already never
    receive `&Challenge`/`Dataset` (only init gets `seed`). The swarm passes a
    **derived seed** (`hash(seed,"optimizer")`) to `optimizer_init_state` instead of
    the real one — a legit optimizer only needs RNG and doesn't care; a cheater loses
    its only path to regenerate the data. This is **not** a generation change.

    **Why it coexists with mounting the agent's code.** The seed is handed over by
    `training_loop`, which the agent does **not** write — the hooks-only restructure
    makes `solve_challenge` + `training_loop` swarm-owned. Benchmark-time layering:
    | Layer | Owner | Where |
    |---|---|---|
    | 3 hooks + `OptimizerState` | agent | **injected/mounted** at run time |
    | `solve_challenge` + blinded `training_loop` | swarm | **baked into the image** |
    | `tig_challenges` types, fuel injection, runtime/verifier | TIG | baked |

    The agent's hooks still mount into the TIG slot and compile against TIG's
    everything; only the harness that feeds them differs (calls the swarm's blinded
    `training_loop`, not `tig_challenges`'). The agent can't bypass it (can't edit the
    loop; the real seed exists nowhere in its inputs). The hooks remain a **byte-valid
    TIG submission** — in real TIG they'd run with the real seed under TIG's downstream
    checks; blinding only protects the swarm's own leaderboard.

    *Caveat:* the swarm's `training_loop` needs the pieces TIG's uses (`MLP`, kernels,
    dataset accessors). If `tig_challenges` exposes them → vendor a one-line-changed
    copy; if some are private → use a cargo `[patch]` to a pinned swarm fork of
    `tig-challenges` with that one line changed (re-check on version bumps).
  - *Telemetry (resolved):* the neuralnet loss curves fed the **dashboard only**
    (not agent feedback — feedback surfaces only `best_track_scores`). Decision:
    **drop them.** Dashboard updated — the "TRAINING LOSS" chart is removed from
    `dashboard/src/challenges/neuralnet_optimizer.ts` (arch diagram + stat bar kept).
    The extra Solution fields (`train_losses`/`validation_losses`) are dropped; the
    Rust/`benchmark.py` production goes away naturally in the migration (TIG's
    `training_loop` doesn't emit them). Dead `.nn-loss-*`/`.nn-side` CSS in
    `style.css` stripped. Other challenges' `viz_data` is reconstructable in the
    adapter from `tig-runtime`'s solution output, so no loss there.
- **D4 — authoring surface (mechanical).** Each `src/<challenge>` becomes TIG's
  `template.rs` form (imports `tig_challenges`); agent prompts describe TIG's
  `Challenge` fields/helpers + the `solve_challenge`/hook contract. Touches
  `prompts.py` / `agentic_backends.py`.

**Phasing:** (1) add `tig_challenges` dep, convert one CPU challenge; (2) stand up
the TIG path behind `compute`; (3) validate parity; (4) roll across the other 6
mechanical challenges; (5) neuralnet per D3; (6) delete the custom generator/
evaluator/local types + the old `benchmark.py` inner path.

## Instance generation

**Decision: use TIG's generator.** `tig-runtime` generates each instance inline from
`(rand_hash, nonce, track_id)`. Drop the swarm's `tig_generator`, the
`datasets/*.txt` disk cache, the GPU inline-gen path, and its `generate_instance`
usage. (Generation is fast; losing the cache-reuse speedup is acceptable.)

The model already matches TIG, so config maps directly onto `modified_test_algorithm`
args:

| Swarm | TIG / arg |
|---|---|
| `track_key` (`key=value`) | `track_id` (`key=value`) |
| per-instance `index` | `nonce` (`--start` / `--nonces`) |
| base `seed` | `rand_hash` (`--seed`) |
| `count` per track | number of nonces |

A **shared base seed** across contributors ⇒ TIG generates identical instances ⇒
fuel/quality stay comparable.

**Tracks verified (schema matches TIG exactly).** All 8 swarm `Track` field
names/types + scenario-enum casing already match TIG. Only difference is *values*
(the swarm's example config has stale difficulties). Action is pure config: set the
`tracks` map to TIG's canonical strings (+ per-track counts); the swarm's own `Track`
types get dropped for `tig_challenges`'. Canonical TIG tracks:

```
c001 satisfiability:  n_vars=10000,ratio=4267 · n_vars=100000,ratio=4150 · n_vars=100000,ratio=4200 · n_vars=5000,ratio=4267 · n_vars=7500,ratio=4267
c002 vehicle_routing: n_nodes=600 · 700 · 800 · 900 · 1000
c003 knapsack:        n_items=1000,budget=5 · 1000,budget=10 · 1000,budget=25 · 5000,budget=10 · 5000,budget=25
c004 vector_search:   n_queries=7000 · 9000 · 11000 · 13000 · 15000
c005 hypergraph:      n_h_edges=10000 · 20000 · 50000 · 100000 · 200000
c006 neuralnet:       n_hidden=4 · 7 · 10 · 14 · 18
c007 job_scheduling:  n=50,s=fjsp_high · fjsp_medium · flow_shop · hybrid_flow_shop · job_shop
c008 energy_arbitrage: s=baseline · capstone · congested · dense · multiday
```

## Timing

**Fuel is the only bound — wall-clock is dropped.** Remove the swarm's per-instance
`timeout` and all wall-clock cutoff/scoring; `tig-runtime` enforces the fuel cap
(`max_fuel_budget` = 5e12; exit 87 = out of fuel → a `failure_reason`). The per-nonce
record already carries no `elapsed`. C3's `c3_time` remains only a coarse job-level
infra ceiling, not per-instance scoring. (Optional: a generous process-level safety
timeout to catch true hangs — but fuel already bounds all instrumented compute.)

## Implementation status

- ✅ **`scripts/modified_test_algorithm`** — fuel-capturing tester (additive copy of
  upstream `test_algorithm`); `--output-json` emits the finalized per-nonce record +
  aggregates; maps signal deaths to `crashed_signal_N`. Validated end-to-end against
  **real** `tig-runtime`/`tig-verifier` (feasible + every failure path).
- ✅ **`Dockerfile.bench`** + **`scripts/build_bench_image.sh`** — Option B custom
  image, arch-parameterized; built + ran `tig-custom-image-knapsack:0.0.6` (9.83 GB),
  fixed `swarm_algo` slot + warm cache confirmed (incremental rebuild ~34s).
- ✅ **`tig_pin.json`** — version pin (`0.0.6`); digests filled in CI phase.
- ✅ **`scripts/tig_bench_driver.py`** — runs inside the image: `build_algorithm`
  once → `modified_test_algorithm` per track → one combined JSON on stdout.
- ✅ **`benchmark.py` TIG path** — `run_tig_benchmark` + `_tig_adapter`, gated by
  `_tig_backend` (config `benchmark_backend: "tig"` / env `TIG_BENCH_BACKEND=tig`),
  branched in `main()` before the custom `_reexec_in_docker`. Self-contained: mounts
  only the agent file into the `swarm_algo` slot + the driver (never `/app`).
  **Validated end-to-end** against the real image → correct `benchmark.json`
  (`score`/`feasible`/`instances_*`/`track_scores`). Per-track **median** scoring +
  shifted-geomean combiner live in `_tig_adapter`; the TIG path bounds by **fuel
  only** (no wall-clock `timeout`).
- ✅ **knapsack conformance (task 4).** Key finding: the **swarm crate is itself a
  vendored `tig-challenges`**, so no external dep is needed — instead a self-alias
  `extern crate self as tig_challenges;` (in `src/lib.rs`) makes `tig_challenges::`
  resolve to the vendored modules. Conformance pattern (reusable for all challenges):
  1. self-alias in `lib.rs` (global, once);
  2. algorithm + `initial_algorithms` files: `use super::*;` → `use
     tig_challenges::<challenge>::*;`;
  3. add `pub fn help()` (the TIG `entry_point` calls `{ALGORITHM}::help()` — the
     swarm files lacked it).
  Validated: the **real** swarm knapsack algorithm compiles via `cargo check` AND in
  the monorepo slot, benchmarking to **positive** quality (26312) through the TIG
  path. One file, both compile targets.
- ✅ **task 7 rollout (mechanical challenges).** Applied the conformance pattern to
  all 6 remaining (script `conform.py`): CPU (satisfiability, vehicle_routing,
  job_scheduling, energy_arbitrage) `use super::*;` → `tig_challenges::<ch>::*` +
  added `help()`; GPU (vector_search, hypergraph) `use crate::<ch>::*;` →
  `tig_challenges::<ch>::*` (`help()` already present). Both the live `src/.../algorithm`
  and the `initial_algorithms` seeds conformed. **CPU validated** via
  `cargo check --features solver,vehicle_routing,job_scheduling,energy_arbitrage`
  (only pre-existing warnings). satisfiability/vector_search had no live `src` algo
  (never activated) — seeds conformed. **GPU** (`cargo check` + runtime) needs
  CUDA + a GPU → deferred to a GPU env; code-conformed.
- ✅ **C3 path validated end-to-end** (the previously-blocked piece). Docker Hub
  mirror → `c3 deploy` on an l40 → `build_algorithm` + `modified_test_algorithm` +
  driver → `combined.json` → `_tig_adapter` → `benchmark.json` (knapsack, real fuel +
  positive quality). Driver is C3-ready (`TIG_WORKDIR`). Remaining for C3: formally
  wire into `c3_compute.py` (task 10) + production amd64 CI build of the custom image.
- ✅ **GPU path validated end-to-end on C3** (hypergraph, l40). `build_so` +
  `build_ptx` (nvcc, PTX fuel injection) + `tig-runtime` on GPU → feasible
  round-robin partition → `benchmark.json`. Two findings applied as fixes:
  1. **GPU kernel-injection fix:** `build_ptx` requires an algorithm-level
     `*.cu` (`tig-algorithms/src/<ch>/<algo>/*.cu`); the injection now places **both**
     `mod.rs` and `kernels.cu` into the slot (`run_tig_benchmark` mounts the algorithm
     *dir*; the C3 runner copies both). Added a `kernels.cu` seed to the GPU algo dir.
  2. **GPU contract is `Result<()>`** (save via `save_solution`), *not* the
     `Result<Option<Solution>>` the GPU templates declare — those are inconsistent
     with the generated `entry_point` in this version. Corrected the GPU `.rs` files.
  (Note: `fuel_consumed: 0` for the host-side round-robin — no kernel launched; a real
  kernel-launching GPU algorithm consumes fuel.)
- ✅ **neuralnet (task 8) validated on C3** (l40). **Seed-blinding anti-cheat:** the
  monorepo `training_loop` is patched to hand `optimizer_init_state` a non-invertible
  `optimizer_seed` (ChaCha `StdRng` from `challenge.seed`, no new dep) instead of the
  raw seed — so the agent's hooks can't regenerate the dataset. **Hooks-only
  injection:** slot `mod.rs` = conformed agent hooks (the real swarm NAdamW+SWA
  optimizer) + injected boilerplate `solve_challenge`; `kernels.cu` = agent kernels.
  Result: trained on GPU with the blinded seed → **quality 668198 (positive), real
  GPU fuel 16.8 B, feasible**. (Discovery: the swarm's *own* `training_loop` also
  leaked the real seed — it never blinded it; now the TIG path does.)
- ✅ **C3 path formally wired (task 10).** `c3_compute.py` `run_benchmark_c3` branches
  to `_run_tig_benchmark_c3` when `_tig_backend(cfg)`: tar-stages the pinned monorepo
  + driver + `modified_test_algorithm` + algorithm dir (+ neuralnet boilerplate),
  writes a `.c3` with the Docker Hub TIG image + a generated runner (per-challenge;
  neuralnet gets the seed-blinding patch + boilerplate assembly), reuses the
  deploy/poll/pull helpers, then `_load_tig_combined` → `_tig_adapter` → `benchmark.json`.
  Image = `docker.io/<tig_dockerhub|env TIG_DOCKERHUB|danieltiagoadams>/tig-dev-<challenge>:<pin>`.
  Validated: live `run_benchmark_c3` knapsack → score 32726.5, feasible.
- ✅ **D4 agent prompts (task 11) done.** `prompts.py` + `agentic_backends.py` no
  longer hardcode `use super::*;` — generic "keep the existing `use` imports"
  phrasing lets the conformed seeds (`use tig_challenges::<ch>::*;`) drive it, and the
  hyperparameter-variant validation now requires `use tig_challenges::`. Prompts and
  files are now consistent.
- ✅ **GPU `.cu` seed mechanism (task 12) — already satisfied.** `initial_algorithms/<ch>.cu`
  seeds exist; `setup.py` posts `initial_kernel_code` to the server; `run_loop` writes
  `kernels.cu` to `kernel_path` (= `src/<ch>/algorithm/kernels.cu`, same dir as `mod.rs`);
  the TIG injection copies the algorithm dir so `build_ptx` finds the `.cu`. Validated
  by the GPU C3 runs.
- ✅ **Parity pass (task 13) done.** Same knapsack GRASP through both paths, both
  feasible; TIG doesn't regress; ranking sane (GRASP ≫ naive greedy); absolute scores
  differ by design.
- ✅ **Production image pipeline (task 14) — code done.** `.github/workflows/mirror-tig-images.yml`:
  matrix over all 8 challenges, `crane copy` each ghcr `dev` image → `docker.io/<user>/tig-dev-<ch>:<pin>`
  (registry-to-registry, no disk pull), keyed off `tig_pin.json`. **Activation (user):**
  push the branch to GitHub + set secrets `DOCKERHUB_USERNAME` (=`danieltiagoadams`,
  must match the swarm's `tig_dockerhub` namespace) + `DOCKERHUB_TOKEN`, then run it.
  After it runs, all 8 challenges are C3-pullable. (Custom warm-cache Option-B images
  are a future optimization.)
- ⏭ **Remaining: task 9 (retire custom path)** — all *validation* prereqs done
  (D4/11, GPU-seed/12, parity/13) + task-14 code done. Unblocks once the mirror
  workflow has **run** (user pushes + sets secrets). Reason: retiring
  makes TIG the only path — safe for local (builds on demand) but **breaks C3 for the
  4 un-mirrored challenges** (only knapsack/hypergraph/vector_search/neuralnet are on
  Docker Hub, manually). The custom path is the fallback until every challenge has a
  C3-pullable image. Then delete the bins/generator/evaluator/datasets + benchmark.py
  custom inner logic + custom `run_benchmark_c3` path (keep the vendored types).
  Other follow-ups:
  vector_search GPU validation ✅ (done),
  + `help()` contract; seeds conformed), GPU seed mechanism (`initial_algorithms`
  carrying a `.cu`), tracks config (operator), production **amd64 CI build** of the
  warm-cache custom images → Docker Hub, and re-tag the neuralnet mirror to
  `tig-dev-neuralnet_optimizer` (wiring uses the full challenge name).

  Empirical note: the naive greedy scored **negative** quality (TIG's
  baseline-relative scale); the real algorithm scores **positive**. Confirms the
  Scoring-policy calibration wrinkle — keep the infeasible floor below the real TIG
  quality range.

## Open items
- _(none — design decisions resolved; see phasing under Algorithm-contract
  conformance + Implementation status for order.)_
