# Architecture: Collaborative AI Swarm Optimization

This document explains how the swarm optimization demo works at a high level — how multiple LLM-driven agents collaborate to evolve a solver for one of eight TIG challenges, and how the coordination server orchestrates their work.

## The Big Picture

A group of autonomous LLM-driven agents — each one a contributor running `scripts/run_loop.py` against an LLM provider — try to improve a Rust solver for the active challenge (chosen at setup time). They share a coordination server that tracks what's been tried, what worked, and what failed. A live dashboard projects the swarm's progress in real-time.

```
 ┌──────────┐  ┌──────────┐  ┌──────────┐
 │  Agent 1 │  │  Agent 2 │  │  Agent N │   Each agent: proposes ideas,
 │ (Claude) │  │ (Claude) │  │ (Claude) │   writes Rust code, benchmarks
 └────┬─────┘  └────┬─────┘  └────┬─────┘
      │              │              │
      └──────────────┼──────────────┘
                     │
              ┌──────┴──────┐
              │ Coordination│
              │   Server    │
              │             │
              └──────┬──────┘
                     │
              ┌──────┴──────┐
              │  Dashboard  │
              │  (Browser)  │
              └─────────────┘
```

## Per-Deploy Isolation

Every swarm is its own independent deployment with its own SQLite database — no central multi-tenant server. A host runs `python setup.py create` to stand up a new swarm; contributors point `fleet.config.json` at the URL it returns. Multiple swarms run side-by-side without overlap, even when launched by the same host. (See `README.md` for the concrete setup commands.)

The singleton `config` table holds global swarm settings: `active_challenge` (the swarm-wide challenge contributors auto-follow), `swarm_name`, `swarm_type` (`cpu` or `gpu`), `owner_name`, `stagnation_threshold`, `stagnation_limit`, `hypothesis_recall_threshold`, `inactive_minutes`, and `admin_key`. Per-challenge sub-config (tracks, timeout, scoring_direction, initial_algorithm_code) lives in a separate `challenge_configs` table — one row per challenge in this swarm's hardware class (five for CPU swarms, three for GPU), all populated in parallel by `setup.py create`.

## Multi-Challenge State

Each swarm hosts every challenge in its hardware class side by side. Contributors all work on **one** challenge at a time — whichever the host has set as `active_challenge` — but per-(agent, challenge) state is preserved across switches, so when the host flips back to a previously-used challenge every agent's prior trajectory resumes.

State isolation is enforced at the schema level:

- **`trajectory_bests`** has a composite primary key `(agent_id, challenge)` — physically per-challenge, no singleton ambiguity.
- **`agent_challenge_state`** is a join table holding per-(agent, challenge) counters: `runs_since_improvement`, `improvements`, `current_trajectory_id`, `current_program_id`, `best_ever_score`, etc. Replaces the per-challenge counter columns previously on the `agents` table.
- **`experiments`, `hypotheses`, `inactive_algorithms`, `trajectories`, `best_history`, `messages`** all carry a `challenge` column; every read filters by it.

Two correctness invariants underpin the design:

1. **The inactive-algorithm pool is per-challenge.** `get_inactive_with_deactivations(conn, challenge)` only returns algorithms tagged with the requested challenge — so a stagnating agent's "fresh start" can't be handed code from a different challenge that wouldn't compile against its `Challenge` / `Solution` types.
2. **Inspiration is filtered by per-challenge active agents.** The "active peers" set comes from `agent_challenge_state(*, challenge).last_active_at`, NOT from the global `agents.last_heartbeat`. An agent who's been on VRP for days but switched to SAT five minutes ago does NOT supply VRP inspiration to other VRP-resident agents.

The host switches the active challenge via admin-key-gated `POST /api/swarm_config { active_challenge }`. The server broadcasts `swarm_config_updated` over the WebSocket so live dashboards re-fetch; contributors pick up the new challenge on their next iteration via `python setup.py sync` (which `run_loop.py` calls automatically at the top of every iteration). Only the host can change the active challenge — `setup.py switch` requires `swarm.admin.json`, which only exists on the host's clone.

## Supported Challenges

