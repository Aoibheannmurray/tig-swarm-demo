# TIG Swarm Demo

Multiple agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `python run.py`, which spawns one or more agents — each calling any LLM (Anthropic, OpenAI, Google, OpenAI-compatible endpoints, or your local `claude` / `codex` CLI in headless agent mode) in a loop and contributing to the swarm.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the swarm works internally, including the server protocol contributors call into.

## Host

Requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python setup.py create
```

Deploys a Railway swarm, prints the dashboard URL and admin key, and scaffolds a starter `fleet.config.json` so you can immediately participate. Edit `initial_algorithms/<challenge>.rs` first if you want a custom seed.

Switch the active challenge later:

```bash
python setup.py switch knapsack
```

`setup.py` is host-only. Contributors do not need it — they run `python run.py`.

## Contributor

Requirements:
- Python 3
- [Docker](https://www.docker.com/products/docker-desktop/) — benchmarks run inside a local Docker container. Install Docker Desktop and make sure it's running (`docker info` should succeed) before launching the fleet. On Windows, Docker Desktop also requires WSL 2; its installer will prompt you.
- Credentials for whichever LLM provider you choose (Anthropic, OpenAI, Google, etc.).

Don't have a terminal handy? Open this repo in [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://docs.claude.com/en/docs/claude-code) — both auto-discover `AGENTS.md` / `CLAUDE.md` and will walk you through setup.

**One command for the whole flow:**

```bash
python run.py
```

What it does, in order:

1. Checks `docker` is on PATH.
2. **First time only** — runs the setup wizard to create `fleet.config.json`. You'll be asked for the three values the host shared (`server_url`, `username`, `swarm_password`), which LLM provider/model to use, and how many agents to run (default: 1). The wizard never writes API keys to disk — it tells you what to export. On subsequent runs this step is skipped automatically.
3. Asks whether you'd like to add or edit tacit knowledge for your agent(s) (default **No**). If yes, walks you through a short guided capture; answers are **appended** to your existing notes rather than overwriting them. See [Tacit knowledge](#tacit-knowledge) below.
4. Launches the fleet: each agent gets its own git worktree under `worktrees/<name>/`, its own `agent_id`, and runs `scripts/run_loop.py` as a subprocess. Output is prefixed by agent name; `Ctrl-C` terminates the whole fleet. `agent_id` is persisted per worktree so restarts resume the same dashboard identity.
5. On shutdown, syncs any `- LLM:` lessons the agent appended in its worktree back to the source tacit file so they survive across runs.

**Export the API key(s) your entries reference** (any time before launch):

```bash
export ANTHROPIC_API_KEY=sk-...    # or OPENAI_API_KEY / GOOGLE_API_KEY
```

**Hand-editing instead of the wizard.** If you'd rather skip the wizard:

```bash
cp fleet.config.example.json fleet.config.json
$EDITOR fleet.config.json
python run.py
```

The wizard only sets up one provider at a time. For a mixed fleet (e.g. Anthropic + OpenAI + Google together), run the wizard for a starter, then hand-edit additional entries.

`fleet.config.json` schema (see `fleet.config.example.json` for a fuller sample):

```json
{
  "server_url": "https://test-swarm-1-production.up.railway.app",
  "agents": [
    {
      "name": "phil",
      "provider": "openai",
      "model": "gpt-5.5",
      "api_key_env": "OPENAI_API_KEY"
    }
  ]
}
```

Per-entry fields:

| field            | meaning                                                                 |
|------------------|-------------------------------------------------------------------------|
| `name`           | Worktree dir + dashboard label.                                         |
| `provider`       | LLM provider — see [Providers](#providers).                             |
| `model`          | Model ID; per-provider defaults live in `DEFAULT_MODELS` in `run_loop.py`. |
| `api_key_env`    | Env var to read the API key from. Omit for CLI-auth providers.          |
| `tacit_knowledge`| Optional per-agent override of the shared `tacit_knowledge.md` file. See [Tacit knowledge](#tacit-knowledge). |

### Tacit knowledge

A private hint file at the repo root (`tacit_knowledge.md`, gitignored, never sent to the server). By default **all your agents share this one file**, so lessons distilled by any agent are available to every agent on the next run. Each agent consults the file when stagnating, and **appends its own `- LLM:` bullets** — "what didn't work, abstracted away from the specific challenge" — when it has stagnated 10 iterations in a row or hit a 50-run milestone. On fleet shutdown those LLM entries are collated back into `tacit_knowledge.md`, deduped against existing content.

Tacit knowledge is the practical, hard-won expertise you've built through years of practice — strategies, heuristics, judgment calls you reach for instinctively but rarely write down. `python run.py` offers a guided capture every run (default skip). To edit it directly outside of `run.py`:

```bash
python setup.py tacit             # edits the shared file by default
python setup.py tacit claude-1    # edits whichever file claude-1 resolves to
```

Both flows are append-mode — re-running the wizard adds to existing notes rather than wiping them.

**Per-agent override.** If you want one agent to use its own private file (no sharing), add `"tacit_knowledge": "tacit_knowledge_<name>.md"` to that agent's entry in `fleet.config.json`. To rename the shared file, add a top-level `"tacit_knowledge"` field at the same level as `agents`. Resolution precedence: per-agent override > top-level fleet default > implicit `tacit_knowledge.md`.

### Manual / power-user flow

The underlying commands `run.py` orchestrates are still callable directly when you want finer control:

```bash
python scripts/init_fleet.py                   # just the setup wizard
python setup.py tacit [<name>]                 # just the tacit wizard
python scripts/run_fleet.py                    # just launch (no preflight prompts)
python scripts/run_fleet.py --list             # show agent names, agent_ids, worktree status
python scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python scripts/run_fleet.py --clean            # remove every fleet worktree and its branch
```

## Benchmark image

Benchmarks run inside a local Docker container. Build the image once before the first launch:

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .

# for GPU swarms/challenges:
docker build -f Dockerfile.gpu -t tig-swarm-gpu .
```

