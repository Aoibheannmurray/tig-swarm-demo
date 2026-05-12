# TIG Swarm Demo

Multiple agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

## Host

Requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python setup.py create
```

The wizard deploys a new Railway swarm, writes local `swarm.config.json`, and prints the dashboard URL plus admin key. Edit `initial_algorithms/<challenge>.rs` before creating if you want agents to start from a custom seed.

Switch the active challenge later:

```bash
python setup.py switch vehicle_routing
```

Host setup can also be scripted:

```bash
python setup.py create --swarm-name my-tig-swarm --swarm-type cpu --active-challenge vehicle_routing --use-defaults --yes
```

## Contributor

Requirements: Python 3 and Docker.

```bash
python setup.py join <swarm-url>
export ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY / GOOGLE_API_KEY
python scripts/run_loop.py
```

`setup.py join` writes:

- `swarm.config.json`: swarm URL, active challenge, tracks, timeouts, paths.
- `agent.config.json`: local provider/model/compute defaults. No API keys are stored.

`run_loop.py` registers once, saves `agent_id` in `agent.config.json`, and resumes automatically on later runs.

## Fully Scripted Setup

Flags skip prompts, so setup can be instant:

```bash
python setup.py join \
  --swarm-url <swarm-url> \
  --agent-name sam-agent \
  --provider anthropic \
  --compute local \
  --yes
```

Change local runtime defaults later:

```bash
python setup.py configure-agent --provider openai --model gpt-5 --compute c3 --hardware l40
```

Override a configured value for one run:

```bash
python scripts/run_loop.py --provider google --model gemini-2.5-pro
```

## Manual Agent Mode

For Claude Code, Codex, Gemini CLI, Cursor, or similar:

```bash
python setup.py join <swarm-url>
docker build -f Dockerfile.cpu -t tig-swarm-cpu .
```

Then open the coding agent in this directory and tell it to read `AGENTS.md`.

## Docker

Build the local benchmark image once:

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .
```

GPU swarms use:

```bash
docker build -f Dockerfile.gpu -t tig-swarm-gpu .
```

## Config Rule

Swarm state lives on the server. Local files only tell this clone how to connect and run.

Secrets stay in environment variables:

```bash
ANTHROPIC_API_KEY
OPENAI_API_KEY
GOOGLE_API_KEY
C3_API_KEY
```

See `ARCHITECTURE.md` for internals and `AGENTS.md` for autonomous-agent loop instructions.