The swarm supports eight TIG challenges, selectable at setup time. Five are CPU-only; three require an NVIDIA GPU (the swarm host picks one of those modes via `swarm_type` and only the matching subset is exposed to contributors).

| Challenge | Hardware | Scoring | Description |
|-----------|----------|---------|-------------|
| `vehicle_routing` | CPU | Higher is better | VRPTW: minimize total distance for a fleet serving customers with time windows |
| `knapsack` | CPU | Higher is better | Quadratic knapsack: maximize value of selected items subject to weight budget |
| `satisfiability` | CPU | Higher is better | MAX-SAT: maximize satisfied clauses in a CNF formula |
| `job_scheduling` | CPU | Higher is better | Minimize makespan across machines for a set of jobs |
| `energy_arbitrage` | CPU | Higher is better | Maximize profit from battery charge/discharge against energy prices |
| `hypergraph` | GPU | Higher is better | Hypergraph partitioning: minimize edge cut across balanced parts |
| `neuralnet_optimizer` | GPU | Higher is better | Train a small neural net under a wall-clock budget; score is validation loss vs baseline |
| `vector_search` | GPU | Higher is better | Approximate nearest-neighbour search over a vector index |

The CPU/GPU split is enforced by `challenges.CHALLENGES[name].is_gpu` — `register_agent` and `/api/swarm_config` filter `available_challenges` against the swarm's `swarm_type` so contributors only see the challenges their hardware can run.

All challenges use baseline-relative quality scoring: `(baseline − you) / baseline × QUALITY_PRECISION` for minimize-direction challenges, `(you − baseline) / baseline × QUALITY_PRECISION` for maximize-direction. The result is always higher-is-better. Per-track scores are arithmetic means; the overall score is a shifted geometric mean across tracks.

Challenge-specific details (types, tips, strategy tags) live in `CHALLENGE.md`, templated from `src/<challenge>/README.md` by `setup.py create` / `setup.py sync` whenever the active challenge changes.

## How Agents Work

Each agent is one contributor running `scripts/run_loop.py` against an LLM provider (Anthropic, OpenAI, Google, Venice, OpenRouter, any OpenAI-compatible endpoint, or the local `claude` / `codex` CLI in either single-shot or agentic mode). The script clones this repo, reads `CHALLENGE.md` (challenge-specific details), and enters an autonomous optimization loop.

### Two contributor modes

- **Single-shot mode** (all API providers, plus `claude-code`). `run_loop.py` owns the whole workflow: hypothesis call → code call → write `mod.rs` → benchmark (with compile-fix and runtime-fix retry loops) → publish. The LLM is a stateless completer that returns a code blob; the Python driver does everything else.
- **Agentic mode** (`claude-code-agentic` or `codex-agentic`). `run_loop.py` shells one headless agent call per iteration inside a sandboxed git worktree. The agent reads state, edits the algorithm file in place via its Edit tool, runs `cargo check` itself, and writes `.swarm/hypothesis.json` before stopping. The Python driver still owns server I/O (state, heartbeat, publish) and the official benchmark; the agent's job is bounded to "edit algorithm files + write hypothesis." A background heartbeat thread fires every 60s during the agentic call so a multi-minute iteration doesn't drop the agent from the inspiration pool. Wall-clock-bounded by `--agentic-timeout` (default 1800s).

  The sandbox has two layers in both backends. The hard boundary is always the git worktree (`worktrees/<agent>/`) — the agent's cwd is the worktree, so it physically cannot reach outside (sibling agents, secrets, the main checkout, the user's home dir). The second layer is backend-specific:
  - `claude-code-agentic`: fine-grained `.swarm/sandbox-settings.json` — Edit limited to the algorithm file (and `kernels.cu` if GPU) plus `.swarm/hypothesis.json`; Read scoped to the worktree by cwd; Bash limited to `cargo check/build/fmt/clippy`; WebFetch/WebSearch and any network-touching Bash command denied. The `claude` CLI enforces these per tool call.
  - `codex-agentic`: coarser sandbox mode `workspace-write` (the only realistic option in `codex exec` for an editing agent) — gives the agent write access to the whole worktree with network access forced off. File-scope inside the worktree is enforced soft-style via `AGENTS.md` instructions; out-of-scope edits get silently dropped because the loop only copies the algorithm file back to the main checkout when scoring.

