# TIG Swarm Demo

Multiple AI agents collaboratively optimize TIG challenges in Rust, sharing results through a coordination server with a live dashboard.

Run as an **agent** (Claude Code, Codex, Gemini CLI — anything that reads `CLAUDE.md`) or as a **script** (`scripts/run_loop.py`, which calls any LLM API).

Supports 7 challenges: **Satisfiability**, **Vehicle Routing**, **Knapsack**, **Job Scheduling**, **Energy Arbitrage**, **Hypergraph** (GPU), **Neural Net Optimizer** (GPU).

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the swarm works internally. See [CLAUDE.md](./CLAUDE.md) for the agent's runtime instructions.

## Quick start

### Host a swarm

Need: Python 3, a free [Railway](https://railway.com) account, and the Railway CLI.

**1. Install the Railway CLI** (pick one):
```bash
brew install railway          # macOS with Homebrew
npm i -g @railway/cli         # Node.js
cargo install railwayapp --locked  # Rust/Cargo
```

**2. Log in:**
```bash
railway login
```
This opens a browser — complete the OAuth flow there.

**3. Provision and deploy:**
```bash
git clone <repo>
cd tig-swarm-demo
python setup.py create
```

The wizard provisions a Railway service, deploys, and prints your **swarm URL** (share with contributors) and **admin key** (also saved to `swarm.config.json`).

### Join a swarm

Need: Python 3, Docker.

```bash
git clone <repo>
cd tig-swarm-demo
python setup.py join <swarm-url>
```

Then pick one:

**Agent mode** — open Claude Code (or any coding agent) here and tell it:
> Read CLAUDE.md and start contributing to the swarm.

To run benchmarks on C3 cloud GPUs instead of local Docker, set these env vars first:
```bash
export TIG_COMPUTE=c3
export C3_API_KEY=c3_key_...
export C3_HARDWARE=l40          # GPU type (default: l40)
```
Then start the agent as normal — `benchmark.py` routes to C3 automatically.

**Script mode** — needs an LLM API key:
```bash
export ANTHROPIC_API_KEY=sk-...    # or OPENAI_API_KEY, GOOGLE_API_KEY
python scripts/run_loop.py --provider anthropic
```
Run `python scripts/run_loop.py --help` for other providers, OpenAI-compatible endpoints, and resuming an existing agent.

To run each benchmark on C3 GPU compute instead of local Docker:
```bash
export ANTHROPIC_API_KEY=sk-...
export C3_API_KEY=c3_key_...
python scripts/run_loop.py --provider anthropic --compute c3 --hardware l40
```
In C3 mode the LLM loop stays local. Each candidate algorithm submits one C3 Docker benchmark job, waits for the benchmark JSON, then publishes back to the swarm server.
If you already ran `c3 login`, `C3_API_KEY` is optional.

> One clone = one swarm. To join a second swarm, clone again into a separate directory.

## Dashboard

Open the swarm URL in a browser. Hotkeys: `1` main / `2` ideas / `Q` QR code / `R` replay. Other pages: `/ideas.html`, `/diversity.html`, `/benchmark.html`.

## Setup commands

| Command | Who | What |
|---|---|---|
| `setup.py create` | Host | Provision a new swarm on Railway |
| `setup.py join <url>` | Contributor | Point this clone at an existing swarm |
| `setup.py switch <challenge>` | Host | Change the active challenge for everyone (per-agent state is preserved per-challenge) |
| `setup.py sync` | Contributor | Re-template if the active challenge changed (the agent loop runs this automatically) |

## Seeding the initial algorithm

Every agent starts a fresh trajectory from the same seed: `initial_algorithms/<challenge>.rs` (one per challenge). Default is a stub with `unimplemented!()` — edit these files *before* `setup.py create` to seed the swarm with working baselines. GPU challenges also have `initial_algorithms/<challenge>.cu` for CUDA kernels.
