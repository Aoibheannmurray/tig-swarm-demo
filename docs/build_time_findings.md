# TIG Algorithm Build Time — Investigation Findings

_Investigation of why hypergraph benchmark "instances" take so long on C3. Challenge: hypergraph (GPU). Reference algorithm: `cloudy`._

---

## TL;DR

A hypergraph benchmark **instance** breaks down as:

| Phase | Time | Whose problem | Fixable? |
|---|---|---|---|
| C3 GPU cold-pool **provisioning** | **~34 min** (0–44 min, highly variable) | C3-side (beta capacity) | Hardware pinning / accept |
| On-machine **build** (`build_algorithm`) | **~14 min** | Swarm/TIG toolchain | See below — likely yes |
| **Solve** (the actual algorithm) | **~2 s** | — | Already fast |
| **Fuel** | 0.001–9% of budget | — | Irrelevant |

**The ~14-min build is FIXED BLOAT** — a trivial stub algorithm builds in **857 s**, essentially identical to cloudy's complex algorithm at **877 s**. It is **not** fuel, **not** the solver, and **not** the algorithm's complexity. It is the cost of compiling + fuel-instrumenting the `tig_algorithms` crate, and it is nearly constant regardless of what the agent writes.

**Leading fix (unconfirmed):** `build_so` compiles with `-C debuginfo=2`, drags that debug info through the entire `opt`/`llc`/`clang` instrumentation pipeline, then throws it away with `strip --strip-debug`. On **amd64** this makes `tig_algorithms.ll` **829 MB** (vs **15 MB** on arm64 — see the arch section below), and the single-threaded pass has to grind through all of it. Removing it (`debuginfo=0`) should collapse that and cut the build dramatically, with a byte-identical stripped `.so`. **Not yet tested.**

> ⚠️ The "~14 min" applies to **amd64 (C3/CI)**. The *same* build on **arm64** is ~3 min — not because arm64 is faster hardware, but because its IR is 55× smaller. See "amd64 vs arm64" below.

---

## What a benchmark instance actually is

Measured from a real 2-agent fleet run (hypergraph, C3 team plan). Ground truth came from the **C3 dashboard timeline** for one job:

```
08:55:59  submit
08:56:01  "No live capacity. Provisioning from the cold pool."
08:56–09:23  repeated "GPU capacity temporarily unavailable for nextgen/l40 — retrying"   (~34 min)
09:30:01  "Job assigned to compute machine"
09:30:06  "Running your script"   (image pull was instant → warm)
09:44:16  "Job completed"          (~14 min on-machine)
```

So **~34 min provisioning + ~14 min on-machine**. The image pull was *not* the bottleneck (machine was warm). Note: C3's `attempt_id`-based timestamps interleave provisioning with runtime and are **not** a reliable measure of compute — only an in-script timer or the dashboard is trustworthy.

---

## Ruling things out

### Not fuel
Across all 5 tracks: fuel used **median 0.02%, max 9%**, **zero `out_of_fuel`**. Budget = 5e12 (`DEFAULT_MAX_FUEL_BUDGET`). GPU work *is* fuel-metered (tig-runtime injects `__fuel_remaining` into the PTX, `gpu_fuel_scale=20`, reports `gpu_fuel/20 + cpu_fuel`) — so the <10% already includes GPU. Lowering the fuel budget does nothing.

### Not the solver
The algorithm **solves in ~2 s** (measured per-nonce). Quality/feasibility unaffected.

### Not the algorithm's complexity
See the decisive trivial-vs-complex test below.

### The object cache — a correct fix for the *wrong* cost (dead end)
We built a content-addressed object cache into `build_so` (cache each `.o` keyed on `sha256(.ll)` + compile mode + toolchain fingerprint). It is **provably transparent** (object set byte-identical across independent builds; validated in CI). But on real C3 runs it gave **no speedup** (807–826 s, same as before): the cache **hits** the 56 stable library crates but **misses** `tig_algorithms` (the algorithm crate), which is the entire cost. **Shelved — do not ship.**

> ⚠️ A CI test initially showed "560 s → 4 s" and looked like a win. That was a **false positive**: the test reused *one fixed template algorithm* for both cold and warm builds, so `tig_algorithms` was a cache hit. With a *different* real algorithm it misses. Lesson: measure with a *changed* algorithm.

