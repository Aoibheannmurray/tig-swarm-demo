# AI assistant — helping a contributor join this TIG swarm

You are a coding assistant (Codex, Claude Code, Cursor, etc.) helping a human
contributor set up their machine to join an existing TIG swarm. The user has
probably pasted a short snippet like:

```
git clone https://github.com/Aoibheannmurray/tig-swarm-demo.git && cd tig-swarm-demo && python run.py
server_url:     https://…railway.app
username:       <their-handle>
swarm_password: <hex string from the swarm host>
```

Your job is to get `python run.py` running cleanly. Nothing more.

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
- **Docker** — benchmarks run inside a container. The user needs the
  `docker` CLI on PATH; the daemon being stopped is fine (`run_fleet.py`
  auto-launches Docker Desktop / OrbStack via `scripts/benchmark.py`).

**Do not install Rust on the host.** Rust lives inside the Docker image
(`Dockerfile.cpu` / `Dockerfile.gpu`). Installing it locally is wasted effort
and often fails on managed machines.

## If Docker is missing

`python run.py` preflight-checks for `docker` on PATH and exits with a link
to Docker Desktop. **Send the user to that link.** Do *not* try to install
Docker yourself via Homebrew / apt / dnf:

- macOS without Homebrew → fragile.
- Linux server installs of dockerd → need sudo and typically conflict with
  Docker Desktop.
- The official installer asks for permissions interactively that you can't
  grant from a coding-assistant shell.

After the user installs Docker Desktop and finishes its first-run setup,
resume from `python run.py`.

## The wizard (invoked by `run.py`, also runnable as `scripts/init_fleet.py`)

It prompts for, in order:

1. `server_url`
2. `username`
3. `swarm_password`
4. LLM provider (Anthropic / OpenAI / Google / Venice / OpenRouter /
   `claude` CLI / `codex` CLI — pick what the user has credentials for)
5. Model (Enter accepts the per-provider default)
6. Fleet size (number of parallel agents — default 1)

On a **re-run** against an existing `fleet.config.json` the wizard adds a
`Keep these connection settings? [Y/n]` step right before (1) — Enter
reuses the previous `server_url` / `username` / `swarm_password` and skips
straight to (4). The user only ends up retyping the connection triplet if
they type `n`.

For non-interactive setup on a fresh clone (no existing config) you can
pipe answers via stdin, e.g.:

```bash
printf '%s\n' "$SERVER_URL" "$USERNAME" "$SWARM_PASSWORD" "1" "" "1" | python scripts/init_fleet.py
```

(Provider/model defaults pick Anthropic + `claude-opus-4-7`. Adjust the
sequence if the user wants a different provider — see the prompts in
`scripts/init_fleet.py`.) On a re-run, add a leading `"y"` for the keep-
settings prompt and drop the three connection lines.

**Fleet size.** The trailing `"1"` in that pipe is the *number of agents to
spawn in parallel* — not a Yes/No. Default to 1 unless the user explicitly
asks for more (e.g. "set me up with 3 agents"); then change the last `"1"`
to that number. The wizard auto-generates unique `<adjective>-<noun>` names
for the extra agents. If the user already has a `fleet.config.json` and
wants to grow it, just duplicate one agent entry under `agents: [...]` with
a new unique `name` — no need to re-run the wizard.

The wizard writes `fleet.config.json` and prints a one-line summary —
`wrote fleet.config.json — N agent(s): <names>` — plus an inline reminder
only when the chosen provider needs an API key that isn't yet exported
(`reminder: export ANTHROPIC_API_KEY=<your-key> before launching`). CLI-auth
providers (`claude-code`, `claude-code-agentic`, `codex-agentic`) skip the
reminder; they use the CLI's own login. **Never write API keys into
`fleet.config.json` yourself.**

## Launching

```bash
python run.py
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
python scripts/run_fleet.py --list     # show agent names, ids, status
python scripts/run_fleet.py --clean    # remove every worktree + branch
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

- **Docker Desktop requires WSL 2.** First-run Docker on Windows needs WSL 2 +
  Virtual Machine Platform enabled (admin PowerShell: `wsl --install`, reboot,
  `wsl --update`). Without this, the Docker daemon never starts and benchmarks
  hang forever.
- **`fleet.config.json` BOM.** PowerShell `Set-Content` writes UTF-8 *with* a
  BOM by default. The loader now reads via `utf-8-sig` so this should no longer
  break things, but the safe write idiom is
  `$json | Out-File -Encoding utf8NoBOM fleet.config.json`. Better still: use
  `python run.py` (which calls the wizard).
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
  is thinking — up to `--agentic-timeout` seconds (default 900). The
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
  (When the wizard is invoked through `python run.py`, this is handled by
  the `Update your fleet config? (y/N)` prompt up front.)

- **`Agent <name>: environment variable ANTHROPIC_API_KEY is unset or empty.`**
  → The contributor picked an API-key provider but never exported the key.
  Tell them to run the suggested `export …=<your-key>` command from the
  error and re-run `python run.py`. CLI-auth providers (`claude-code`,
  `claude-code-agentic`, `codex-agentic`) never produce this error — they
  log in through their CLI instead.

## What you should *not* do

- Don't edit Rust files, `CHALLENGE.md`, or `initial_algorithms/`.
- Don't try to register agents via the HTTP API yourself — that's
  `register_agent` in `scripts/server.py`, called by `run_loop.py`.
- Don't install Rust, cargo, or Python packages on the host.
- Don't push, force-push, or create commits unless the user explicitly asks.
- Don't paste the user's `swarm_password` into chat output or commit it.
  It belongs only in `fleet.config.json` (gitignored).

For deeper context on how the swarm works internally, see
[ARCHITECTURE.md](./ARCHITECTURE.md). The end-user-facing instructions are
in [README.md](./README.md).
