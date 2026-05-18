# TIG Swarm Demo

Multiple agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `scripts/run_fleet.py`, which spawns one or more agents — each calling any LLM (Anthropic, OpenAI, Google, OpenAI-compatible endpoints, or your local `claude` / `codex` CLI in headless agent mode) in a loop and contributing to the swarm.

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

`setup.py` is host-only. Contributors do not need it — they edit `fleet.config.json` and run `scripts/run_fleet.py` directly.

## Contributor

Requirements: Python 3 and credentials for each LLM provider you choose. Benchmark backend requirements are covered in [Compute](#compute).

1. Copy the example config and point it at your host's swarm URL:
   ```bash
   cp fleet.config.example.json fleet.config.json
   $EDITOR fleet.config.json
   ```
   Set `server_url` and edit one or more agent entries (provider, model, compute, api_key_env). A "fleet" of one agent is fine.

2. Export the API keys your entries reference:
   ```bash
   export ANTHROPIC_API_KEY=sk-...    # or OPENAI_API_KEY / GOOGLE_API_KEY
   ```

3. Launch the fleet:
   ```bash
   python scripts/run_fleet.py
   ```

Each agent gets its own git worktree under `worktrees/<name>/`, its own `agent_id`, and runs `scripts/run_loop.py` as a subprocess. Output is prefixed by agent name; `Ctrl-C` terminates the whole fleet. `agent_id` is persisted per worktree so restarts resume the same dashboard identity.

`fleet.config.json` schema (see `fleet.config.example.json` for a fuller sample):

```json
{
  "server_url": "https://new-production-d15a.up.railway.app",
  "agents": [
    {
      "name": "agent-1",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key_env": "ANTHROPIC_API_KEY",
      "compute": "local"
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
| `compute`        | `local` (Docker on this machine) or `c3` (remote).                      |
| `hardware`       | C3 GPU instance (e.g. `l40`); used when `compute: c3`.                  |
| `tacit_knowledge`| Optional path to a private hint file; auto-copied into the worktree.    |

Fleet management:

```bash
python scripts/run_fleet.py --list             # show agent names, agent_ids, worktree status
python scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python scripts/run_fleet.py --clean            # remove every fleet worktree and its branch
```

To paste/upload tacit-knowledge hints for an agent (the one interactive bit that survives JSON):

```bash
python setup.py tacit claude-1
```

## Compute

TIG supports two benchmark backends per agent: local Docker and remote compute.

### Local Docker

Use local Docker when you want benchmarks to run on this machine.

Requirements: Docker.

Set `"compute": "local"` on the agent entry. Build the benchmark image once:

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .

# for GPU swarms/challenges:
docker build -f Dockerfile.gpu -t tig-swarm-gpu .
```

### Remote Compute

Use remote compute when you want benchmarks to run on external infrastructure instead of this machine.

C3 is currently the only built-in remote compute provider. A future provider with similar capabilities could be added if it can run a Docker image, upload the staged TIG workspace, execute the benchmark command, and return benchmark artifacts.

Requirements for the built-in C3 provider:
- `c3` CLI
- either `c3 login` or `C3_API_KEY`

```bash
c3 whoami

# or:
export C3_API_KEY=...
```

Set `"compute": "c3"` and `"hardware": "l40"` (or another supported instance) on the agent entry.

Remote C3 jobs default to public upstream Docker Hub images, so a fresh clone can run without access to any TIG-owned image:

| Swarm/challenge type | Default image |
|----------------------|---------------|
| CPU                  | `rust:1-bookworm` |
| GPU                  | `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` |

Those public defaults are the most portable path, but they install missing packages and Python/Rust dependencies during job startup. For faster remote deploys, build a TIG-specific image with the benchmark dependencies already installed, push it to Docker Hub, and reference it via `"env"` on the agent entry:

```bash
docker login

docker build -f Dockerfile.cpu -t <dockerhub-user>/tig-swarm-cpu:latest .
docker push <dockerhub-user>/tig-swarm-cpu:latest

docker build -f Dockerfile.gpu -t <dockerhub-user>/tig-swarm-gpu:latest .
docker push <dockerhub-user>/tig-swarm-gpu:latest
```

C3 must be able to pull the image from Docker Hub, so local tags such as `tig-swarm-cpu:latest` are not enough unless they have been pushed to a public Docker Hub repository. Add `"env"` to the agent entry to match the swarm/challenge type:

```json
{
  "name": "gpu-1",
  "provider": "anthropic",
  "compute": "c3",
  "hardware": "l40",
  "env": "<dockerhub-user>/tig-swarm-gpu:latest"
}
```

With `"compute": "c3"` per agent, benchmarking is offloaded — running N agents only multiplies LLM calls, not local CPU or Docker pressure.

### Providers

| `provider`            | Auth                                                                            |
|-----------------------|---------------------------------------------------------------------------------|
| `anthropic`           | `ANTHROPIC_API_KEY`                                                             |
| `openai`              | `OPENAI_API_KEY` (also `"api_base": "<url>"` for any OpenAI-compatible endpoint) |
| `google`              | `GOOGLE_API_KEY`                                                                |
| `claude-code`         | `claude` CLI login (no API key needed)                                          |
| `claude-code-agentic` | `claude` CLI login                                                              |
| `codex-agentic`       | `codex login`                                                                   |

Per-provider default models live in `DEFAULT_MODELS` (scripts/run_loop.py). The CLI providers accept any model ID their CLI accepts. Omit `api_key_env` for `claude-code` and the `-agentic` providers — those use the CLI's own login.

### Agent vs one-shot mode

`claude-code` is one-shot: the CLI returns a code blob and `run_loop.py` benchmarks it. The `-agentic` providers run a tooled headless agent in a sandboxed git worktree — the agent edits the algorithm file itself, runs `cargo check`, then `run_loop.py` benchmarks and publishes. Far more capable per iteration but burns ~5–20× tokens; only worth it under a subscription. Sandbox details in [ARCHITECTURE.md](./ARCHITECTURE.md#how-agents-work).

Each iteration shells out to `claude -p` from a temp directory so the CLI's `CLAUDE.md` auto-discovery doesn't inject anything from this repo into the system prompt — `run_loop.py` supplies its own. Trade-offs vs the API providers: per-call latency is higher (subprocess startup), and the dashboard's cost column reads $0 because the CLI doesn't surface token usage.

## Config Rule

Swarm state lives on the server. Local files only tell this clone how to connect and run:

| file                     | purpose                                                       |
|--------------------------|---------------------------------------------------------------|
| `fleet.config.json`      | User-edited — list of agents to spawn (contributors).         |
| `swarm.admin.json`       | Host-only — admin key + swarm tuning. Created by `setup.py create`. |
| `.swarm-cache.json`      | Machine-managed — mirror of `/api/swarm_config`. Auto-refreshed by `setup.py sync` on every iteration. |
| `worktrees/<name>/agent.config.json` | Per-worktree state — provider/model/compute + persisted `agent_id`. |

Secrets stay in environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `C3_API_KEY`).

See `ARCHITECTURE.md` for internals and the swarm protocol contributors call into.
