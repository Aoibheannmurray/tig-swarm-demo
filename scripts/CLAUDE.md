# CLAUDE.md — scripts/ (orchestration)

The Python that drives the swarm locally: the per-agent improvement loop, the
multi-agent fleet, benchmarking, and publishing results to the server.

## Gotchas — read before refactoring

- **This is NOT a package.** Modules are flat, have no `__init__.py`, and import
  each other by **bare name** (`from prompts import ...`, `from agentic_backends
  import ...`) via `sys.path`. Don't nest files into subfolders or switch to
  relative imports without rewiring every import.
- **Several modules run as subprocesses by path** — e.g. `python
  scripts/run_loop.py`, and `benchmark.py` is invoked with `cwd=<worktree>`.
  Renaming or moving a module breaks those call sites; grep the filename first.
- `setup.py` is at the **repo root** (host-admin CLI), not here. Scripts shell
  out to it as `python setup.py sync`.

## Map

- `run_loop.py` — one agent's loop: sync config → get state → propose an edit →
  `benchmark.py` → `publish.py`. The core driver.
- `run_fleet.py` — launches/manages N agents (one git worktree each) from
  `fleet.config.json`. The root `run.py` wraps this for contributors.
- `benchmark.py` — compiles + scores an algorithm (local Docker or C3 cloud).
  Run with `cwd` = the agent's worktree.
- `publish.py` — posts score + hypothesis + heartbeat to the server.
- `agentic_backends.py` / `agentic_sandbox.py` — sandboxed coding-agent mode
  (Claude Code / Codex editing in a worktree). **`agentic_backends.py` generates
  the per-worktree `CLAUDE.md` / `AGENTS.md` and permission settings** — see
  `_build_sandbox_settings`.
- `llm_backends.py` / `prompts.py` — single-shot (non-agentic) LLM mode.
- `swarm_client.py` — HTTP client for the coordination server (register, state,
  heartbeat, publish, `resolve_server_url`). Named to avoid shadowing the
  top-level `server/` package on `sys.path`.
- `c3_compute.py` — cloud (C3) path for benchmarking.
- `challenge_files.py`, `download_algorithm.py`, `init_fleet.py`,
  `sync_identity.py`, `build_ptx.py` — supporting helpers.

## Benchmark backend (simple builds, wall-clock)

`benchmark.py` compiles the swarm's own binaries (`tig_generator` /
`tig_solver` / `tig_evaluator`, or `tig_gpu_benchmark` for GPU challenges)
with cargo and scores each instance under a **per-instance wall-clock
timeout** (`timeout` in the synced challenge config; the solver saves early
and re-saves — unsaved at the deadline = infeasible).

- **Local**: `benchmark.py` re-execs itself inside a plain toolchain image
  built from root `Dockerfile.cpu` / `Dockerfile.gpu` (`tig-swarm-cpu` /
  `tig-swarm-gpu`, built lazily on first run), with a per-agent cargo
  `target/` volume. Instances are generated once and cached under
  `datasets/<challenge>/generated/`.
- **C3** (`c3_compute.py`): stages a minimal workspace (Cargo.toml + src/ +
  scripts/ + .swarm-cache.json) into a temp dir, deploys it as ONE C3 job on
  a public Docker Hub image (`rust:1-bookworm` CPU / `nvidia/cuda:…-devel`
  GPU — the runner script apt-installs anything missing), runs
  `scripts/benchmark.py` inside, and pulls back `benchmark.json`.

Algorithms author against `tig_challenges::<ch>::*` (`src/lib.rs` self-aliases
the crate as `tig_challenges`, so a file also compiles when ported to the
upstream tig-monorepo).

**Warm images (the DEFAULT C3 path).** `Dockerfile.warm` bakes the swarm
crate source + a pre-built release cargo target into
`tig-swarm-warm-{cpu,gpu}` (build: `scripts/build_warm_image.sh`; publish: CI
`build-warm-images.yml`, amd64-only — C3 is amd64 and local compute never
uses these). The C3 job uploads ONLY the algorithm dir +
scripts + config, injects the algorithm into the baked crate at `/app`, and
incremental-builds in well under a minute instead of a 10–20 min cold
compile. This is on by default: every C3 benchmark pulls
`docker.io/tigfoundation/tig-swarm-warm-{cpu|gpu}:latest` (the TIG
Foundation's public namespace, published by CI; override the namespace with
`tig_dockerhub: <ns>` / env `TIG_DOCKERHUB`), or an exact ref pinned with
`c3_warm_image: <full ref>` (or env `TIG_C3_WARM_IMAGE`). Opt out with
`c3_warm_images: false` (or `TIG_C3_WARM_IMAGES=0`) to fall back to the
full-source path above — worth doing only when running a namespace whose
images aren't published. Rebuild/republish the images whenever `src/` (the
challenge harnesses) or the Cargo manifests change — CI does this on push to
staging; a job-side cmp-guarded overlay of the Cargo manifests AND the
`src/` harness tree (uploaded with algorithm dirs excluded, ~0.5MB) keeps a
drifted or stale-cached image correct (just slower) in the meantime — C3
nodes cache `:latest`, so without the src overlay a stale node fails the
build with method-not-found errors on APIs the current crate supports. See
`scripts/test_warm_c3.py`.

