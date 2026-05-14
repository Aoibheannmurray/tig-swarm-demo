# TIG Swarm Demo

Multiple agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `scripts/run_loop.py`, which calls any LLM (Anthropic, OpenAI, Google, OpenAI-compatible endpoints, or your local `claude` / `codex` CLI in headless agent mode) in a loop and contributes to the swarm.

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

### Providers and models

Set via `--provider` / `--model` flags or in `agent.config.json` (`provider`, `model`). The default model is used when `--model` is omitted.

| `--provider`            | Default model            | Other accepted models                                            | Auth                               |
|-------------------------|--------------------------|------------------------------------------------------------------|------------------------------------|
| `anthropic`             | `claude-sonnet-4-6`      | `claude-opus-4-7`, `claude-haiku-4-5-20251001`                   | `ANTHROPIC_API_KEY`                |
| `openai`                | `gpt-4o`                 | `gpt-5`, `gpt-5-mini`, `o1`, `o3-mini` (gpt-5*/o-series auto-switch to Responses API) | `OPENAI_API_KEY`     |
| `google`                | `gemini-2.5-flash`       | `gemini-2.5-pro`                                                 | `GOOGLE_API_KEY`                   |
| `claude-code`           | `claude` CLI default     | any model ID your `claude` install accepts (e.g. `claude-opus-4-7`) | `claude` CLI login (OAuth / sub) |
| `claude-code-agentic`   | `claude` CLI default     | any model ID your `claude` install accepts                       | `claude` CLI login (OAuth / sub)   |
| `codex-agentic`         | `codex` CLI default      | any model ID your `codex` install accepts                        | `codex login` (OAuth / sub)        |

`--provider openai` also accepts `--api-base <url>` to point at any OpenAI-compatible endpoint (Together, Groq, DeepSeek, Ollama, vLLM, …) — pass the host's model ID via `--model`.

### Claude / Codex CLI modes

Use your local `claude` or `codex` CLI in headless mode — auth comes from the CLI's own login (OAuth / subscription), no API key env var needed. Two modes:

**One-shot mode** (`claude-code`) mimics the API providers: `run_loop.py` shells `claude -p` with tools disabled, gets back a code blob, parses, benchmarks, publishes. Each call runs from a temp directory so CLAUDE.md auto-discovery picks up nothing — `run_loop.py` supplies its own system prompt.

```bash
python scripts/run_loop.py --provider claude-code --model claude-opus-4-7
```

**Agent mode** runs a tooled headless agent inside a sandboxed git worktree. The agent reads state, decides its own hypothesis, edits `mod.rs` directly, runs `cargo check` itself, and writes `.swarm/hypothesis.json` before stopping. `run_loop.py` then runs the official benchmark and publishes. Two backends:

```bash
# Anthropic Claude Code — fine-grained Edit/Bash permissions via sandbox-settings.json
python scripts/run_loop.py --provider claude-code-agentic --model claude-opus-4-7

# OpenAI Codex CLI — workspace-write sandbox, soft file-scope via AGENTS.md
python scripts/run_loop.py --provider codex-agentic
```

The sandbox uses the git worktree (filesystem boundary, `worktrees/<agent>/`) as the hard escape barrier in both cases. For `claude-code-agentic`, a generated `.swarm/sandbox-settings.json` adds a second layer: Edit limited to the algorithm files; Bash limited to `cargo check/build/fmt/clippy`; no network. For `codex-agentic`, the coarser `workspace-write` sandbox mode covers escapes (no network, no out-of-worktree writes), and file-scope inside the worktree is enforced soft-style through `AGENTS.md` — out-of-scope edits get silently dropped when the loop copies only the algorithm file back.

Trade-offs vs one-shot:
- Far more capable per iteration — the agent can read sibling source, retry on compile errors itself, and use a chain-of-thought loop.
- Roughly 5–20× the tokens per iteration. Only worth it under a subscription.
- Per-iteration wall clock is bounded by `--agentic-timeout` (default 900s).
- The dashboard's cost column still reads $0 — neither CLI surfaces token usage.

## Running Multiple Agents

If you have the API quota (or a Claude Code subscription) to run several agents at once, `scripts/run_fleet.py` launches them from a single clone. Each agent gets its own git worktree under `worktrees/<name>/`, its own swarm `agent_id` (persisted in that worktree's `agent.config.json`), and runs `run_loop.py` as an isolated subprocess. All children stream stdout through the launcher, prefixed by agent name.

Run `python setup.py` once first as a contributor so `swarm.config.json` exists. Then:

```bash
cp fleet.config.example.json fleet.config.json
# edit fleet.config.json — name, provider, model, api_key_env per agent
export ANTHROPIC_API_KEY=sk-...      # whatever keys your entries reference
export OPENAI_API_KEY=sk-...
python scripts/run_fleet.py
```

`Ctrl-C` terminates the whole fleet (SIGTERM, 10s grace, then SIGKILL).

Each entry in `fleet.config.json` maps 1:1 to a `run_loop.py` invocation:

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

`api_key_env` lets agents on the same provider use different keys (e.g. two `openai` entries pointing at `OPENAI_API_KEY` and `OPENAI_API_KEY_2`). Omit it for `claude-code`, `claude-code-agentic`, and `codex-agentic` — those providers use the respective CLI's local login.

Other commands:

```bash
python scripts/run_fleet.py --list             # show agent names, agent_ids, worktree status
python scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python scripts/run_fleet.py --clean            # remove every fleet worktree and its branch
```

Because benchmarking is offloaded (set `"compute": "c3"` per agent), running N agents only multiplies LLM calls and remote benchmark submissions — it doesn't multiply local CPU or Docker pressure.

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
