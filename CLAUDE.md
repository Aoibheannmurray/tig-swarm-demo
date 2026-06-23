# CLAUDE.md — developer guide

Context for working **on** this repo (editing the code). For how the running
system behaves, see [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md).

## What this is

A self-hostable "swarm" that runs many AI coding agents in parallel to improve
Rust solvers for TIG challenges. A host stands up a coordination server; each
contributor runs a fleet of agents locally; the server tracks scores and shares
hints/inspiration across the swarm.

## Layout

| Path | What | Stack |
|------|------|-------|
| `src/` | The Rust solvers — feature-gated, one per challenge | Rust |
| `scripts/` | Orchestration: per-agent loop, fleet, benchmarking, publishing | Python — see `scripts/CLAUDE.md` |
| `server/` | Coordination server (scores, config, leaderboard, WebSocket) | Python/FastAPI — see `server/CLAUDE.md` |
| `dashboard/` | Web UI for the server | TypeScript/Vite — see `dashboard/CLAUDE.md` |
| `initial_algorithms/` | Editable per-challenge seed algorithms | Rust |
| `docs/` | Long-form internals (`ARCHITECTURE.md`, …) | — |
| `run.py` | **Contributor** entry point (`python3 run.py`) | Python |
| `setup.py` | **Host-admin CLI** (`create`/`switch`/`sync`/`tacit`) — NOT packaging | Python |

## Build & test

- **Rust solver** (per challenge): `cargo check --features solver,<challenge>`.
  Challenges: `satisfiability`, `vehicle_routing`, `knapsack`, `job_scheduling`,
  `energy_arbitrage`, `hypergraph`, `neuralnet_optimizer`, `vector_search`.
  Every binary needs at least one challenge feature enabled.
- **Python scripts / host CLI**: stdlib only, no install. Worker Docker deps are
  in the root `requirements.txt`.
- **Server**: `cd server && pip install -r requirements.txt && uvicorn server:app`
  (`DATA_DIR` sets the SQLite location).
- **Dashboard**: `cd dashboard && npm install && npm run dev` (build:
  `npm run build`).

**Tests:** there is no pytest in this repo. Python `test_*.py` files (under
`scripts/` and `server/`) are self-running — execute them directly, e.g.
`python server/test_infeasible_floor_trap.py`. The dashboard uses vitest:
`cd dashboard && npm test`.

## Root constraints — don't "tidy" these

- `Cargo.toml` / `Cargo.lock` must stay at root (workspace root; every `src/`
  crate path and the swarm sandbox anchor here).
- `.dockerignore`, `.ignore`, `railway.toml` are root-pinned — Docker honors
  `.dockerignore` only at the build-context root; Railway only reads `.ignore`
  and `railway.toml` at root.
- `setup.py` is the host-admin CLI, **not** Python packaging — don't `pip
  install` it. It runs as `python setup.py <subcommand>` and is called by
  subprocess from `scripts/run_loop.py` / `scripts/run_fleet.py`.

## Editing note — the swarm overwrites CLAUDE.md in worktrees

The swarm runs each coding agent in a git worktree and writes its **own**
generated `CLAUDE.md` into that worktree root every iteration (see
`scripts/agentic_backends.py`). Therefore:

- This root `CLAUDE.md` is the **developer** guide; the swarm's generated one is
  separate and only exists inside throwaway worktrees.
- **Do NOT add a `CLAUDE.md` under `src/`.** A swarm agent works inside
  `src/<challenge>/` and would auto-load it into its context, polluting the
  optimization. Keep dev guidance in this root file plus the
  `scripts/` / `server/` / `dashboard/` files — directories swarm agents never read.