## Providers

| `provider`            | Auth                                                                            |
|-----------------------|---------------------------------------------------------------------------------|
| `anthropic`           | `ANTHROPIC_API_KEY`                                                             |
| `openai`              | `OPENAI_API_KEY` (also `"api_base": "<url>"` for any OpenAI-compatible endpoint) |
| `google`              | `GOOGLE_API_KEY`                                                                |
| `venice`              | `VENICE_API_KEY` (Venice.ai — OpenAI-compatible, base URL baked in)             |
| `openrouter`          | `OPENROUTER_API_KEY` (multi-model proxy — model IDs are `publisher/name`, e.g. `anthropic/claude-3.5-sonnet`) |
| `claude-code`         | `claude` CLI login (no API key needed)                                          |
| `claude-code-agentic` | `claude` CLI login                                                              |
| `codex-agentic`       | `codex login`                                                                   |

Per-provider default models live in `DEFAULT_MODELS` (scripts/run_loop.py). The CLI providers accept any model ID their CLI accepts. Omit `api_key_env` for `claude-code` and the `-agentic` providers — those use the CLI's own login.

### Agent vs one-shot mode

`claude-code` is one-shot: the CLI returns a code blob and `run_loop.py` benchmarks it. The `-agentic` providers run a tooled headless agent in a sandboxed git worktree — the agent edits the algorithm file itself, runs `cargo check`, then `run_loop.py` benchmarks and publishes. Far more capable per iteration but burns ~5–20× tokens; only worth it under a subscription. Sandbox details in [ARCHITECTURE.md](./ARCHITECTURE.md#how-agents-work).

Each iteration shells out to `claude -p` from a temp directory so the CLI's `CLAUDE.md` auto-discovery doesn't inject anything from this repo into the system prompt — `run_loop.py` supplies its own. Trade-offs vs the API providers: per-call latency is higher (subprocess startup), and the dashboard's cost column reads $0 because the CLI doesn't surface token usage.

> **Agentic providers run silently.** `claude-code-agentic` and `codex-agentic` each invoke their CLI inside a single subprocess with `capture_output=True` so we can read the trace afterwards — there is **no live stdout** for the duration of that call. Expect no terminal output for up to `--agentic-timeout` seconds (default **900s / 15 min**) per iteration. The fleet still heartbeats every 60s in the background, and `[BENCH]` / Docker activity only starts after the agent returns.

## Config Rule

Swarm state lives on the server. Local files only tell this clone how to connect and run:

| file                     | purpose                                                       |
|--------------------------|---------------------------------------------------------------|
| `fleet.config.json`      | User-edited — list of agents to spawn (contributors).         |
| `swarm.admin.json`       | Host-only — admin key + swarm tuning. Created by `setup.py create`. |
| `.swarm-cache.json`      | Machine-managed — mirror of `/api/swarm_config`. Auto-refreshed by `setup.py sync` on every iteration. |
| `worktrees/<name>/agent.config.json` | Per-worktree state — provider/model + persisted `agent_id`. |

Secrets stay in environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`).

See `ARCHITECTURE.md` for internals and the swarm protocol contributors call into.
