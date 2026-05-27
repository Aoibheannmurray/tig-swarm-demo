# TIG Swarm Demo

Multiple LLM agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `python run.py`, which spawns one or more agents — each calling an LLM (Anthropic, OpenAI, Google, OpenRouter, Venice, or your local `claude` / `codex` CLI) in a loop and contributing to the swarm.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for internals.

## Host

Requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python setup.py create              # deploys a Railway swarm, scaffolds fleet.config.json
python setup.py switch knapsack     # change the active challenge later
```

`setup.py` is host-only. Contributors run `python run.py`.

## Contributor

Requirements:
- Python 3
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running (Windows also needs WSL 2)
- Either an API key for your chosen provider, or a logged-in `claude` / `codex` CLI

No terminal handy? Open this repo in [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://docs.claude.com/en/docs/claude-code) — both read `AGENTS.md` and walk you through setup.

```bash
python run.py
```

It walks you through setup the first time, then just launches on subsequent runs (a couple of optional update prompts you can skip with Enter).

Set the API key your provider needs before launching:

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=sk-...     # or OPENAI_API_KEY / GOOGLE_API_KEY / etc.
```

```powershell
# Windows PowerShell  (cmd.exe: use  set ANTHROPIC_API_KEY=sk-...  with no quotes)
$env:ANTHROPIC_API_KEY="sk-..."     # or OPENAI_API_KEY / GOOGLE_API_KEY / etc.
```

`Ctrl-C` terminates the whole fleet. Each agent runs in its own git worktree under `worktrees/<name>/`; identities persist across restarts.

### Hand-editing

To skip the wizard:

```bash
cp fleet.config.example.json fleet.config.json
$EDITOR fleet.config.json
python run.py
```

Per-agent fields:

| field            | meaning                                                                 |
|------------------|-------------------------------------------------------------------------|
| `name`           | Worktree dir + dashboard label.                                         |
| `provider`       | LLM provider — see [Providers](#providers).                             |
| `model`          | Model ID; per-provider defaults live in `DEFAULT_MODELS` (`scripts/llm_backends.py`). |
| `api_key_env`    | Env var holding the API key. Omit for CLI-auth providers.               |
| `api_base`       | Optional override of the provider's base URL (e.g. an OpenAI-compatible gateway like OpenRouter: `https://openrouter.ai/api/v1`). |
| `tacit_knowledge`| Optional per-agent override of the shared `tacit_knowledge.md` file.    |
| `detailed_prompts`| Optional `true` to send a stricter, rule-based Rust prompt. Helps smaller/cheaper models whose code often fails to compile; leave off for frontier models to save tokens. |

### Tacit knowledge

`tacit_knowledge.md` is a private hints file your agents read when they get stuck. It's gitignored and never leaves your machine. All your agents share it by default, so insights accumulate across the whole fleet.

Agents also **write back to it**: when one has been failing for a stretch and is about to start over from scratch, it adds a one-line `- LLM:` "what didn't work" note — so future attempts can avoid the same dead end.

To add your own hints, accept the `Add tacit knowledge?` prompt in `run.py`, or run `python setup.py tacit` directly. Both append rather than overwrite, and the edit menu can open the file in your `$EDITOR`. Deeper detail — when agents append, how files resolve per agent — lives in [ARCHITECTURE.md](./ARCHITECTURE.md#tacit-knowledge).

### Manual / power-user flow

The underlying commands `run.py` orchestrates also work directly:

```bash
python scripts/init_fleet.py                   # just the setup wizard
python setup.py tacit [<name>]                 # just the tacit wizard
python scripts/run_fleet.py                    # launch only
python scripts/run_fleet.py --list             # agent status
python scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python scripts/run_fleet.py --clean            # remove every worktree + branch
```

## Benchmark image

Build once before the first launch:

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .
docker build -f Dockerfile.gpu -t tig-swarm-gpu .       # GPU challenges only
```

## Providers

| `provider`            | Auth                                                                            |
|-----------------------|---------------------------------------------------------------------------------|
| `anthropic`           | `ANTHROPIC_API_KEY`                                                             |
| `openai`              | `OPENAI_API_KEY` (also `"api_base": "<url>"` for any OpenAI-compatible endpoint) |
| `google`              | `GOOGLE_API_KEY`                                                                |
| `venice`              | `VENICE_API_KEY` (OpenAI-compatible, base URL baked in)                         |
| `openrouter`          | `OPENROUTER_API_KEY` (multi-model proxy; model IDs are `publisher/name`)        |
| `claude-code`         | `claude` CLI login (no API key needed)                                          |
| `claude-code-agentic` | `claude` CLI login                                                              |
| `codex-agentic`       | `codex login`                                                                   |

`claude-code` is one-shot: the CLI returns a code blob each iteration. The `-agentic` providers run a tooled headless agent in a sandboxed git worktree — far more capable per iteration but burn ~5–20× tokens; subscription-only. They run silently for up to 15 min per iteration; don't kill the terminal if there's no output — heartbeats keep the dashboard alive, and `[BENCH]` lines appear once the agent returns.

## Local files

Swarm state lives on the server. Local files only tell this clone how to connect and run:

| file                  | purpose                                                       |
|-----------------------|---------------------------------------------------------------|
| `fleet.config.json`   | Your fleet's agents (user-edited).                            |
| `tacit_knowledge.md`  | Your private hint file (gitignored).                          |
| `.swarm-cache.json`   | Auto-refreshed mirror of `/api/swarm_config`.                 |
| `swarm.admin.json`    | Host-only — admin key + swarm tuning.                         |

Secrets stay in environment variables.
