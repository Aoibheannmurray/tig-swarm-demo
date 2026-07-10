# Prometheus TIG Swarm

Multiple LLM agents optimize TIG challenge solvers in Rust, coordinated by a server and live dashboard.

Each contributor runs `python3 run.py`, which spawns one or more agents — each calling an LLM (Anthropic, OpenAI, Google, OpenRouter, Venice, or your local `claude` / `codex` CLI) in a loop and contributing to the swarm.

See [ARCHITECTURE.md](./docs/ARCHITECTURE.md) for internals.

## Status & support

This project was built largely with Claude Code and is released as-is: no
support is provided and things may break — use at your own risk. Issues are
tracked but not triaged on any schedule; contributions are welcome via PR.

## Host

**Browser-only (easiest):** deploy the coordination server to Railway with one
click — no terminal, no CLI. The server self-configures on first boot
(generates its admin key + swarm password, seeds starting algorithms from the
image), then you manage everything — challenge, contributors, join links — from
the hosted Admin Console. See
[deploy/DEPLOY_ON_RAILWAY.md](./deploy/DEPLOY_ON_RAILWAY.md).

**CLI:** requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python3 setup.py create              # deploys a Railway swarm, scaffolds fleet.config.json
python3 setup.py switch energy_arbitrage     # change the active challenge later
```

Manage contributors from the same clone:

```bash
python3 setup.py invite [<username>]   # issue per-contributor credentials (username +
                                       # derived swarm_password; random slug if omitted)
