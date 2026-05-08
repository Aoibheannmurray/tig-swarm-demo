# TIG Swarm Demo

Multiple AI agents collaboratively optimize TIG challenges in Rust, sharing results through a coordination server with a live dashboard.

Run as an **agent** (Claude Code, Codex, Gemini CLI — anything that reads `CLAUDE.md`) or as a **script** (`scripts/run_loop.py`, which calls any LLM API).

Supports 5 challenges: **Satisfiability**, **Vehicle Routing**, **Knapsack**, **Job Scheduling**, **Energy Arbitrage**.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the swarm works internally. See [CLAUDE.md](./CLAUDE.md) for the agent's runtime instructions.

## Quick start

### Host a swarm

Need: [Railway](https://railway.com) account, [Railway CLI](https://docs.railway.com/guides/cli), Python 3.

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

**Script mode** — needs an LLM API key:
```bash
export ANTHROPIC_API_KEY=sk-...    # or OPENAI_API_KEY, GOOGLE_API_KEY
python scripts/run_loop.py --provider anthropic
```
Run `python scripts/run_loop.py --help` for other providers, OpenAI-compatible endpoints, and resuming an existing agent.

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

Every agent starts a fresh trajectory from the same seed: `initial_algorithm.rs` at the repo root. Default is a stub with `unimplemented!()` — edit this file *before* `setup.py create` to seed the swarm with a working baseline. Changing it later requires re-creating the swarm.
