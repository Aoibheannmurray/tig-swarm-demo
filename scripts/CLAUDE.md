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

**Warm images (optional, C3 fast path).** `Dockerfile.warm` bakes the swarm
crate source + a pre-built release cargo target into
`tig-swarm-warm-{cpu,gpu}` (build: `scripts/build_warm_image.sh`; publish: CI
`build-warm-images.yml`, amd64-only — C3 is amd64 and local compute never
uses these). When configured, the C3 job uploads ONLY the algorithm dir +
scripts + config, injects the algorithm into the baked crate at `/app`, and
incremental-builds in well under a minute instead of a 10–20 min cold
compile. Opt in per fleet/agent via `c3_warm_image: <full ref>` (or env
`TIG_C3_WARM_IMAGE`), or `c3_warm_images: true` + `tig_dockerhub: <ns>` to
derive `docker.io/<ns>/tig-swarm-warm-{cpu|gpu}:latest`. Unset = the plain
full-source path above. Rebuild/republish the images whenever `src/` (the
challenge harnesses) or the Cargo manifests change — CI does this on push to
staging; a job-side cmp-guarded overlay keeps a drifted Cargo.toml correct
(just slower) in the meantime. See `scripts/test_warm_c3.py`.

**Distributed C3 (balanced sharding + fleet pool).** On the C3 path a
benchmark's instances are split into exactly
`min(c3_max_parallel_jobs, total_instances)` **balanced** shards (sizes differ
by ≤1 instance: 22 over 3 → 8,7,7), packing *across* track boundaries (a shard
may carry slices from several tracks). Each shard is its own C3 job running
`benchmark.py` on its per-track window — `TIG_TRACK_STARTS` offsets instance
indices, and the per-instance seed depends only on the global index
(`tig_generator --start` / `tig_gpu_benchmark --index`), so shard windows are
byte-identical to the unsharded run. The per-shard `benchmark.json`s are
merged (`_merge_shard_benchmarks`) and re-aggregated with `benchmark.aggregate`,
so the score matches a single-job run exactly — only faster. With
`c3_max_parallel_jobs=1` a benchmark runs as a single job. One benchmark can
therefore use every chip the plan allows (e.g. free plan = 3) while the rest
of the fleet waits on LLM responses. See `scripts/test_c3_sharding.py`.
Sharding pays off most on warm images (per-shard fixed cost is seconds); on
the full-source path each shard repeats the cold compile.

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

## Tests

No pytest. `test_*.py` are self-running scripts (`if __name__ == "__main__"`) —
run one directly, e.g. `python scripts/test_benchmark_run_ids.py`.
