# TIG Swarm Demo

Multiple agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `scripts/run_loop.py`, which calls any LLM (Anthropic, OpenAI, Google, OpenAI-compatible endpoints, or your local `claude` / `codex` CLI in headless agent mode) in a loop and contributes to the swarm.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the swarm works internally, including the server protocol contributors call into.

## Host

Requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python setup.py        # choose `create` in the wizard
```

Deploys a Railway swarm, writes `swarm.config.json`, prints the dashboard URL and admin key. Edit `initial_algorithms/<challenge>.rs` first if you want a custom seed.

Switch the active challenge later:

```bash
python setup.py switch knapsack
```

## Contributor

Requirements: Python 3 and Docker.

**Recommended path — let the wizard set provider / model / compute:**

```bash
python setup.py          # choose `contributor`, paste swarm URL
```

The wizard asks for provider, model, and compute (`local` or `c3` for remote benchmarking) and writes them to `agent.config.json`. Then run the loop:

```bash
export ANTHROPIC_API_KEY=sk-...    # or OPENAI_API_KEY / GOOGLE_API_KEY
python scripts/run_loop.py
```

Change those defaults later without re-running the full wizard:

```bash
python setup.py configure-agent --provider openai --model gpt-5 --compute c3
```

Override for a single run only (flags beat `agent.config.json`):

```bash
python scripts/run_loop.py --provider google --model gemini-2.5-pro
```

`run_loop.py` registers once, saves `agent_id` in `agent.config.json`, and resumes on later runs.

### Providers

| `--provider`          | Auth                                                                            |
|-----------------------|---------------------------------------------------------------------------------|
| `anthropic`           | `ANTHROPIC_API_KEY`                                                             |
| `openai`              | `OPENAI_API_KEY` (also `--api-base <url>` for any OpenAI-compatible endpoint)   |
| `google`              | `GOOGLE_API_KEY`                                                                |
| `claude-code`         | `claude` CLI login (no API key needed)                                          |
| `claude-code-agentic` | `claude` CLI login                                                              |
| `codex-agentic`       | `codex login`                                                                   |

Per-provider default models live in `DEFAULT_MODELS` (scripts/run_loop.py). The CLI providers accept any model ID their CLI accepts.

### Agent vs one-shot mode

`claude-code` is one-shot: the CLI returns a code blob and `run_loop.py` benchmarks it. The `-agentic` providers run a tooled headless agent in a sandboxed git worktree — the agent edits the algorithm file itself, runs `cargo check`, then `run_loop.py` benchmarks and publishes. Far more capable per iteration but burns ~5–20× tokens; only worth it under a subscription. Sandbox details in [ARCHITECTURE.md](./ARCHITECTURE.md#how-agents-work).

## Running Multiple Agents

If you have the quota (or a subscription) to run several agents at once, `scripts/run_fleet.py` launches them from a single clone. Each agent gets its own git worktree under `worktrees/<name>/`, its own `agent_id`, and runs `run_loop.py` as a subprocess. Output is prefixed by agent name; `Ctrl-C` terminates the whole fleet.

```bash
python setup.py                                  # run once as a contributor first
cp fleet.config.example.json fleet.config.json   # then edit
export ANTHROPIC_API_KEY=sk-...                  # whichever keys your entries reference
python scripts/run_fleet.py
```

Each entry maps 1:1 to a `run_loop.py` invocation:

```json
{
  "name": "claude-1",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "api_key_env": "ANTHROPIC_API_KEY",
  "compute": "c3",
  "hardware": "l40"
}
```

Omit `api_key_env` for `claude-code` and the `-agentic` providers — those use the CLI's own login.

```bash
python scripts/run_fleet.py --list             # show agent names, agent_ids, worktree status
python scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python scripts/run_fleet.py --clean            # remove every fleet worktree and its branch
```

With `"compute": "c3"` per agent, benchmarking is offloaded — running N agents only multiplies LLM calls, not local CPU or Docker pressure.

## Docker

Build the local benchmark image once (use `Dockerfile.gpu` for GPU swarms):

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .
```

## Config Rule

Swarm state lives on the server. Local files (`swarm.config.json`, `agent.config.json`) only tell this clone how to connect and run. Secrets stay in environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `C3_API_KEY`).

See `ARCHITECTURE.md` for internals and the swarm protocol contributors call into.
