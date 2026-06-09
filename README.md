# TIG Swarm Demo

Multiple LLM agents optimize TIG challenge solvers in Rust, coordinated by a FastAPI server and live dashboard.

Each contributor runs `python3 run.py`, which spawns one or more agents — each calling an LLM (Anthropic, OpenAI, Google, OpenRouter, Venice, or your local `claude` / `codex` CLI) in a loop and contributing to the swarm.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for internals.

## Host

Requirements: Python 3, Railway CLI, Railway account.

```bash
railway login
python3 setup.py create              # deploys a Railway swarm, scaffolds fleet.config.json
python3 setup.py switch energy_arbitrage     # change the active challenge later
```

`setup.py` is host-only. Contributors run `python3 run.py`.

## Contributor

Requirements:
- Python 3
- Git (each agent runs in its own git worktree)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), running — **only if you benchmark locally** (`"compute": "local"`). Not needed when all agents benchmark on C3, which runs every benchmark remotely. (Windows local Docker also needs WSL 2.)
- Either an API key for your chosen provider, or a logged-in `claude` / `codex` CLI

> **Windows:** use `python` instead of `python3` in the commands below. On macOS
> (Homebrew) and most Linux the interpreter is `python3` and there is no bare
> `python`, so the examples use `python3`.

No terminal handy? Open this repo in [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://docs.claude.com/en/docs/claude-code) — both read `AGENTS.md` and walk you through setup.

```bash
python3 run.py
```

It walks you through setup the first time, then just launches on subsequent runs (a couple of optional update prompts you can skip with Enter).

Export your keys before launching — your provider key (skip if you use a `claude` / `codex` CLI login) and `C3_API_KEY` for the GPU compute:

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

| field            | meaning                                                                 |
|------------------|-------------------------------------------------------------------------|
| `name`           | Worktree dir + dashboard label.                                         |
| `provider`       | LLM provider — see [Providers](#providers).                             |
| `model`          | Model ID. Run `python scripts/list_models.py <provider>` to see what's available; per-provider defaults live in `DEFAULT_MODELS` (`scripts/llm_backends.py`). |
| `api_key_env`    | Env var holding the API key. Omit for CLI-auth providers.               |
| `api_base`       | Optional override of the provider's base URL (e.g. an OpenAI-compatible gateway like OpenRouter: `https://openrouter.ai/api/v1`). |
| `tacit_knowledge`| Optional per-agent override of the shared `tacit_knowledge.md` file.    |
| `detailed_prompts`| Optional `true` to send a stricter, rule-based Rust prompt. Helps smaller/cheaper models whose code often fails to compile; leave off for frontier models to save tokens. |
| `role`           | `explorer` (default) writes novel/ambitious algorithms; `exploiter` makes only small localized edits, never a rewrite. **Hot-editable** — change it in `fleet.config.json` while the fleet runs and it takes effect on the agent's next iteration. |

Remember to add your hints to `tacit_knowledge.md` — see [Tacit knowledge](#tacit-knowledge) below.

Now that you've manually set up your `fleet.config.json` and `tacit_knowledge.md`, you can run the fleet (make sure you've exported your API keys first):

`python3 scripts/run_fleet.py`

### Tacit knowledge

`tacit_knowledge.md` is a private hints file your agents read when they get stuck. It's gitignored and never leaves your machine. All your agents share it by default, so insights accumulate across the whole fleet.

Agents also **write back to it**: when one has been failing for a stretch and is about to start over from scratch, it adds a one-line `- LLM:` "what didn't work" note — so future attempts can avoid the same dead end.

To add your own hints, accept the `Add tacit knowledge?` prompt in `run.py`, or run `python3 setup.py tacit` directly. Both append rather than overwrite, and the edit menu can open the file in your `$EDITOR`. Deeper detail — when agents append, how files resolve per agent — lives in [ARCHITECTURE.md](./ARCHITECTURE.md#tacit-knowledge).

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

**Which models can I use?** Run `python scripts/list_models.py <provider>` to
print a provider's live model list — the IDs you can drop into the `model`
field above:

```bash
python scripts/list_models.py                 # providers + their defaults
python scripts/list_models.py anthropic       # Anthropic's models
python scripts/list_models.py openrouter      # OpenRouter (no key needed)
```

It reads the provider's API key from the environment (the same var in the
table above); OpenRouter's catalog is public so no key is needed there. The
CLI providers (`claude-code`, `claude-code-agentic`, `codex-agentic`) have no
models endpoint — they accept any model ID their CLI knows.

`claude-code` is single-shot: the CLI returns a code blob each iteration. The `-agentic` providers run a tooled headless agent in a sandboxed git worktree — far more capable per iteration but burn ~5–20× tokens; subscription-only. They run silently for up to 15 min per iteration; don't kill the terminal if there's no output — heartbeats keep the dashboard alive, and `[BENCH]` lines appear once the agent returns.

## Reading the score

Each iteration prints a `[BENCH]` line — the aggregate `Score`, `Feasible`, and a per-track breakdown:

```
[BENCH] Score: -199814  Feasible: False
        Track 0: 52000
        Track 1: -1000000  (below baseline)
        Track 2: 46800
```

The aggregate is a **shifted geometric mean** across tracks, and a failed or infeasible track is assigned a large fixed penalty. Because of that penalty, **a single bad track can drag the whole aggregate negative** even when the other tracks scored well. 

## Local files

Swarm state lives on the server. Local files only tell this clone how to connect and run:

| file                  | purpose                                                       |
|-----------------------|---------------------------------------------------------------|
| `fleet.config.json`   | Your fleet's agents (user-edited).                            |
| `tacit_knowledge.md`  | Your private hint file (gitignored).                          |
| `.swarm-cache.json`   | Auto-refreshed mirror of `/api/swarm_config`.                 |
| `swarm.admin.json`    | Host-only — admin key + swarm tuning.                         |

Secrets stay in environment variables.

## Remote benchmarking with C3

This swarm benchmarks on [C3](https://cthree.cloud) cloud GPUs by default — the
`run.py` wizard and `fleet.config.example.json` both set `"compute": "c3"`, so
you don't need a local GPU. To benchmark locally in Docker instead, set
`"compute": "local"` on an agent in `fleet.config.json`, then launch as usual
with `python run.py`. 

First install the `c3` CLI (from `https://cthree.cloud/install.sh`) and
authenticate, via either:

- `curl -fsSL https://cthree.cloud/install.sh | sh` to update your version of `c3` 
- `c3 login` (uses your existing session), or
- `c3 apikey create tig-swarm` then export `C3_API_KEY=...`, or
- put the key in `fleet.config.json` — a top-level `"c3_api_key"` applies to
  every agent, and a per-agent `"c3_api_key"` overrides it for that agent.
  An agent with no key set anywhere falls back to `C3_API_KEY` / `c3 login`.

<details>
<summary><strong>Windows: installing the <code>c3</code> CLI</strong> (the install script above is macOS/Linux only)</summary>

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

</details>

Then add the C3 keys to the agent:

```jsonc
{
  "name": "claude-1",
  "provider": "anthropic",
  "model": "claude-opus-4-7",
  "api_key_env": "ANTHROPIC_API_KEY",
  "compute": "c3",          // run benchmarks on C3 instead of local Docker
  "c3_hardware": "l40",     // GPU profile (default: l40)
  "c3_time": "02:00:00",    // per-job walltime (default: 02:00:00)
  "c3_provider": null,      // optional C3 backend (c3 deploy -p ...)
  "c3_api_key": null,       // optional per-agent C3 key; omit to fall back
  "env_image": null         // Docker Hub image override (see below)
}
```

| key            | purpose                                                              |
|----------------|---------------------------------------------------------------------|
| `compute`      | `"c3"` for C3 cloud GPU (the wizard & example default), `"local"` for local Docker. Omit the field and it falls back to `"local"`. |
| `c3_hardware`  | C3 GPU profile (default: `l40`).                                    |
| `c3_time`      | Per-job walltime (default: `02:00:00`).                             |
| `c3_provider`  | Optional C3 backend passed as `c3 deploy -p ...`.                  |
| `c3_api_key`   | Optional per-agent C3 API key (raw value). Omit to inherit the top-level fleet `c3_api_key`, then `C3_API_KEY`, then the `c3 login` session. Lets agents bill C3 to different keys. |
| `env_image`    | Docker Hub image for the job. Defaults: `rust:1-bookworm` (CPU) or `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` (GPU). Use `env_cpu` / `env_gpu` to set each separately. |

Each C3 benchmark runs the same `scripts/benchmark.py` inside that Docker Hub
image: the loop stages a minimal workspace, deploys it, polls until the job
finishes, then pulls the `benchmark.json` result back.
