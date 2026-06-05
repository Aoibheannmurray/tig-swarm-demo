# AI assistant — helping a contributor join this TIG swarm

You are a coding assistant (Codex, Claude Code, Cursor, etc.) helping a human
contributor set up their machine to join an existing TIG swarm. The user has
probably pasted a short snippet like:

```
git clone https://github.com/tig-foundation/prometheus-early-beta && cd prometheus-early-beta && python3 run.py
server_url:     https://…railway.app
username:       <their-handle>
swarm_password: <hex string from the swarm host>
c3_api_key:     <c3_… key, optional — for C3 cloud GPU compute>
```

Your job is to get `python3 run.py` running cleanly. Nothing more.

If the contributor includes a `c3_api_key`, they want **C3 cloud GPU**
compute. The wizard's connection paste reads `c3_api_key` straight out of
that block (alongside `server_url` / `username` / `swarm_password`), stores
it as the fleet-wide top-level `c3_api_key`, and skips the "enter your C3
key" step — so just feed the whole block to the paste prompt. No `c3_api_key`
means they either run on a **local GPU** (pick "Local Docker" at the compute
step — benchmarks use the machine's NVIDIA GPU via `--gpus all`) or will
`export C3_API_KEY` themselves.

`run.py` is the single contributor entry point. It orchestrates four phases
in one command:

1. Preflight (`docker` on PATH).
2. **Init wizard** if `fleet.config.json` is missing. On a re-run with the
   file already present, asks `Update your fleet config (provider / model
   / agent count)? (y/N)` (default No). On yes, re-enters the wizard with
   `force=True` and offers to keep the existing `server_url` / `username`
   / `swarm_password` triplet so the user doesn't have to paste them again.
3. **Tacit-knowledge phase**. First run (no source file has real content
   yet) jumps straight into the create wizard. Subsequent runs gate behind
   `Add or edit tacit knowledge for your agent(s)? (y/N)` (default No); on
   yes, the per-path wizard auto-picks the create menu (file missing /
   stub-only) or the edit menu (file has user content). Edit menu offers
   an `Open in $EDITOR` option for direct hand-editing. By default every
   agent in the fleet shares one file (`tacit_knowledge.md` at repo root),
   so the wizard runs once and all agents benefit.
4. **Launches the fleet** — same logic as `scripts/run_fleet.py`. On
   shutdown, any `- LLM:` lessons agents appended in their worktrees are
   collated back into the shared `tacit_knowledge.md` (deduped against
   existing entries), so distillations accumulate across runs and across
   agents.

The underlying scripts (`scripts/init_fleet.py`, `setup.py tacit`,
`scripts/run_fleet.py`) all still work standalone for power-user / scripted
flows. `run.py` just calls into them, so behavior is identical.

## What this repo is (so you don't get pulled off-mission)

A swarm of LLM agents collaboratively optimize a Rust solver for a TIG
challenge. The swarm-runtime agents are *separate* from you — they run in
sandboxed worktrees launched by `scripts/run_loop.py` and receive their own
briefing (a different `AGENTS.md` materialized at runtime by
`scripts/agentic_backends.py`). **You are not one of those agents.** Don't try
to edit the algorithm, run `cargo`, or read `CHALLENGE.md` — that's the
runtime agent's job, not yours.

## Host-side requirements (the only two)

- **Python 3** — for the wizard and fleet driver. Stdlib only; **no pip
  install needed**. Do not run `pip install -r requirements.txt`. That
  `requirements.txt` is for the benchmark Docker image, not the host.
  Examples use `python3` (macOS Homebrew and most Linux have no bare
  `python`). On Windows use `python`. Child processes are spawned with
  `sys.executable`, so whichever interpreter starts `run.py` is reused.
- **Docker** — benchmarks run inside a container. The user needs the
  `docker` CLI on PATH; the daemon being stopped is fine (`run_fleet.py`
  auto-launches Docker Desktop / OrbStack via `scripts/benchmark.py`).

**Do not install Rust on the host.** Rust lives inside the Docker image
(`Dockerfile.cpu` / `Dockerfile.gpu`). Installing it locally is wasted effort
and often fails on managed machines.

## If Docker is missing

`python3 run.py` preflight-checks for `docker` on PATH and exits with a link
to Docker Desktop. **Send the user to that link.** Do *not* try to install
Docker yourself via Homebrew / apt / dnf:

- macOS without Homebrew → fragile.
- Linux server installs of dockerd → need sudo and typically conflict with
  Docker Desktop.
- The official installer asks for permissions interactively that you can't
  grant from a coding-assistant shell.

After the user installs Docker Desktop and finishes its first-run setup,
resume from `python3 run.py`.

## The wizard (invoked by `run.py`, also runnable as `scripts/init_fleet.py`)

It prompts for, in order:

1. `server_url`
2. `username`
3. `swarm_password`
4. LLM provider (Anthropic / OpenAI / Google / Venice / OpenRouter /
   `claude` CLI / `codex` CLI — pick what the user has credentials for)
5. Model (Enter accepts the per-provider default). To see what model IDs a
   provider offers, run `python scripts/list_models.py <provider>` — it queries
   the provider's live models endpoint (reads the API key from the env; the
   CLI providers have no endpoint and accept any ID their CLI knows).