The mode is selected per-agent in `fleet.config.json` (`provider` field) and can be overridden per-run via `--provider` on `scripts/run_loop.py`. All modes interact with the swarm server through the same protocol — published iterations look the same on the dashboard, just with different cost profiles (agentic mode typically burns 5–20× the tokens of single-shot for the same iteration).

### 1. Register

The agent registers with the server and receives a unique ID and a randomly generated name (like "cosmic-eagle" or "swift-hydra"), along with configuration for which benchmark instances to run.

### 2. Check State

The agent asks the server for the current state, passing its `agent_id`. The server returns the agent's **own current best** algorithm code (or the swarm's host-configured *initial algorithm* on first run; see "Initial algorithm" below), so each agent advances its own lineage. If the agent is stagnating (`runs_since_improvement >= stagnation_threshold`, default 2), the response may also include `inspiration_code` from a random active peer to study.

#### How inspiration is picked

Inspiration is the only channel for cross-pollination between lineages, so the selection rule matters. It is deliberately simple:

- **Trigger.** Inspiration is attached to the `/api/state` response whenever `runs_since_improvement >= stagnation_threshold` (the swarm-config setting, default 2). The counter increments on every non-improving publish and resets to 0 the moment the agent beats its own best. So at the default an agent sees inspiration starting on its *3rd* state fetch after a breakthrough — i.e. after two failed attempts against its current best — and keeps seeing it every poll until it improves.
- **Candidate pool.** The pool is built from every agent's *current best* (one row per agent, via `db.list_trajectory_bests`), with two filters: (a) the requesting agent is excluded, and (b) only peers whose `agent_challenge_state.last_active_at` for the *active* challenge is within the last `inactive_minutes` (swarm-config setting, default 20) are eligible. Dormant agents — including agents whose global heartbeat is recent but who have not touched this challenge lately — are skipped entirely.
- **Selection.** Uniform random (`random.choice`) over the filtered pool. **Not** weighted by score, recency, improvement rate, or diversity. A mid-pack active agent is just as likely to be picked as the current leader, and the pool can hand you a peer whose best is *worse* than yours — the value is in structural ideas, not in the score.
- **Memorylessness.** Selection is re-rolled on every state fetch while the agent is stagnating. There is no "don't repeat last pick" rule and no rotation guarantee: two consecutive polls can return the same peer, and over many polls coverage of the pool is probabilistic rather than guaranteed. The *content* of a peer's entry can also change between polls as that peer publishes new bests.
- **Empty pool.** If no peer passes the active-and-not-self filter (e.g. the agent is alone, or all peers are dormant), `inspiration_code` is simply `null` for that poll — stagnation continues without a suggestion.

The state includes:

- **Best algorithm code** — the Rust source code of the agent's own current best branch.
- **Best score** — the current global best score across all agents.
- **Personal counters** — own best score, runs completed, improvements, and runs since last improvement.
- **Prior hypotheses (up to 20)** — only attached once the agent has stagnated past `hypothesis_recall_threshold` (default 3). The list is the most recent **failed** hypotheses any agent tried against *this exact program* — the point is "here's what's already been ruled out from this starting point, so don't repeat it."
- **Inspiration code** — optional code from a random active peer when stagnating.
- **Leaderboard** — agent rankings by best score.

The prior-hypotheses list is scoped by `program_id` (carried on `agent_challenge_state.current_program_id`), so the moment the agent lands a new best — or adopts a trajectory from the inactive pool — the list naturally resets to the attempts made against that new starting point.

### 3. Propose a Hypothesis

The agent formulates a specific optimization idea and submits it to the server with a strategy tag. An example of available strategy tags might be :

| Tag | Examples |
|-----|----------|
| `construction` | Nearest neighbor, savings algorithm, regret insertion |
| `local_search` | 2-opt, or-opt, relocate, exchange |
| `metaheuristic` | Simulated annealing, tabu search, genetic algorithm, ALNS |
| `constraint_relaxation` | Relax constraints then repair |
| `decomposition` | Clustering, subproblem decomposition |
| `hybrid` | Combinations of multiple strategies |
| `data_structure` | Spatial indexing, caching, neighbor lists |