**Distributed C3 (balanced sharding + fleet pool).** On the C3 path a
benchmark's instances are split into **balanced** shards (sizes differ
by ≤1 instance: 22 over 3 → 8,7,7), packing *across* track boundaries (a shard
may carry slices from several tracks). Each shard is its own C3 job running
`benchmark.py` on its per-track window — `TIG_TRACK_STARTS` offsets instance
indices, and the per-instance seed depends only on the global index
(`tig_generator --start` / `tig_gpu_benchmark --index`), so shard windows are
byte-identical to the unsharded run. The per-shard `benchmark.json`s are
merged (`_merge_shard_benchmarks`) and re-aggregated with `benchmark.aggregate`,
so the score matches a single-job run exactly — only faster. One benchmark can
therefore use every chip the plan allows (e.g. free plan = 3) while the rest
of the fleet waits on LLM responses. See `scripts/test_c3_sharding.py`.

**How many shards — the cap is a ceiling, not a target.**
`c3_max_parallel_jobs` bounds the shard count; `_worthwhile_shards` decides
how much of it to spend. A shard is not free: it provisions its own C3 box and
repeats the build, and because shards run in *parallel* the slowest provision
sets the wall clock. Measured here, three identical 1-instance full-source
shards deployed 8s apart finished 2m37s / 6m01s / 9m20s after submit — so a
benchmark with a minute of solving in it gains nothing from three boxes, bills
three machines, and holds the whole fleet's slot pool while it does.

The rule: going from `s-1` to `s` shards saves `solve / (s·(s-1))` of wall
clock, so take it only while that beats one shard's fixed cost
(`_SHARD_FIXED_SECS_WARM` 60s / `_SHARD_FIXED_SECS_FULL_SOURCE` 300s, override
per swarm with `c3_shard_fixed_secs`). `solve` is
`ceil(instances / workers) × timeout`, where `workers` mirrors
`benchmark.resolve_bench_workers` (explicit `bench_workers`, else half the
vCPUs parsed out of the `c3_hardware` profile name). Never more shards than
solving waves — a shard that can't fill its workers idles cores and still pays
a full provision. So 6 instances × 30s → 1 job; 200 × 60s → the full cap. GPU
jobs are one solver each, so sharding *is* their parallelism and they still
fan out. Missing cost inputs (no `timeout`, `c3_shard_fixed_secs: 0`) fall back
to using the whole cap.

**Big CPU machines (auto).** With `c3_hardware` unset/`auto`, each CPU
benchmark queries the C3 control plane and deploys on the best CPU profile
actually available right now — highest availability tier, then most vCPUs
(`_best_cpu_hardware`, cached ~10 min; falls back to `cpu-d3-4vcpu-16gb`
offline). Inside the job, `bench_workers` (config, or env
`TIG_BENCH_WORKERS`) sets concurrent solver processes; its default
`max(4, cpu_count // 2)` rides whatever machine the job landed on (≈ physical
cores — solvers are single-threaded + timeout-bounded, so oversubscribing SMT
threads lowers scores). Set `c3_hardware` + `bench_workers` explicitly to pin
hardware and contention instead. GPUs: one job = one GPU regardless of
profile; parallelism comes from sharding (default `l40` — cheapest, highest
availability).

**Fleet-wide C3 slot pool.** Every agent in a fleet shares ONE C3 key, so all
C3 shard jobs gate on a fleet-wide FCFS slot pool (`c3_pool.py`) of
`c3_max_parallel_jobs` slots: total live C3 jobs never exceed the plan cap,
extras queue first-come-first-served. `run_fleet.py` points every agent at one
pool dir (`.c3-pool/` under the repo/clone root) via `C3_POOL_DIR` and injects
one agreed `C3_POOL_SIZE`; a lone `run.py` agent (no pool dir) falls back to an
in-process semaphore.

**The cap is read LIVE from C3, not configured.** At launch `run_fleet.py` queries
the control plane (`/v2/billing/subscription` + `/v2/billing/tiers`, honoring any
per-account override) for the concurrency limit C3 actually enforces (free 3 /
pro 10 / team 50 today) and stamps it onto every agent's `c3_max_parallel_jobs` —
so the pool size always matches the real subscription. A configured
`c3_max_parallel_jobs` is only a fallback used when the query fails (offline /
no C3 auth). See `scripts/test_c3_pool.py` and `scripts/test_c3_plan_cap.py`.

**Retry safety net for the plan cap (`CONCURRENCY_LIMIT` 429).** The pool keeps
*this fleet's* live jobs under the cap, but C3's real-time chip count can still
reject a `c3 deploy` with `429 (CONCURRENCY_LIMIT)` — teardown lag on a
just-released sibling chip, a manual/lone `c3` job holding a chip outside the
pool, or a pool sized above the account's true tier. That 429 reports "No new
job was queued", so nothing is orphaned: `_run_one_c3_job_inner` treats it as
retryable and *waits for a chip to free* (patient — chips free on a minute
scale — but bounded by `_CONCURRENCY_MAX_WAIT_SECS`, after which it surfaces an
actionable error naming `c3 squeue`/`c3 cancel`), holding its pool slot
throughout so it stays first in line. This is distinct from the object-store
upload throttle (`_DEPLOY_RETRY_SIGNATURES`, sub-20s backoff) and is checked
first. See `scripts/test_c3_concurrency_retry.py`.

## Tests

No pytest. `test_*.py` are self-running scripts (`if __name__ == "__main__"`) —
run one directly, e.g. `python scripts/test_benchmark_run_ids.py`.