6. Fleet size (number of parallel agents — default 1)
7. Compute backend — **C3 cloud GPU (default)** or local Docker. GPU swarm
   agents default to C3, so this step defaults to `c3`. Picking `c3` then asks
   for the GPU profile (`l40` default / `a100` / `h100`) and the C3 API key.
   The key is written as a fleet-wide top-level `c3_api_key`. **If the
   contributor already pasted a `c3_api_key` with their connection details
   (step 1), the wizard reuses it and skips this prompt** — otherwise enter it
   here, or leave it blank to fall back to the `C3_API_KEY` env var or an
   existing `c3 login` session. Choosing **local Docker** needs no key and runs
   benchmarks on the machine's own NVIDIA GPU. Providers that don't support C3
   skip this step and stay local.

On a **re-run** against an existing `fleet.config.json` the wizard adds a
`Keep these connection settings? [Y/n]` step right before (1) — Enter
reuses the previous `server_url` / `username` / `swarm_password` (and the
stored `c3_api_key`, if any) and skips straight to (4). The user only ends up
retyping the connection details if they type `n`.

For non-interactive setup on a fresh clone (no existing config) you can
pipe answers via stdin, e.g.:

```bash
printf '%s\n' \
  "server_url: $SERVER_URL" "username: $USERNAME" "swarm_password: $SWARM_PASSWORD" \
  "c3_api_key: $C3_API_KEY" "" \
  "" "" "1" "" "" | python3 scripts/init_fleet.py
```

The connection lines must be fed as `key: value` (the wizard parses them out
of the paste block) — include `c3_api_key` here for C3 compute. The lone `""`
after them ends the paste. The next `""` `""` `1` accept the default
provider/model and set fleet size, and the final `""` `""` drive the compute
step: the first accepts the default **C3** backend, the second accepts the
default `l40` GPU profile. Because the key came in the paste block, the wizard
does **not** prompt for it again — so don't add a trailing key field. To defer
C3 auth to the `C3_API_KEY` env var / `c3 login`, drop the `c3_api_key:` line.
To run benchmarks on a **local GPU** instead, drop the `c3_api_key:` line and
replace the final compute block with a single `"2"` (the local-Docker choice),
dropping the GPU-profile line. Adjust the provider/model fields if the user
wants a non-default provider — see the prompts in `scripts/init_fleet.py`. On
a re-run, add a leading `"y"` for the keep-settings prompt and drop the
connection lines.

**Fleet size.** The trailing `"1"` in that pipe is the *number of agents to
spawn in parallel* — not a Yes/No. Default to 1 unless the user explicitly
asks for more (e.g. "set me up with 3 agents"); then change the last `"1"`
to that number. The wizard auto-generates unique `<adjective>-<noun>` names
for the extra agents. If the user already has a `fleet.config.json` and
wants to grow it, just duplicate one agent entry under `agents: [...]` with
a new unique `name` — no need to re-run the wizard.