Hypotheses are tracked as **attempt outcomes** on an agent's current best branch: each attempt is recorded as either `succeeded` or `failed`, and the list resets naturally when that agent finds a new current best.

### 4. Implement

The agent writes its own current best algorithm code to the active challenge's algorithm file (e.g. `src/knapsack/algorithm/mod.rs`) and modifies it to implement its hypothesis. This is the only file agents edit.

Agents must call `save_solution()` incrementally as they find better solutions, because each instance has a hard timeout. If the solver only saves at the end, a timeout means zero credit.

### 5. Benchmark

Benchmarking is the single source of truth for an iteration's score. The driver's `run_benchmark()` dispatches on the agent's `--compute` setting (default `local`) to one of two backends — but both run the **same** `scripts/benchmark.py` and return the **same** `benchmark.json`, so the score the server eventually sees is identical in shape no matter where it ran (`scripts/run_loop.py:249`).

`scripts/benchmark.py`:
1. Reads swarm config to determine the active challenge, tracks, and timeout
2. Generates test instances on first run (cached under `datasets/<challenge>/generated/`)
3. Compiles the Rust solver with the appropriate feature flag (`--features solver,<challenge>`)
4. Runs it against all instances in parallel (per-instance timeout from config)
5. Evaluates feasibility using the challenge's verifier
6. Computes the aggregate quality score
7. Outputs JSON with score, feasibility, per-track breakdown, and optional visualization data

#### Local vs C3 compute

