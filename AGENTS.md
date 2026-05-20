# AI assistant — helping a contributor join this TIG swarm

You are a coding assistant (Codex, Claude Code, Cursor, etc.) helping a human
contributor set up their machine to join an existing TIG swarm. The user has
probably pasted a short snippet like:

```
git clone https://github.com/Aoibheannmurray/tig-swarm-demo.git && cd tig-swarm-demo && python scripts/init_fleet.py
server_url:     https://…railway.app
username:       <their-handle>
swarm_password: <hex string from the swarm host>
```

Your job is to get `python scripts/run_fleet.py` running cleanly. Nothing more.

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

`python scripts/init_fleet.py` preflight-checks for `docker` on PATH and
exits with a link to Docker Desktop. **Send the user to that link.** Do
*not* try to install Docker yourself via Homebrew / apt / dnf:

- macOS without Homebrew → fragile.
- Linux server installs of dockerd → need sudo and typically conflict with
  Docker Desktop.
- The official installer asks for permissions interactively that you can't
  grant from a coding-assistant shell.

After the user installs Docker Desktop and finishes its first-run setup,
resume from `python scripts/init_fleet.py`.

## The wizard (`scripts/init_fleet.py`)

It prompts for, in order:

1. `server_url`
2. `username`
3. `swarm_password`
4. LLM provider (Anthropic / OpenAI / Google / Venice / `claude` CLI /
   `codex` CLI / Venice — pick what the user has credentials for)
5. Model (Enter accepts the per-provider default)
6. Fleet size (number of parallel agents — default 1)

For non-interactive setup you can pipe answers via stdin, e.g.:

```bash
printf '%s\n' "$SERVER_URL" "$USERNAME" "$SWARM_PASSWORD" "1" "" "1" | python scripts/init_fleet.py
```

(Provider/model defaults pick Anthropic + `claude-opus-4-7`. Adjust the
sequence if the user wants a different provider — see the prompts in
`scripts/init_fleet.py`.)

**Fleet size.** The trailing `"1"` in that pipe is the *number of agents to
spawn in parallel* — not a Yes/No. Default to 1 unless the user explicitly
asks for more (e.g. "set me up with 3 agents"); then change the last `"1"`
to that number. The wizard auto-generates unique `<adjective>-<noun>` names
for the extra agents. If the user already has a `fleet.config.json` and
wants to grow it, just duplicate one agent entry under `agents: [...]` with
a new unique `name` — no need to re-run the wizard.

The wizard writes `fleet.config.json` and prints the env var the user must
export (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) — unless they picked a
CLI-auth provider (`claude-code`, `claude-code-agentic`, `codex-agentic`),
which use the CLI's own login. **Never write API keys into
`fleet.config.json` yourself.**

## Launching

```bash
python scripts/run_fleet.py
```

Each agent spawns its own git worktree under `worktrees/<name>/`. Output is
prefixed by agent name. `Ctrl-C` terminates the whole fleet. If the user
restarts later, agent identities persist via `worktrees/<name>/agent.config.json`.

Useful fleet management:

```bash
python scripts/run_fleet.py --list     # show agent names, ids, status
python scripts/run_fleet.py --clean    # remove every worktree + branch
```

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
  `python scripts/init_fleet.py`.
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
