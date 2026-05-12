# TIG Swarm Demo

Multiple agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `scripts/run_loop.py`, which calls any LLM (Anthropic, OpenAI, Google, OpenAI-compatible endpoints, or your local `claude` CLI) in a loop and contributes to the swarm.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the swarm works internally, including the server protocol contributors call into.

## Host

Requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python setup.py
```

Choose `create` in the wizard. It deploys a new Railway swarm, writes local `swarm.config.json`, and prints the dashboard URL plus admin key. Edit `initial_algorithms/<challenge>.rs` before creating if you want agents to start from a custom seed.

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
python setup.py
export ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY / GOOGLE_API_KEY
python scripts/run_loop.py
```

Choose `contributor` in the wizard and paste the swarm URL when asked. Setup writes:

- `swarm.config.json`: swarm URL, active challenge, tracks, timeouts, paths.
- `agent.config.json`: local provider/model/compute defaults. No API keys are stored.

`run_loop.py` registers once, saves `agent_id` in `agent.config.json`, and resumes automatically on later runs.

Or use your local `claude` CLI in headless mode — auth comes from your Claude Code login (OAuth / subscription), no `ANTHROPIC_API_KEY` needed:

```bash
python scripts/run_loop.py --provider claude-code --model claude-opus-4-7
```

Each iteration shells out to `claude -p` from a temp directory so the CLI's `CLAUDE.md` auto-discovery doesn't inject anything from this repo into the system prompt — `run_loop.py` supplies its own. Trade-offs vs the API providers: per-call latency is higher (subprocess startup), and the dashboard's cost column reads $0 because the CLI doesn't surface token usage.

## Fully Scripted Setup

Flags skip prompts, so setup can be instant:

```bash
python setup.py \
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

See `ARCHITECTURE.md` for internals and the swarm protocol contributors call into.
