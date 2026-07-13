# Contributing

Thanks for your interest in improving the TIG swarm demo! This guide covers
working **on** the repo itself. If you just want to run agents and contribute
solver improvements to a swarm, see [README.md](./README.md) — that needs no
dev setup at all.

## Repo layout

| Path | What | Stack |
|------|------|-------|
| `src/` | Rust solver framework — feature-gated, one module per challenge | Rust |
| `initial_algorithms/` | Per-challenge starting code (`<ch>/stub/`) + authored seed pool (`<ch>/seeds/`) | Rust |
| `scripts/` | Orchestration: per-agent loop, fleet, benchmarking, publishing | Python |
| `server/` | Coordination server (scores, config, leaderboard, WebSocket) | Python / FastAPI |
| `dashboard/` | Web UI for the server | TypeScript / Vite |
| `control-ui/` | Setup/onboarding UI (host create, contributor join, admin console) | Svelte / Vite |
| `run.py` | Contributor entry point | Python |
| `setup.py` | Host-admin CLI (`create` / `switch` / `sync` / `tacit`) — **not** Python packaging; thin dispatcher over `hostadmin/` | Python |
| `hostadmin/` | Implementation package behind `setup.py` (Railway, config I/O, invite/revoke, tacit wizard) | Python |
| `control_server.py` | Local companion server behind `run.py --ui` | Python / FastAPI |
| `docs/` | Long-form internals (start with `ARCHITECTURE.md`) | — |

## Dev setup & tests

**Rust** — `src/<challenge>/algorithm/` is gitignored (the swarm overwrites
it at runtime), so on a fresh clone seed it first:

```bash
python3 scripts/seed_algorithms.py          # copies initial_algorithms/ starting code in
cargo check --features solver,knapsack      # then per-challenge checks work
```

Challenge features: `satisfiability`, `vehicle_routing`, `knapsack`,
`job_scheduling`, `energy_arbitrage`, `hypergraph`, `neuralnet_optimizer`,
`vector_search`. Every binary needs at least one challenge feature enabled.
The last three are GPU challenges and need the CUDA toolkit (`nvcc`) to build.

**Python scripts / host CLI** — stdlib only, nothing to install.

**Server**:

```bash
cd server && pip install -r requirements.txt
uvicorn server:app          # DATA_DIR sets the SQLite location
```

**Dashboard**:

```bash
cd dashboard && npm install && npm run dev
```

**Control UI** (the prebuilt `control-ui/dist/` is committed so contributors
don't need Node; rebuild it when you change `control-ui/src/`):

```bash
cd control-ui && npm install && npm run build
```

### Running the tests

There is no pytest. Python `test_*.py` files are self-running:

```bash
for f in scripts/test_*.py; do python3 "$f"; done
pip install -r server/requirements.txt -r control-ui-requirements.txt
for f in server/test_*.py control_server_test.py; do python3 "$f"; done
```

The dashboard and control UI use vitest:

```bash
cd dashboard && npm test
cd control-ui && npm test
```

Please run the tests touching your area before opening a PR, and add a
self-running `test_*.py` (or vitest case) alongside any behavior change.

## Things that look wrong but aren't

- `Cargo.toml` / `Cargo.lock` must stay at the repo root (workspace root; the
  swarm sandbox anchors here).
- `.dockerignore`, `.ignore`, `railway.toml` are root-pinned by their tools.
- `setup.py` is a CLI, not packaging — never `pip install` it. It's a thin
  dispatcher over the root-level `hostadmin/` package, and doubles as the
  back-compat import surface (`import setup` still exposes every helper).
- `src/lib.rs`'s `extern crate self as tig_challenges;` is load-bearing: it
  lets one algorithm file compile both here and in the TIG-docker slot.
- `control-ui/dist/` is committed on purpose (contributors run the companion
  UI without Node).
- Do **not** add a `CLAUDE.md` under `src/` — swarm agents work inside
  `src/<challenge>/` and would auto-load it, polluting their context.

## Pull requests

- Branch from `main`; keep PRs focused on one change.
- Describe what changed and how you verified it (test run, manual flow).
- CI must pass: Python self-tests, `cargo check` for the CPU challenges, and
  dashboard vitest.
