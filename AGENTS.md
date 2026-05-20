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
