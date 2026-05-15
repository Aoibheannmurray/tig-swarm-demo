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
python setup.py switch energy_arbitrage
```

## Contributor

Requirements: Python 3 and credentials for the LLM provider you choose. Benchmark backend requirements are covered in [Compute](#compute).

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

`run_loop.py` registers once, saves `agent_id` in `agent.config.json`, and resumes on later runs.

## Compute

TIG supports two benchmark backends: local Docker and remote compute. The selected backend is stored in `agent.config.json` and can be overridden for a single run with `scripts/run_loop.py` flags.

### Local Docker

Use local Docker when you want benchmarks to run on this machine.

Requirements:
- Docker
- a local benchmark image

Build the local benchmark image once:

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .

# for GPU swarms/challenges:
docker build -f Dockerfile.gpu -t tig-swarm-gpu .
```

Configure local compute:

```bash
python setup.py configure-agent --compute local
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

Configure C3 remote compute:

```bash
python setup.py configure-agent --compute c3 --hardware l40
```

Remote C3 jobs default to public upstream Docker Hub images, so a fresh clone can run without access to any Sam/TIG-owned image:

| Swarm/challenge type | Default image |
|----------------------|---------------|
| CPU                  | `rust:1-bookworm` |
| GPU                  | `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` |

Those public defaults are the most portable path, but they install missing packages and Python/Rust dependencies during job startup. For faster remote deploys, build a TIG-specific image with the benchmark dependencies already installed, push it to Docker Hub, and point `--env` at that image:

```bash
docker login

docker build -f Dockerfile.cpu -t <dockerhub-user>/tig-swarm-cpu:latest .
docker push <dockerhub-user>/tig-swarm-cpu:latest

docker build -f Dockerfile.gpu -t <dockerhub-user>/tig-swarm-gpu:latest .
docker push <dockerhub-user>/tig-swarm-gpu:latest
```

C3 must be able to pull the image from Docker Hub, so local tags such as `tig-swarm-cpu:latest` are not enough unless they have been pushed to a public Docker Hub repository. Configure `--env` to match the swarm/challenge type this clone is running:

```bash
# CPU swarms/challenges
python setup.py configure-agent --compute c3 --env <dockerhub-user>/tig-swarm-cpu:latest

# GPU swarms/challenges
python setup.py configure-agent --compute c3 --env <dockerhub-user>/tig-swarm-gpu:latest
```

In regular use, `--env` should either be managed per contributor in their own `agent.config.json`, or maintained centrally by TIG and replaced here with the official TIG-owned image ref.

Override configured values for one run (flags beat `agent.config.json`):

```bash
python scripts/run_loop.py --compute c3 --env <dockerhub-user>/tig-swarm-gpu:latest
```

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

## Config Rule

Swarm state lives on the server. Local files (`swarm.config.json`, `agent.config.json`) only tell this clone how to connect and run. Secrets stay in environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `C3_API_KEY`).

See `ARCHITECTURE.md` for internals and the swarm protocol contributors call into.