The wizard writes `fleet.config.json` and prints a one-line summary —
`wrote fleet.config.json — N agent(s): <names>` — plus an inline reminder
only when the chosen provider needs an API key that isn't yet set
(`reminder: export ANTHROPIC_API_KEY=<your-key> before launching` on
macOS/Linux; on Windows the reminder prints the `set …` / `$env:…` form
instead). CLI-auth providers (`claude-code`, `claude-code-agentic`,
`codex-agentic`) skip the reminder; they use the CLI's own login. **Never
write API keys into `fleet.config.json` yourself.**

## Launching

```bash
python3 run.py
```

`run.py` reuses the existing `fleet.config.json` after asking
`Update your fleet config? (y/N)` (default No), then handles the tacit
phase — silently jumping to the create menu on a first run, or asking
`Add or edit tacit knowledge? (y/N)` on subsequent runs — and launches. Each
agent spawns its own git worktree under `worktrees/<name>/`. Output is
prefixed by agent name. `Ctrl-C` terminates the whole fleet. If the user
restarts later, agent identities persist via `worktrees/<name>/agent.config.json`.

Useful fleet management (still on `scripts/run_fleet.py`, since `run.py`
doesn't forward management flags):

```bash
python3 scripts/run_fleet.py --list     # show agent names, ids, status
python3 scripts/run_fleet.py --clean    # remove every worktree + branch
```

## Tacit-knowledge from a coding-agent session

`run.py` skips its tacit prompts entirely when stdin isn't a TTY (your
Bash tool's case), so the interactive wizard isn't the right channel for
piping notes through. If the contributor wants to seed tacit-knowledge
hints, write them directly to `tacit_knowledge.md` at the repo root
before launching:

```markdown
# Personal tacit knowledge

## Strategies

- when local search plateaus, perturb the neighborhood structure before
  switching metaheuristic
- large-neighborhood search underperforms on tight feasibility regions
```

The file is gitignored, never sent to the server, and read by every
agent in the fleet by default. Format: a `## Strategies` section
followed by one bullet per insight. Agents will also append their own
`- LLM: …` failure-lessons here as they hit dead ends.

## Windows-specific gotchas

If the contributor is on Windows, walk through these *before* debugging anything
else — they're the failure modes our docs have actually been bitten by:

- **Docker Desktop install order (the #1 beta pain).** Walk the contributor
  through this exact sequence — skipping a step is what caused the "benchmarks
  hang forever" reports:
  1. **Enable WSL 2 first.** Admin PowerShell: `wsl --install`, **reboot**,
     then `wsl --update`. This also enables the Virtual Machine Platform
     feature. If WSL 2 isn't in place, the Docker daemon never starts and
     benchmarks hang forever — this is the single most common failure.
  2. **Install Docker Desktop** from
     https://www.docker.com/products/docker-desktop/ and launch it once so it
     finishes first-run setup.
  3. **Confirm the daemon is up:** `docker info` should succeed (not just
     `docker --version`, which works even when the daemon is stopped). The
     wizard's preflight only checks `docker` is on PATH, so a stopped daemon
     still passes preflight but stalls at the first benchmark.
  4. **Check free disk space.** The benchmark image + cargo/Docker build cache
     want **~15–20 GB free**. The wizard now prints a low-disk warning under
     ~15 GB; if a build dies with "no space left on device", run
     `docker system prune` and free up the drive. Docker Desktop's disk image
     also grows over time — reclaim it via Settings → Resources if needed.
- **`fleet.config.json` BOM.** PowerShell `Set-Content` writes UTF-8 *with* a
  BOM by default. The loader now reads via `utf-8-sig` so this should no longer
  break things, but the safe write idiom is
  `$json | Out-File -Encoding utf8NoBOM fleet.config.json`. Better still: use
  `python3 run.py` (which calls the wizard).
- **Codex CLI: prefer the npm install.** The Windows Store alias for `codex`
  (`%LOCALAPPDATA%\Microsoft\WindowsApps\codex.exe`) commonly returns
  `Access is denied` when invoked from a subprocess. Have the user run
  `npm install -g @openai/codex`, then either ensure the npm `codex.cmd` is
  ahead of the Store alias on PATH or set
  `$env:CODEX_CLI = "$env:APPDATA\npm\codex.cmd"`. `agentic_backends.py` and
  `run_loop.py` both honor that env var.
- **Claude CLI: same shape.** The npm install
  (`npm install -g @anthropic-ai/claude-code`) puts `claude.cmd` at
  `%APPDATA%\npm\claude.cmd`. Usually fine on PATH out of the box, but if the
  precheck still can't find it (or another `claude.exe` further up PATH is
  winning), pin it explicitly:
  `$env:CLAUDE_CLI = "$env:APPDATA\npm\claude.cmd"`. Same resolver, same
  fallback behavior. Unlike Codex, `claude-code-agentic` does *not* care
  whether the model name is `gpt-5`-style: the Claude CLI accepts any model
  ID it knows.
- **Codex + ChatGPT login.** A ChatGPT-account-authenticated Codex CLI
  *rejects* model IDs such as `gpt-5`. The wizard default for `codex-agentic`
  is empty — accept it (Codex picks its own supported default). Do not guess
  a model name.
- **Agentic providers look frozen.** `claude-code-agentic` and `codex-agentic`
  run with `capture_output=True`, so there is no live stdout while the agent
  is thinking — up to `--agentic-timeout` seconds (default 1800). The
  background heartbeat keeps the dashboard happy; Docker stays idle until the
  agent returns and `[BENCH]` starts. If the contributor reports "the terminal
  is stuck," confirm an `[AGENTIC] Launching …` line is already on screen and
  ask them to wait.
- **PowerShell launch.** Tell them to `Set-Location` to the repo first; the
  scripts assume cwd is the repo root. If `python` isn't on PATH, the bundled
  Codex Python at
  `%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
  works as a fallback.

## Common failure modes & resolution

- **`Docker is required to run benchmarks but \`docker\` was not found on PATH.`**
  → Direct user to https://www.docker.com/products/docker-desktop/. Do not
  attempt to install Docker yourself.

- **403 on heartbeat / `agent token rejected` at startup**
  → The swarm host has revoked this contributor. The wizard's
  `swarm_password` will also fail. Tell the user to ask the host for a
  fresh invite.

- **`This agent's access has been revoked`** at fleet startup
  → Same as above — revoked contributor. Re-running won't help.

- **Benchmark crashes claiming missing crates / Rust toolchain on the host**
  → The user has a stale image or accidentally ran `cargo` outside Docker.
  Run `docker build -f Dockerfile.cpu -t tig-swarm-cpu .` from the repo root.

- **`fleet.config.json already exists. Overwrite?`**
  → They've run the wizard before. Either answer `y` or run with `--force`.
  (When the wizard is invoked through `python3 run.py`, this is handled by
  the `Update your fleet config? (y/N)` prompt up front.)

- **`Agent <name>: environment variable ANTHROPIC_API_KEY is unset or empty.`**
  → The contributor picked an API-key provider but never set the key. Tell
  them to run the suggested set-the-env-var command from the error and re-run
  `python3 run.py`. **Use the platform-correct form:** macOS/Linux
  `export KEY=<your-key>`; Windows `cmd` `set KEY=<your-key>` (no quotes,
  no `export`); Windows PowerShell `$env:KEY="<your-key>"`. The env var must
  be set in the *same* shell session that launches `python3 run.py`. CLI-auth
  providers (`claude-code`, `claude-code-agentic`, `codex-agentic`) never
  produce this error — they log in through their CLI instead.

## What you should *not* do

- Don't edit Rust files, `CHALLENGE.md`, or `initial_algorithms/`.
- Don't try to register agents via the HTTP API yourself — that's
  `register_agent` in `server/server.py`, called by `run_loop.py`.
- Don't install Rust, cargo, or Python packages on the host.
- Don't push, force-push, or create commits unless the user explicitly asks.
- Don't paste the user's `swarm_password` into chat output or commit it.
  It belongs only in `fleet.config.json` (gitignored).

For deeper context on how the swarm works internally, see
[ARCHITECTURE.md](./ARCHITECTURE.md). The end-user-facing instructions are
in [README.md](./README.md).