---

## Decomposing the ~14-min build

`build_algorithm` = `build_so` (Rust → instrumented `.so`) + `build_ptx` (nvcc → PTX).

| Component | Time |
|---|---|
| `build_so` | ~825 s |
| `build_ptx` | ~1.4 s |
| solve | ~1.8 s |

`build_so` itself (measured, real algorithm, warm cargo cache):

| Phase | Time | What |
|---|---|---|
| **cargo (rustc)** | ~307 s | compile `tig_algorithms` + `tig_binary` → LLVM IR |
| **instrumentation loop** | ~557–575 s | `opt` (fuel pass) + `llc` + `clang` over the `.ll` files → objects |
| **link + strip** | ~1 s | — |

**The entire instrumentation-loop time is ONE file: `tig_algorithms.ll`.** `tig_binary` is 2 s; the 56 library crates (std/core/alloc/cudarc/…) are cache hits (~instant with the object cache; otherwise also compiled).

---

## The decisive experiment: TRIVIAL vs COMPLEX

Built two algorithms through the **identical** path (warm std/deps cargo cache; each a fresh `tig_algorithms` compile), CI run `29327326099`:

| Algorithm | Total | cargo | loop | loop file |
|---|---|---|---|---|
| **TRIVIAL** (template stub + one dummy `fn`) | **857 s** | 299 s | 557 s | `tig_algorithms` |
| **COMPLEX** (cloudy's real algorithm) | **877 s** | 314 s | 561 s | `tig_algorithms` |

**TRIVIAL ≈ COMPLEX.** A stub that does nothing costs the same ~14 min. → The build time is a **fixed cost of compiling + instrumenting the `tig_algorithms` crate**, independent of the algorithm. (This corrected an earlier wrong guess that cloudy's *complexity* was the cause.)

---

## Root cause (leading hypothesis — UNCONFIRMED)

The bloat is that `tig_algorithms.ll` is enormous even for a stub. Prime suspect in `build_so`'s flags:

```bash
RUSTFLAGS="--emit=llvm-ir ... -C opt-level=3 -C debuginfo=2 ..."   # generates full debug info
...
strip --strip-debug $output                                        # ...then discards it
```

`-C debuginfo=2` emits full debug info for every monomorphized `cudarc`/`std` instantiation. That debug-laden IR is dragged through the whole `opt`(fuel pass) → `llc` → `clang` pipeline (the ~557 s), then `strip --strip-debug` deletes it. Debug info is **not executable code**, so it cannot affect `fuel_consumed` or the runtime signature, and the shipped `.so` is stripped anyway — which makes it look like **safe dead weight.**

**Secondary factor:** `-C codegen-units=1` forces `tig_algorithms` into a **single** IR file, so the instrumentation loop is **single-threaded** on it (idle cores). `codegen-units>1` could parallelize it — but cu=1 is likely load-bearing for deterministic fuel/signatures, so that's riskier.

---

## Fixes & levers (ranked)

1. **`debuginfo=0` in `build_so`** — TIG-side, one-line, likely large + safe (debug info is stripped anyway). **Needs confirmation:** rebuild with `debuginfo=0`, verify the loop collapses and the *stripped* `.so` is byte-identical. _(Not yet run.)_
2. **`codegen-units>1`** to parallelize the instrumentation of `tig_algorithms.ll` across cores — TIG-side, must verify it doesn't change fuel/runtime_signature.
3. **Build once per benchmark, distribute `.so`+`.ptx` to all shards** — swarm-side, no TIG change. All 25 shards of a benchmark build the *same* algorithm; today they each pay the ~14 min. Build once → hand the artifact to the rest.
4. **Provisioning (~34 min)** — the single biggest wall-clock chunk. Route auto-picked `nextgen/l40` with no live capacity; `c3_hardware='auto'` → pin to a warm-capacity class.
5. **Shard less** — each shard is an independent provisioning lottery (0–44 min); 25 one-nonce shards = 25 lotteries, and the benchmark waits for the unluckiest.

---

## Tooling built during the investigation

- **In-script timer** in `scripts/tig_bench_driver.py` — prints `[timing]` (build/solve) and `[build-analysis]` (cargo `Compiling` lines, cache hits) to stderr → captured in each C3 job's `driver.stderr` artifact; also embeds `timings` in `combined.json`. Includes a diagnostic `build_so`/`build_ptx` split.
- **CI measurement workflows** (on `Aoibheannmurray/tig-swarm-demo`, throwaway branches): `measure-buildso-split` (phase + per-file timing, trivial-vs-complex), `validate-build-so-cache` (object-cache transparency), `build-hypergraph-interim` (build+push a patched image).
- **Fuel data** is only retrievable per-job via `c3 pull` → `combined.json` (`fuel_consumed` is dropped by `_tig_adapter` before publish, so the server never stores it).

---

## Key references (for traceability)

- C3 timer run (build 846 s / solve 1.9 s): job `job_1783950240807_cgn72p`
- `build_so`/`build_ptx` split: job `job_1783951929265_sb7rig`
- Object-cache real-algo tests (no speedup): jobs `…22r4m9` (`:0.0.6`), `…wncyg3` (fresh `:objcache-test` tag)
- Phase split (cargo 307 / loop 575 / link 1): CI run `29323868875`
- Trivial vs complex (857 vs 877): CI run `29327326099`
- `build_so` source: `../tig-monorepo/tig-binary/scripts/build_so` (RUSTFLAGS line 33, strip line 254)

## Open items

- [ ] **Confirm the `debuginfo=0` fix** (the one remaining high-value test).
- [ ] Object-cache PR (`Daniel-T-S-Adams/tig-monorepo:build-so-object-cache`) — **shelved**, don't merge.
- [ ] `docker.io/danieltiagoadams/tig-bench-hypergraph:0.0.6` is currently the object-cache-patched image (functionally identical output, useless cache) — harmless, can restore the original.

---

## amd64 vs arm64 — the real driver of the C3 slowness (55× IR blowup)

A local build of the *same* algorithm is far faster than C3, which triggered a deeper look. The answer is **not hardware** — it's that the amd64 build emits a pathologically huge IR file.

### Local (arm64) vs C3 (amd64), same algorithm (cloudy)

| | Local (Daniel) | C3 L40 node |
|---|---|---|
| CPU | ARM Neoverse-N1 @ 2.0 GHz | **AMD EPYC 7763** @ 2.44 GHz |
| cores (container) | 8 | **28** (2 sockets) |
| RAM | 16 GB | **60 GB** |
| CPU throttle | none | **none** (`cpu.max: max`) |
| single-core benchmark | 43.9 s (2.3 Miter/s) | **17.5 s (5.7 Miter/s)** — 2.5× *faster* |
| **hypergraph build** | **~192 s (3 min)** | **~846 s (14 min)** — 4.4× *slower* |

C3's machine is **strictly more powerful** (faster core, 3.5× cores, 4× RAM, no throttle) yet builds **4.4× slower**. So it is **not** the hardware.

### It's single-thread-bound, and the amd64 IR is 55× bigger
- **28 cores don't help:** GitHub's 2-core amd64 runner (843 s) ≈ C3's 28-core EPYC (846 s). Identical → the build is **single-thread-bound** (extra cores sit idle). The bottleneck is instrumenting one giant file, `tig_algorithms.ll`, through `opt`/`llc`/`clang` on a single core.
- **The file size, same source + flags, different target:**

  | | `tig_algorithms.ll` |
  |---|---|
  | arm64 (local) | **15 MB** |
  | amd64 (C3/CI) | **829 MB** — ~55× bigger |

829 MB of LLVM IR for one crate is not normal codegen — it's almost certainly **DWARF debug metadata** from `-C debuginfo=2` exploding over all the monomorphized `cudarc` generic code on x86-64 (arm64's is far leaner). And `build_so` **strips it all** at the end (`strip --strip-debug`). So it generates ~800 MB of debug info, drags it single-threaded through the whole instrumentation pipeline, then discards it.

### Why the local builds felt instant
Two independent reasons, now both understood:
1. The algorithms built locally were often **CPU challenges** (e.g. `job_scheduling`) — no `cudarc`, so `tig_algorithms.ll` stays small (~32 s builds).
2. Even for the GPU challenge, the **arm64 IR is 55× smaller** than amd64's, so the arm64 build is minutes, not the ~14 min amd64 pays.

### Confirmed dead ends along the way
- **Not fuel** (0.001–9% used, zero out-of-fuel).
- **Not the algorithm** (trivial stub ≈ complex: 857 s ≈ 877 s on amd64).
- **Not the object cache** (validated byte-identical, but `tig_algorithms` is a per-build miss → no speedup).
- **Not warm/incremental state** (warm identical rebuild still ~591 s — the loop reprocesses everything).
- **Not the hardware** (C3 is faster + more cores + no throttle, still 4.4× slower).
- **It's the 829 MB amd64 IR** (debug metadata over monomorphized cudarc), processed single-threaded.

### The fix to test
`-C debuginfo=2 → 0` in `build_so`. Prediction: `tig_algorithms.ll` collapses from 829 MB, the amd64/C3 build drops toward arm64-like times, and the shipped (stripped) `.so` is byte-identical. Verification: build cloudy on amd64 with `debuginfo=0`, compare IR size + build time, and confirm the stripped `.so` matches.

### Key run/job references (this section)
- CPU comparison job (C3 l40): `job_1784040116887_2afj1r`
- amd64 IR-size CI run (829 MB): `29343473723`
- warm-repeat test (BUILD2 still 591 s): `29333572571`
- arm64 IR size (15 MB): user's local `~/swarm/tig-monorepo/target/aarch64-*/release/deps/tig_algorithms.ll`

---

## The `debuginfo=0` experiment (CI run `29348258939`)

Built cloudy on amd64 both ways (fresh `target/` each, so no stale-IR collisions):

| | build time | `tig_algorithms.ll` | cargo | loop |
|---|---|---|---|---|
| **debuginfo=2** (current) | 898 s | **829 MB** | 365 s | 531 s |
| **debuginfo=0** (fix) | **522 s** | **295 MB** | 242 s | 278 s |

**`debuginfo=0` is a real ~42% build-time win** (898 → 522 s), a one-line `build_so` change. Debug info was **~534 MB (64%)** of the IR and got stripped at the end anyway — pure dead weight through the single-threaded instrumentation pipeline.

**Correctness:** debug info is metadata, not code, and the shipped `.so` is stripped either way, so `debuginfo=0` *should* be behavior-identical. A crude `nm` symbol-hash flagged a difference, but both stripped `.so`s are the identical **21 MB** — likely benign (`--gc-sections` keeping a few debug-referenced symbols). **CONFIRMED behavior-identical** (C3 GPU job `84ajgh`): same nonce/seed, both builds gave identical `fuel_consumed=52,767,733`, quality=-1888, feasible=True → identical executed instructions. Safe to ship.

### …but it's only HALF the gap: amd64 emits ~20× more cudarc *code* than arm64

The big surprise: even at `debuginfo=0`, amd64 `tig_algorithms.ll` is **295 MB**, vs arm64's **15 MB *total* (including debug)**. So there are **two independent amd64 bloat sources**:

| source | size on amd64 | fix |
|---|---|---|
| DWARF debug info (`debuginfo=2`) | ~534 MB | `debuginfo=0` (done — ~42% win) |
| **monomorphized `cudarc` code** | **~295 MB** (vs ~15 MB arm64) | **open — deeper** |

The second one is the real puzzle: **x86-64 monomorphizes/inlines ~20× more `cudarc` code than aarch64 for the identical source and flags.** This is why the amd64 build stays ~8.7 min even after removing debug info, while arm64 is ~3 min. Likely candidates: `bindgen`-generated cudarc structs (huge unions with per-field `Default` impls — visible in the linker errors), target-specific inlining/monomorphization, or an amd64-specific codegen blowup. Not yet root-caused.

**Ranked fix update:**
1. **`debuginfo=0`** — confirmed ~42% win, one line, **behavior-identity verified** (identical fuel/quality/feasibility on GPU). Ship it.
2. **Reduce the amd64 cudarc monomorphization** (the ~295 MB) — the other ~half; deeper, needs investigation (bindgen output, inlining, or `codegen-units`).
3. Build-once-distribute to shards; provisioning/hardware pinning; shard less (as before).