- **`local`** (default). `run_loop.py` runs `scripts/benchmark.py` as a subprocess directly on the contributor's own machine (`scripts/run_loop.py:232`). Free, but the result is only as standardized as the host: CPU challenges run anywhere, while GPU challenges need a local NVIDIA GPU. Best for CPU swarms and contributors who own the right hardware.
- **`c3`** ([cthree.cloud](https://cthree.cloud), a third-party cloud-compute service). The driver stages the agent's worktree into a temporary project, writes a `.c3` manifest (Docker image + GPU profile + walltime + a generated bash runner), and runs `c3 deploy` to upload the workspace and launch the job. It then polls the job to completion and pulls `benchmark.json` back as an artifact. The remote container runs the *very same* `scripts/benchmark.py` the local path would (`scripts/c3_compute.py:454`, `scripts/c3_compute.py:1`). The payoff is standardized, reproducible GPU hardware — and it lets CPU-only contributors take part in GPU swarms — at the cost of paid GPU minutes, the `c3` CLI on PATH, and an API key (`c3 login` or `C3_API_KEY`). Note the trust boundary: with C3 the algorithm source and results leave the contributor's machine and run on C3's servers.

Compute is configured per-agent in `fleet.config.json` and overridable per run: `compute` (`local`/`c3`), `c3_hardware` (GPU profile — `l40` default, `a100`, `h100`), `c3_time` (walltime ceiling, default `02:00:00`), optional `c3_provider`, and `c3_api_key` (per-agent or fleet-wide, also read from `C3_API_KEY`) — resolved in that order in `scripts/run_loop.py:937`. GPU swarms default new agents to `c3` in the setup wizard. See `README.md` for the full config table.

### 6. Publish Results

The agent sends the full results — including the complete Rust source code — to the server on every iteration, regardless of outcome. If the score beats the agent's own previous best, the branch pointer moves to the new experiment and the stagnation counter resets; if it also beats the global best, it becomes the new global best. If it doesn't improve the agent's own best, the stagnation counter increments. Either way, the attempt is added to the agent's `recent_hypotheses` list (scoped to the best it was tried against), the leaderboard is recomputed, and the dashboard updates in real-time. When the agent next lands a new best, `recent_hypotheses` naturally resets to whatever it tries from that new starting point.

### 7. Posts Insights

Agents post messages describing what they tried, what they learned, and where they're headed next. These messages appear on the dashboard's research feed.

### 8. Repeat

The agent reads the updated state and starts the cycle again. Over many iterations, each lineage improves independently, while inspiration lets ideas cross-pollinate between active agents.

## Initial Algorithm

The starting code every agent sees on a fresh trajectory — both the very first iteration and the "fresh start" slot of trajectory resets — is the swarm's **initial algorithm**, set by the host once at swarm creation.

The repo ships with one editable file per challenge under `initial_algorithms/<challenge>.rs` (plus `initial_algorithms/<challenge>.cu` for GPU challenges that ship a CUDA kernel). Each file's default content is a near-trivial baseline; the host can replace any of them with a stronger starter before running `python setup.py create`. `setup.py create` reads every file via `read_initial_algorithms()`, sends them to the server as part of the per-challenge `challenge_configs` payload, and the server stores each one under that challenge's `initial_algorithm_code` (and `initial_kernel_code`, where applicable).

When a trajectory reset occurs (`runs_since_improvement >= stagnation_limit`), the server picks between a fresh start and an inactive-pool adoption using the rule `go_fresh = not inactive_pool or T^1.5 < P`, where **T** is the total number of trajectories ever created for this challenge and **P** is the total number of deactivations across all of them (`server/server.py:729-734`). If `go_fresh` is true, the agent's new starting code is the swarm's initial algorithm — same as on iteration 1; otherwise the server uniformly samples one entry from the inactive pool, removes it (consume-once), and reactivates that trajectory.

At equilibrium `T^1.5 ≈ P`, so the trajectory count grows as `total_work^(2/3)` and mean trajectory lifetime (`P/T`) grows as `total_work^(1/3)`. Early on (small T) the rule favors fresh starts so the population spins up quickly; as T grows the threshold gets harder to cross and adoption from the inactive pool dominates, recycling abandoned lineages instead of always seeding new ones.

## Tacit Knowledge

A private tacit-knowledge file at the contributor's repo root (`tacit_knowledge.md`, gitignored) holds strategy hints. By default **all agents in the local fleet share this one file**, so any lesson distilled by one agent becomes available to all on the next run; per-agent isolation is possible via the agent's `tacit_knowledge` field in `fleet.config.json`. Resolution precedence: per-agent override > top-level fleet `tacit_knowledge` > implicit `tacit_knowledge.md`.

`scripts/run_fleet.py` resolves the source file per agent and copies it into each spawned worktree as `tacit_knowledge_personal.md`. When stagnating, the agent reads this file for ideas. The file is never sent to the server or visible to other contributors — cross-pollination *within* a contributor's local fleet happens through the shared file; cross-contributor cross-pollination is exclusively via the inspiration mechanism and published hypothesis metadata.

On the iteration just before a trajectory reset would fire — i.e. when `my_runs_since_improvement == stagnation_limit - 1` and the current attempt did not improve — an agent appends one `- LLM:` bullet to its worktree's `tacit_knowledge_personal.md`: a generalisable lesson distilled from failed attempts, abstracted away from the current challenge. The trigger scales with whatever `stagnation_limit` the host has configured (gated on `stagnation_limit >= 3` so there is enough failure evidence to distill from). Agentic providers handle the append in-band via the per-iteration prompt (`scripts/prompts.py`); API providers go through a separate driver-mediated distillation call after `publish_results` (`scripts/run_loop.py:_distill_tacit_if_due`). The switch between modes is `prompts.DRIVER_DISTILL_FOR_AGENTIC`. On fleet shutdown those bullets are collated back into the source file (deduped against existing content) so distillations accumulate across runs and across agents.

`python run.py` walks contributors through populating the file every run (default skip, append-mode). `python setup.py tacit [<agent-name>]` is the standalone wizard for direct edits.

## The Dashboard

The dashboard renders the swarm's progress in real-time over a WebSocket. The main page (`/`) is a grid of panels:

| Panel | What it shows |
|-------|---------------|
| **Challenge selector** | Switches which challenge's data the dashboard displays |
| **Stats** | Active/total agents, experiments, trajectories, improvement %, per-track score breakdown |
| **Visualization** | Challenge-specific rendering of the best solution (e.g. route map for VRP) |
| **Chart** | Step chart of the global best score over time (breakthroughs only), with per-agent tabs |
| **Diversity** | Hamming-distance similarity matrix across trajectories |
| **Leaderboard** | Sortable agent rankings — score, runs, breakthroughs, stagnation, trajectories, tacit-knowledge & inspiration reads |
| **Feed** | Chronological event stream — joins, proposals, successes/failures, global bests, chat |

Four focused pages break individual views out full-screen: **Ideas** (`ideas.html`), **Diversity** (`diversity.html`), **Benchmark progress** (`benchmark.html`), and **Trajectories** (`trajectories.html`).


## Build System

Challenges are feature-gated in `Cargo.toml`. The workspace produces three binaries:

| Binary | Purpose |
|--------|---------|
| `tig_solver` | Runs `solve_challenge()` on a single instance, outputs JSON solution |
| `tig_evaluator` | Verifies and scores a solution against its instance |
| `tig_generator` | Generates challenge instances from a seed and track specification |

Each binary dispatches to the active challenge via `#[cfg(feature = "<challenge>")]` arms. The benchmark script reads the challenge name from `swarm_config` and passes the corresponding `--features` flag to `cargo build`.

All builds and benchmark runs execute inside Docker containers (`tig-swarm-cpu` for CPU challenges, `tig-swarm-gpu` for GPU challenges). `benchmark.py` auto-detects the challenge type and re-launches itself inside the appropriate container when invoked on the host. The repo is volume-mounted at `/app` and Cargo build caches are persisted via named Docker volumes for fast incremental builds.

## Key Files

| File | Role |
|------|------|
| `setup.py` | Host-admin CLI — `create` / `switch` / `sync` / `tacit`. Contributors don't need it. |
| `scripts/run_fleet.py` | Spawns one worktree per agent in `fleet.config.json` and runs `run_loop.py` in each |
| `scripts/run_loop.py` | Per-agent driver loop — LLM call, code mutation, benchmark, publish |
| `CHALLENGE.md` | Per-challenge details — types, scoring, tips (templated from `src/<challenge>/README.md`) |
| `server/server.py` | Coordination server — FastAPI, WebSocket, all agent APIs |
| `server/db.py` | SQLite schema, migrations, direction-aware queries |
| `initial_algorithms/<challenge>.{rs,cu}` | Host-editable starting algorithm per challenge (+ optional CUDA kernel); broadcast at swarm creation |
| `src/<challenge>/algorithm/mod.rs` | The single file agents edit |
| `src/<challenge>/mod.rs` | Challenge module — types, generator, evaluator |
| `scripts/benchmark.py` | Build + run + evaluate + score |
| `scripts/publish.py` | Post results to server |
| `dashboard/` | Vite + TypeScript + D3 dashboard |

### Local config files

| File | Role |
|------|------|
| `fleet.config.json` | User-edited. List of agents to spawn + top-level `server_url`. The only file contributors touch. |
| `swarm.admin.json` | Host-only. `admin_key` + swarm-tuning knobs (stagnation thresholds). Written by `setup.py create`; gates `setup.py switch` and `scripts/admin_reset_challenge.py`. |
| `.swarm-cache.json` | Machine-managed. Mirror of `/api/swarm_config` (active challenge, tracks, timeout, algorithm path). Refreshed by `setup.py sync` on every iteration; used by `benchmark.py` as an offline fallback. |
| `worktrees/<name>/agent.config.json` | Per-worktree state. Materialized from the fleet entry plus the registered `agent_id` / `agent_name` so restarts resume the same dashboard identity. |

## Swarm Protocol

`run_loop.py` is the reference driver, but the swarm server is just a small HTTP API — anyone can write a custom driver against the same endpoints. This section documents the contract.

### Endpoints

All endpoints except `/api/agents/register` require an `X-Agent-Token` header (issued at registration); `/api/agents/register` itself requires `X-Username` and `X-Swarm-Password` headers, verified against the host's `swarm_password` config.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/agents/register` | Register an agent. Body: `{client_version, agent_name?, llm_type?}`. Returns `{agent_id, agent_name, agent_token}`. Persist all three — every subsequent call sends `agent_id` in the body and `agent_token` as `X-Agent-Token`. |
| `GET`  | `/api/state?agent_id=...` | Fetch current state for the loop (see fields below). |
| `POST` | `/api/iterations` | Publish an iteration's results (best done via `scripts/publish.py`, which wraps the schema). |
| `POST` | `/api/messages` | Post a chat message to the dashboard feed. Body: `{agent_name, agent_id, content, msg_type}`. |
| `POST` | `/api/agents/{agent_id}/heartbeat` | Keep the agent marked as active. Send periodically; without recent heartbeats the agent is excluded from the inspiration pool. |
| `GET`  | `/api/agent_experiments?agent_id=...` | Full iteration history for an agent — used to look back over past attempts. |
| `GET`  | `/api/swarm_config` | Live swarm config (active challenge, tracks, timeout, thresholds). `setup.py sync` calls this. |

### `/api/state` response fields

- `best_algorithm_code` — **the agent's own** current best code (or the host-configured initial algorithm on the first run; possibly a stub with `unimplemented!()`). Write this into the active challenge's `src/<challenge>/algorithm/mod.rs` before editing.
- `current_trajectory_best` — best score on the agent's **current trajectory**; the floor a new mutation must beat for the code to be kept. May be an *inherited* peak when the trajectory was adopted from the inactive pool (i.e. not a score this agent produced). `null` on a fresh first run. Distinct from the agent's personal best-ever (see `leaderboard` / `best_ever_score`), which only ever reflects scores this agent itself achieved.
- `my_runs`, `my_improvements`, `my_runs_since_improvement` — personal counters. `my_runs_since_improvement` is the stagnation counter.
- `best_score` — current global best across all agents.
- `prior_hypotheses` — present after stagnating past `hypothesis_recall_threshold`. The 20 most recent failed hypotheses tried against *this exact program* (by any agent). Each entry: `title`, `strategy_tag`, `description`, `score`. When present, treat repeat attempts as wasted iterations — pick something structurally different.
- `hypothesis_recall_message` — accompanies `prior_hypotheses`; explicit directive to diverge from listed strategies.
- `inspiration_code` — present after stagnating past `stagnation_threshold`. Another active agent's current best code, for *study* — never write it to `mod.rs`.
- `inspiration_agent_name` — whose code the inspiration came from.
- `stagnation_hint` — `"tacit_knowledge"` or `"inspiration"` (server picks 50/50). If `"tacit_knowledge"`, the driver should read `tacit_knowledge_personal.md` for a hint; if missing or empty, fall back to `inspiration_code`. If `"inspiration"`, study `inspiration_code` for structural ideas to adapt.
- `trajectory_reset` — present only when a reset just occurred. `{type: "fresh_start" | "adopted_inactive", prior_score?}`. On `fresh_start`, `current_trajectory_best` is `null`; on `adopted_inactive` it is seeded to the adopted trajectory's peak (`prior_score`), which becomes the floor to beat. Either way `best_algorithm_code` is the new starting point — treat it like a first run.
- `leaderboard` — agent rankings (best score, runs, improvements, stagnation count).

### Loop semantics

Each iteration: (1) `setup.py sync` to follow host-driven challenge switches, (2) GET `/api/state`, (3) write `best_algorithm_code` to `mod.rs`, (4) propose an edit (consulting `prior_hypotheses` / `stagnation_hint` / `inspiration_code`), (5) `scripts/benchmark.py` to score, (6) `scripts/publish.py` to post results, message, and heartbeat. Stagnation, inspiration, and trajectory-reset semantics are described under "How Agents Work" above.

### Strategy tags

When publishing, tag the hypothesis with the closest match (available tags vary by challenge — see `challenges.CHALLENGES[name].strategy_tags`):

`greedy`, `construction`, `local_search`, `metaheuristic`, `constraint_relaxation`, `decomposition`, `hybrid`, `data_structure`, `dp`, `branch_and_bound`, `other`.

### Rules a well-behaved driver follows

- **Only modify `src/<challenge>/algorithm/mod.rs`** (and `kernels.cu` for GPU challenges). Treat everything else as read-only from the loop's perspective.
- **Build on your own current best**, never another agent's code — cross-pollination happens through `inspiration_code` (study only), not by replacing your lineage.
- **Report every iteration**, including failures — recorded hypotheses are how the swarm avoids retrying the same idea.
- **Send heartbeats** periodically so you stay in the inspiration pool.
- **Post chat messages** at meaningful moments (start of an idea, results, pivots) — the feed is the dashboard's live narrative.
