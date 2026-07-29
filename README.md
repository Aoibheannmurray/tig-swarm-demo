# Prometheus TIG Swarm

Multiple LLM agents optimize TIG challenge solvers in Rust, coordinated by a server and live dashboard.

Each contributor runs `python3 run.py`, which spawns one or more agents — each calling an LLM (Anthropic, OpenAI, Google, OpenRouter, Venice, or your local `claude` / `codex` CLI) in a loop and contributing to the swarm.

See [ARCHITECTURE.md](./docs/ARCHITECTURE.md) for internals.

## Status & support

This project was built largely with Claude Code and is released as-is: things
may break — use at your own risk. What that means in practice:

- **Bug reports welcome** — file an issue with the failing command, its
  output, and your platform. We fix bugs on a best-effort basis, on no
  schedule. Security problems go through [SECURITY.md](./SECURITY.md)
  (privately), never a public issue.
- **Building on it is the intended use.** Fork it, point it at your own
  challenges, replace pieces — the swarm mechanics are GPLv3
  ([license details](#license)). Small bug-fix PRs are welcome; large
  features are better carried in your fork
  (see [CONTRIBUTING.md](./CONTRIBUTING.md) for why, and for dev setup).
- There is no support channel; the README and [docs/](./docs/) are the docs.

## Choose your role

- **Hosting a new swarm?** Follow [For hosts: create a swarm](#for-hosts-create-a-swarm).
- **Joining an existing swarm?** Follow [For contributors: join a swarm](#for-contributors-join-a-swarm).

## For hosts: create a swarm

Hosts deploy and manage the shared coordination server and invite contributors.

Requirements: Python 3, Git, and a Railway account. The setup UI can install
the Railway CLI if it is not already available.

Clone the repository, then start the setup UI:

```bash
git clone https://github.com/tig-foundation/prometheus-early-beta.git
cd prometheus-early-beta
python3 run.py --ui
```

Choose **Host → Create & manage a swarm**. The companion UI guides Railway
login and provisioning, challenge selection, seed setup, and contributor
invites. The UI runs locally; keep its terminal open while using it.

Once the swarm is live, day-to-day host controls move to the hosted **Admin
Console** at `<your-server-url>/admin/` (sign in with the admin key from
setup): invites and revocation, challenge switching, benchmark
instances/timeout, pool seeding and resets, and the swarm-tuning knobs — each
setting is explained inline next to its control, and edits apply to the
running swarm without restarts. See
[ARCHITECTURE.md](./docs/ARCHITECTURE.md#host-controls-the-admin-console).

### Optional: host-admin terminal commands

The setup UI is the recommended path. These equivalent commands are available
for hosts who prefer to manage the swarm directly from the cloned repository:

```bash
railway login
python3 setup.py create              # deploys a Railway swarm, scaffolds fleet.config.json
python3 setup.py switch hypergraph     # change the active challenge later
```

Manage contributors from the same clone:

```bash
python3 setup.py invite [<username>]   # issue per-contributor credentials (username +
                                       # derived swarm_password; random slug if omitted)
python3 setup.py revoke <username>     # block future registers, stop their running agents
python3 setup.py list                  # contributors: agents, activity, revoked state
```

`setup.py` is host-only. Contributors use the same local UI or run
`python3 run.py` for terminal setup.

## For contributors: join a swarm

Contributors run one or more agents that improve challenge solvers. You need a
join link from the host before starting.

Requirements:

- Python 3
- Git (each agent runs in its own git worktree)
- **Compute — choose one:**
  - **Docker installed and running** for local benchmarking (`"compute": "local"`): use [Docker Desktop](https://docs.docker.com/desktop/) on macOS or Windows (Windows also requires WSL 2), or [Docker Engine](https://docs.docker.com/engine/install/) or Docker Desktop on Linux.
  - A [C3 account](https://cthree.cloud/) for cloud-based compute. C3 runs benchmarks remotely, so Docker is not required.
- Either an API key for your chosen provider, or a logged-in `claude` / `codex` CLI

> **Local compute resources:** The first local benchmark automatically builds
> the required Docker image. Expect a several-gigabyte download and allow at
> least 10 GB of free disk space for CPU challenges or 25 GB for GPU challenges
> (the CUDA image is substantially larger), including build layers and caches.
> 8 GB of system RAM is a practical minimum; 16 GB is recommended, especially
> when running multiple agents. GPU challenges also require a supported NVIDIA
> GPU and the NVIDIA Container Toolkit.

### Join with a link (easiest)

If your host sent you a **join link** (`https://<swarm>/join#u=…&p=…`), open it
in a browser and run the command it provides. The setup app will open, where
you can choose your models and compute option, add any required API keys, and
launch your fleet. You do not need to clone the repository manually.

```bash
# macOS / Linux (needs Python 3 + git)
curl -fsSL https://raw.githubusercontent.com/tig-foundation/prometheus-early-beta/main/deploy/get-swarm.py \
  | python3 - join "<your-join-link>" --ui
```

```powershell
# Windows (PowerShell or cmd; try `py` if `python` isn't recognized)
curl.exe -fsSL https://raw.githubusercontent.com/tig-foundation/prometheus-early-beta/main/deploy/get-swarm.py | python - join "<your-join-link>" --ui
```

### Setup UI from an existing clone

If you have already cloned the repository manually, run:

```bash
python3 run.py --ui
```

This opens the local web companion. It walks you through fleet setup, then lets
you launch the fleet and edit it later—adding agents or changing settings and
providers.

### Optional: terminal-only setup

The setup UI is the recommended path. From an existing clone, `python3 run.py`
runs the setup wizard entirely in the terminal. On subsequent runs it launches
the saved fleet, with optional update prompts that you can skip with Enter.

Export your keys before launching — your provider key (skip if you use a
`claude` / `codex` CLI login), plus `C3_API_KEY` only if using C3 compute:

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=sk-...     # or OPENAI_API_KEY / GEMINI_API_KEY / etc.
export C3_API_KEY=c3_...            # C3 compute only
```

```powershell
# Windows PowerShell  (cmd.exe: use  set ANTHROPIC_API_KEY=sk-...  with no quotes)
$env:ANTHROPIC_API_KEY="sk-..."     # or OPENAI_API_KEY / GEMINI_API_KEY / etc.
$env:C3_API_KEY="c3_..."            # C3 compute only
```

`Ctrl-C` terminates the whole fleet. Each agent runs in its own git worktree under `worktrees/<name>/`; identities persist across restarts.

### Updating fleet configuration manually

After setup, you can change agents, providers, models, compute options, and
other settings by editing your local `fleet.config.json`. Use
[`fleet.config.example.json`](./fleet.config.example.json) as a reference. The
setup UI can also make these changes for you.

Many settings **hot-reload**: edit `fleet.config.json` while the fleet runs
and the change lands on each agent's next iteration, no restart — `role`,
`seeded_start`, the HPO and cleaner knobs, and the C3 warm-image settings.
Provider, model, and compute are read at startup and need a fleet restart.

### Tacit knowledge

`tacit_knowledge.md` is a local file containing your domain tacit knowledge, i.e: strategies to use when one gets stuck. It is shown to your agents when they stagnate. It's gitignored and remains local. All your agents share it by default, so insights accumulate across the whole fleet.

Agents can also optionally **write back to it**: when one has been failing for a stretch and is about to start over from scratch, it adds a one-line `- LLM:` "what didn't work" note — so future attempts can avoid the same dead end.

To add your own hints, accept the `Add tacit knowledge?` prompt in `run.py`, or run `python3 setup.py tacit` directly. More detail can be found in [ARCHITECTURE.md](./docs/ARCHITECTURE.md#tacit-knowledge).

### Optional: manual power-user commands

The underlying commands `run.py` orchestrates also work directly:

```bash
python3 scripts/init_fleet.py                   # just the setup wizard
python3 setup.py tacit [<name>]                 # just the tacit wizard
python3 scripts/run_fleet.py                    # launch only
python3 scripts/run_fleet.py --list             # agent status
python3 scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python3 scripts/run_fleet.py --clean            # remove every worktree + branch
```

To inspect recorded Claude Code agentic sessions and tool activity, see
[Inspecting agentic sessions](./docs/AGENTIC_SESSIONS.md).


## Providers


| `provider`            | Auth                                                                             |
| --------------------- | -------------------------------------------------------------------------------- |
| `anthropic`           | `ANTHROPIC_API_KEY`                                                              |
| `openai`              | `OPENAI_API_KEY` (also `"api_base": "<url>"` for any OpenAI-compatible endpoint) |
| `google`              | `GEMINI_API_KEY`                                                                 |
| `venice`              | `VENICE_API_KEY` (OpenAI-compatible, base URL baked in)                          |
| `openrouter`          | `OPENROUTER_API_KEY` (multi-model proxy; model IDs are `publisher/name`)         |
| `claude-code`         | `claude` CLI login (no API key needed)                                           |
| `claude-code-agentic` | `claude` CLI login                                                               |
| `codex-agentic`       | `codex login`                                                                    |
| `custom`              | Setup-only name for your own OpenAI-compatible endpoint — see below              |

Picking **Custom / local LLM** in setup asks for the endpoint URL, the model id
it serves, and the name of the variable holding its key, then writes them as a
`provider: "openai"` agent with an explicit `api_base`. A key is optional when
the endpoint is on your own machine or local network — llama.cpp, vLLM, Ollama
and LM Studio check none by default. Benchmarking is independent of this, so a
local model can still use C3 cloud compute.



`claude-code` is single-shot: the CLI returns a code blob each iteration. The `-agentic` providers run a tooled headless agent in a sandboxed git worktree. More capable per iteration but burn ~5–20× tokens; subscription-only.

## Interpreting the score

Each iteration prints a `[BENCH]` line: the aggregate `Score`, `Feasible`, and a per-track breakdown:

```
[BENCH] Score: -199814  Feasible: False
        Track 0: 52000
        Track 1: -1000000  (below baseline)
        Track 2: 46800
```

The aggregate is a **shifted geometric mean** across tracks, and a failed or infeasible track is assigned a large fixed penalty. Because of that penalty, **a single bad track can drag the whole aggregate negative** even when the other tracks scored well. 

## Local files

Swarm state lives on the server. Local files only tell this clone how to connect and run:


| file                 | purpose                                       |
| -------------------- | --------------------------------------------- |
| `fleet.config.json`  | Your fleet's agents (user-edited).            |
| `tacit_knowledge.md` | Your private hint file (gitignored).          |
| `.swarm-cache.json`  | Auto-refreshed mirror of `/api/swarm_config`. |
| `swarm.admin.json`   | Host-only — admin key + swarm tuning.         |


## Remote benchmarking with C3

[C3](https://cthree.cloud) runs benchmarks remotely, so Docker and local
benchmark hardware are not required. Create a C3 account, obtain an API key
from the [C3 dashboard](https://cthree.cloud/dashboard/settings), and enter it
in the setup UI or export it as `C3_API_KEY`.

See [C3 cloud compute](./docs/C3.md) for CLI installation, manual configuration,
hardware options, and details of how remote benchmark jobs run.

## License

The swarm — orchestration (`scripts/`, `run.py`), coordination server
(`server/`), web UIs (`dashboard/`, `control-ui/`), hosted runner (`runner/`),
and host-admin CLI (`setup.py` / `hostadmin/`) — is free software under the
**GNU GPLv3** ([LICENSE](./LICENSE)).

The TIG challenge and solver code (`src/`, `initial_algorithms/`) is the example
workload the swarm ships with; it derives from the
[tig-monorepo](https://github.com/tig-foundation/tig-monorepo) and remains under
the TIG Foundation's license agreements — see
[LICENSE-TIG.md](./LICENSE-TIG.md).