python3 setup.py revoke <username>     # block future registers, stop their running agents
python3 setup.py list                  # contributors: agents, activity, revoked state
```

`setup.py` is host-only. Contributors run `python3 run.py`.

## Contributor

Requirements:

- Python 3
- Git (each agent runs in its own git worktree)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running — **only if you benchmark locally** (`"compute": "local"`). Not needed when all agents benchmark on C3, which runs every benchmark remotely. (Windows local Docker also needs WSL 2.)
- Either an API key for your chosen provider, or a logged-in `claude` / `codex` CLI



### Join with a link (easiest)

If your host sent you a **join link** (`https://<swarm>/join#u=…&p=…`), open it
in a browser — the join page hands you a single command with the link already
baked in. It fetches the swarm code (no manual clone) and opens the local
**setup app** in your browser, where you pick your provider/models, paste your
API keys (LLM + [C3](https://cthree.cloud/dashboard/settings); stored locally in
a gitignored `secrets.local.json`, never uploaded), and click **Launch fleet**.

```bash
# macOS / Linux (needs Python 3 + git)
# NOTE: pinned to the server-onboarding branch until it merges to main —
# then use .../main/deploy/get-swarm.py and drop --branch.
curl -fsSL https://raw.githubusercontent.com/Aoibheannmurray/tig-swarm-demo/server-onboarding/deploy/get-swarm.py \
  | python3 - join "<your-join-link>" --ui --branch server-onboarding
```

```powershell
# Windows (PowerShell or cmd; try `py` if `python` isn't recognized)
curl.exe -fsSL https://raw.githubusercontent.com/Aoibheannmurray/tig-swarm-demo/server-onboarding/deploy/get-swarm.py | python - join "<your-join-link>" --ui --branch server-onboarding
```

(Advanced: dropping `--ui` runs headless instead, fetching a fleet config
stored server-side via the `/api/contributor/config` API — there's no UI for
authoring that anymore, so most people want the setup-app flow above.)

### Local Web Setup

```bash
python3 run.py --ui
```

Opens the local web companion. It walks you through fleet setup, then lets you launch the fleet and edit it later — add agents, change settings or providers.

### Terminal setup

`python3 run.py` runs setup through a wizard on the terminal. It walks you through setup the first time, then just launches on subsequent runs (a couple of optional update prompts you can skip with Enter).

Export your keys before launching — your provider key (skip if you use a `claude` / `codex` CLI login) and `C3_API_KEY` for C3 compute:

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=sk-...     # or OPENAI_API_KEY / GOOGLE_API_KEY / etc.
export C3_API_KEY=c3_...            # from `c3 apikey create tig-swarm`
```

```powershell
# Windows PowerShell  (cmd.exe: use  set ANTHROPIC_API_KEY=sk-...  with no quotes)
$env:ANTHROPIC_API_KEY="sk-..."     # or OPENAI_API_KEY / GOOGLE_API_KEY / etc.
$env:C3_API_KEY="c3_..."            # from `c3 apikey create tig-swarm`
```

`Ctrl-C` terminates the whole fleet. Each agent runs in its own git worktree under `worktrees/<name>/`; identities persist across restarts.

### Hand-editing

To skip the wizard:

```bash
cp fleet.config.example.json fleet.config.json
$EDITOR fleet.config.json
```

Per-agent fields:


| field              | meaning                                                                                                                                                                                                                    |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`             | Agent name                                                                                                                                                                                                                 |
| `provider`         | LLM provider eg: claude-code — see [Providers](#providers).                                                                                                                                                                |
| `model`            | Model ID. Run `python scripts/list_models.py <provider>` to see what's available; per-provider defaults live in `DEFAULT_MODELS` (`scripts/llm_backends.py`).                                                              |
| `api_key_env`      | Variable holding the API key, eg: `OPENROUTER_API_KEY`. Omit for CLI-auth providers.                                                                                                                                       |
| `api_base`         | Optional override of the provider's base URL, e.g. `https://openrouter.ai/api/v1`.                                                                                                                                         |
| `detailed_prompts` | Optional `true` to send a stricter, rule-based Rust prompt. Helps smaller/cheaper models whose code often fails to compile.                                                                                                |
| `role`             | `explorer` writes novel/ambitious algorithms; `exploiter` makes small focused localized edits. **Hot-editable** — change it in `fleet.config.json` while the fleet runs and it takes effect on the agent's next iteration. |
| `seeded_start`     | Optional `true`/`false` override of where the agent starts a fresh trajectory. `true`: start from working code (server seed pool → best active peer → stub as fallback); `false`: always start from the bare stub. Omit for the default policy: frontier explorers bootstrap from the stub on CPU challenges, everyone else (and all GPU challenges) gets working code. **Hot-editable** like `role`; applies at the next fresh trajectory (registration or stagnation reset), not mid-trajectory. |




Then run the fleet (make sure you've exported your API keys first):

```bash
python3 scripts/run_fleet.py
```

### Tacit knowledge

`tacit_knowledge.md` is a local file containing your domain tacit knowledge, i.e: strategies to use when one gets stuck. It is shown to your agents when they stagnate. It's gitignored and remains local. All your agents share it by default, so insights accumulate across the whole fleet.

Agents can also optionally **write back to it**: when one has been failing for a stretch and is about to start over from scratch, it adds a one-line `- LLM:` "what didn't work" note — so future attempts can avoid the same dead end.

To add your own hints, accept the `Add tacit knowledge?` prompt in `run.py`, or run `python3 setup.py tacit` directly. More detail can be found in [ARCHITECTURE.md](./docs/ARCHITECTURE.md#tacit-knowledge).

### Manual / power-user flow

The underlying commands `run.py` orchestrates also work directly:

```bash
python3 scripts/init_fleet.py                   # just the setup wizard
python3 setup.py tacit [<name>]                 # just the tacit wizard
python3 scripts/run_fleet.py                    # launch only
python3 scripts/run_fleet.py --list             # agent status
python3 scripts/run_fleet.py --only claude-1    # run a subset (repeatable)
python3 scripts/run_fleet.py --clean            # remove every worktree + branch
```



## Benchmark image (local compute only)

Only needed if an agent has `"compute": "local"` — the C3 default needs no
local images. Build once before the first launch:

```bash
docker build -f Dockerfile.cpu -t tig-swarm-cpu .
docker build -f Dockerfile.gpu -t tig-swarm-gpu .       # GPU challenges only
```



## Providers


| `provider`            | Auth                                                                             |
| --------------------- | -------------------------------------------------------------------------------- |
| `anthropic`           | `ANTHROPIC_API_KEY`                                                              |
| `openai`              | `OPENAI_API_KEY` (also `"api_base": "<url>"` for any OpenAI-compatible endpoint) |
| `google`              | `GOOGLE_API_KEY`                                                                 |
| `venice`              | `VENICE_API_KEY` (OpenAI-compatible, base URL baked in)                          |
| `openrouter`          | `OPENROUTER_API_KEY` (multi-model proxy; model IDs are `publisher/name`)         |
| `claude-code`         | `claude` CLI login (no API key needed)                                           |
| `claude-code-agentic` | `claude` CLI login                                                               |
| `codex-agentic`       | `codex login`                                                                    |



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

## Inspecting agentic prompts

In agentic mode (`claude-code-agentic` / `codex-agentic`) each iteration runs
`claude -p` inside `worktrees/<agent>/`, and Claude Code logs the full session.
To see exactly what an agent was told and did:

```bash
python3 scripts/show_agent_session.py <agent> --list   # list that agent's sessions, newest first
python3 scripts/show_agent_session.py <agent>          # render the newest one
python3 scripts/show_agent_session.py <agent> --index 3  # an older run
python3 scripts/show_agent_session.py <agent> --full   # don't truncate long blocks
```

A session is a full agentic trace, not just input→output text. You see, in order:

- **SYSTEM** — the swarm's stable rules (`worktrees/<agent>/CLAUDE.md`). These
  aren't in the raw session log — the harness folds them into the system prompt —
  so the script prints the on-disk copy for you. (Claude Code's own base system
  prompt isn't recoverable from a log; capture it with `claude --debug` on a live run.)
- **USER** — the per-iteration prompt the swarm piped in (score, role, niche,
  inspiration, task).
- **thinking** — Claude's private reasoning before it acts (scratchpad
  chain-of-thought, not shown to end users normally; surfaced here).
- **ASSISTANT** — the text replies.
- **⚙ TOOL CALL / └─ TOOL RESULT** — each `Read`/`Edit`/`Bash` the agent made and
  what came back.

## Local files

Swarm state lives on the server. Local files only tell this clone how to connect and run:


| file                 | purpose                                       |
| -------------------- | --------------------------------------------- |
| `fleet.config.json`  | Your fleet's agents (user-edited).            |
| `tacit_knowledge.md` | Your private hint file (gitignored).          |
| `.swarm-cache.json`  | Auto-refreshed mirror of `/api/swarm_config`. |
| `swarm.admin.json`   | Host-only — admin key + swarm tuning.         |


## Remote benchmarking with C3

This swarm benchmarks on [C3](https://cthree.cloud) cloud hardware by default — the
`run.py` wizard and `fleet.config.example.json` both set `"compute": "c3"`, so
you don't need local compute. To benchmark locally in Docker instead, set
`"compute": "local"` on an agent in `fleet.config.json`, then launch as usual
with `python3 run.py`. 

First install the `c3` CLI:

```bash
curl -fsSL https://cthree.cloud/install.sh | sh
```

Then authenticate, via either:

- `c3 login` (uses your existing session), or
- `c3 apikey create tig-swarm` then export `C3_API_KEY=...`

**Windows: installing the** `c3` **CLI** (the install script above is macOS/Linux only)

The equivalent of `curl -fsSL https://cthree.cloud/install.sh | sh`, in PowerShell:

```powershell
# 1. Create an install folder
$dir = "$env:LOCALAPPDATA\Programs\c3"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# 2. Download the Windows binary as c3.exe
curl.exe -fsSL "https://cthree.cloud/releases/latest/c3-windows-amd64.exe" -o "$dir\c3.exe"

# 3. Add the folder to your User PATH (permanent) if not already there
$userPath = [System.Environment]::GetEnvironmentVariable("Path","User")
if (($userPath -split ';') -notcontains $dir) {
  [System.Environment]::SetEnvironmentVariable("Path", "$userPath;$dir", "User")
}

# 4. Make it available in the CURRENT window too (no restart needed)
$env:Path = "$env:Path;$dir"

# 5. Verify
c3 --version
```

- **PATH:** step 3 adds it permanently for your user account; step 4 makes it work in the window you're in right now. Other already-open terminals won't see `c3` until you open a new window.
- **Command name:** it's `c3` (the binary is `c3.exe`) — no `.sh` and no `sh` required on Windows.
- **Arch:** this uses the `amd64` build (correct for most machines, `PROCESSOR_ARCHITECTURE = AMD64`). On an ARM Windows PC, swap the URL to `c3-windows-arm64.exe`.
- **Updating later:** re-run steps 1–2 to overwrite `c3.exe` with the latest release.

### Optional C3 fields in `fleet.config.json`

| key           | purpose                                                                                                                                                                             |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `compute`     | `"c3"` for C3 cloud hardware (the wizard & example default), `"local"` for local Docker. Omit the field and it falls back to `"local"`.                                             |
| `c3_hardware` | C3 hardware selector. Use `"auto"` to run CPU challenges on `cpu-d3-4vcpu-16gb` and GPU challenges on `l40`; pin an exact profile only when needed.                                 |
| `c3_time`     | Per-job walltime (default: `02:00:00`).                                                                                                                                             |
| `c3_provider` | Optional C3 backend passed as `c3 deploy -p ...`.                                                                                                                                   |
| `c3_api_key`  | Optional per-agent C3 API key (raw value). Omit to inherit the top-level fleet `c3_api_key`, then `C3_API_KEY`, then the `c3 login` session. Lets agents bill C3 to different keys. |
| `env_image`   | Docker Hub image for the job. Defaults: `rust:1-bookworm` (CPU) or `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` (GPU). Use `env_cpu` / `env_gpu` to set each separately.            |
