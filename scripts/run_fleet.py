#!/usr/bin/env python3
"""Fleet launcher — run multiple swarm agents from one repo via git worktrees.

Each entry in fleet.config.json gets:
  - its own git worktree at worktrees/<name>/ on branch fleet/<name>
  - its own agent.config.json (registers a fresh swarm agent on first run,
    resumes the persisted agent_id on subsequent runs)
  - a subprocess running scripts/run_loop.py inside that worktree

All children stream stdout through this process, prefixed by agent name.
Ctrl-C terminates the whole fleet.

Usage:
    python scripts/run_fleet.py                    # spawn everyone
    python scripts/run_fleet.py --only claude-1    # spawn just one (repeatable)
    python scripts/run_fleet.py --list             # status table, then exit
    python scripts/run_fleet.py --clean            # remove every fleet worktree
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLEET_CONFIG_PATH = ROOT / "fleet.config.json"
WORKTREES_DIR = ROOT / "worktrees"

# Fields on a fleet entry that are forwarded into the worktree's
# agent.config.json. run_loop.py reads its provider/model/compute defaults
# from there — no CLI flags needed on the subprocess.
_AGENT_CONFIG_KEYS = (
    "provider", "model", "api_base", "compute",
    "c3_hardware", "c3_time", "c3_cloud_provider", "c3_no_build",
    # Honor hand-set agent_id / agent_name in a fleet entry — useful if a user
    # wants to point a new clone at an existing dashboard agent without
    # re-registering. Normal flow: run_loop.py writes these after the first
    # /api/agents/register call so restarts resume the same identity.
    "agent_id", "agent_name",
    "log_prompts",
)

_PROVIDER_TO_DEFAULT_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "venice": "VENICE_API_KEY",
}

_COLORS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[31m"]
_RESET = "\033[0m"


# ── Config ─────────────────────────────────────────────────────────


def _load_fleet() -> tuple[str, str, str, list[dict]]:
    if not FLEET_CONFIG_PATH.exists():
        sys.exit(
            f"fleet.config.json not found at {FLEET_CONFIG_PATH}.\n\n"
            f"Run the wizard to generate one (recommended):\n"
            f"    python scripts/init_fleet.py\n\n"
            f"Or hand-edit:\n"
            f"    cp fleet.config.example.json fleet.config.json"
        )
    data = json.loads(FLEET_CONFIG_PATH.read_text())
    agents = data.get("agents") or []
    if not agents:
        sys.exit("fleet.config.json has no agents.")

    server_url = data.get("server_url") or ""
    if not server_url:
        sys.exit(
            "fleet.config.json is missing top-level `server_url`. "
            "Add it (the host who ran `setup.py create` has the URL)."
        )

    username = (data.get("username") or "").strip()
    if not username:
        sys.exit(
            "fleet.config.json is missing top-level `username`. "
            "Ask the host to run `python setup.py invite <your-name>` and "
            "paste the username + swarm_password they send you."
        )

    swarm_password = (data.get("swarm_password") or "").strip()
    if not swarm_password:
        sys.exit(
            "fleet.config.json is missing top-level `swarm_password`. "
            "Ask the host to run `python setup.py invite <your-name>` — "
            "they'll send you a derived password to paste here."
        )

    names: list[str] = []
    for entry in agents:
        name = entry.get("name")
        if not name:
            sys.exit("Every agent in fleet.config.json must have a 'name'.")
        names.append(name)
    if len(set(names)) != len(names):
        sys.exit("fleet.config.json has duplicate agent names.")
    return server_url, username, swarm_password, agents


# ── Git worktree helpers ───────────────────────────────────────────


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _existing_worktree_paths() -> set[str]:
    out = _git(["worktree", "list", "--porcelain"])
    paths: set[str] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.add(line[len("worktree "):])
    return paths


def _branch_exists(branch: str) -> bool:
    return bool(_git(["branch", "--list", branch]))


def _ensure_worktree(name: str) -> Path:
    path = WORKTREES_DIR / name
    branch = f"fleet/{name}"
    known = _existing_worktree_paths()

    if path.exists() and str(path) not in known:
        # Stale directory not tracked as a worktree — prune and rebuild.
        _git(["worktree", "prune"])
        shutil.rmtree(path)

    if not path.exists():
        WORKTREES_DIR.mkdir(exist_ok=True)
        if _branch_exists(branch):
            _git(["worktree", "add", str(path), branch])
        else:
            _git(["worktree", "add", "-b", branch, str(path)])

    return path


def _seed_worktree(
    path: Path, agent: dict,
    fleet_server_url: str, fleet_username: str, fleet_swarm_password: str,
) -> None:
    """Materialize one fleet entry into a worktree's agent.config.json and
    seed .swarm-cache.json from the host clone if one is present.

    agent.config.json is the source of truth run_loop.py reads — identity,
    provider/model/compute, and the persisted agent_id once /api/agents/register
    has returned it. The cache copy is best-effort: the first iteration's
    `setup.py sync` will populate the worktree's .swarm-cache.json regardless,
    so a missing root cache just means benchmark.py can't run until after that
    first sync.
    """
    root_cache = ROOT / ".swarm-cache.json"
    wt_cache = path / ".swarm-cache.json"
    if root_cache.exists() and not wt_cache.exists():
        # Only seed when the root cache mirrors the fleet's current server.
        # Otherwise it's a leftover from a prior swarm and would feed a stale
        # server_url straight into setup.py sync. A skipped seed just means
        # benchmark.py waits one extra iteration for the first sync to land.
        try:
            cached = json.loads(root_cache.read_text())
            cached_url = (cached.get("server_url") or "").rstrip("/")
        except (json.JSONDecodeError, OSError):
            cached_url = ""
        if cached_url and cached_url == fleet_server_url.rstrip("/"):
            shutil.copy2(root_cache, wt_cache)

    wt_agent = path / "agent.config.json"
    existing: dict = {}
    if wt_agent.exists():
        try:
            parsed = json.loads(wt_agent.read_text())
            if isinstance(parsed, dict):
                existing = parsed
        except json.JSONDecodeError:
            pass

    merged = dict(existing)
    for key in _AGENT_CONFIG_KEYS:
        if key in agent:
            merged[key] = agent[key]
    # The example config uses "hardware" as the friendly name; run_loop.py
    # reads "c3_hardware" first and falls back to "hardware", so normalize.
    if "hardware" in agent and "c3_hardware" not in agent:
        merged["c3_hardware"] = agent["hardware"]
    # Materialize identity + server_url + credentials so run_loop.py can
    # read everything it needs from agent.config.json alone.
    merged["name"] = agent["name"]
    merged["server_url"] = fleet_server_url
    merged["username"] = fleet_username
    merged["swarm_password"] = fleet_swarm_password
    if agent.get("tacit_knowledge"):
        merged["tacit_knowledge"] = agent["tacit_knowledge"]
    wt_agent.write_text(json.dumps(merged, indent=2) + "\n")

    tacit = agent.get("tacit_knowledge")
    if tacit:
        src = Path(tacit)
        if not src.is_absolute():
            src = ROOT / src
        if not src.exists():
            sys.exit(f"Agent {agent['name']}: tacit_knowledge file not found: {src}")
        shutil.copy2(src, path / "tacit_knowledge_personal.md")


# ── API keys ───────────────────────────────────────────────────────


def _resolve_api_key(agent: dict) -> tuple[str | None, str | None]:
    """Return (env_var_to_set, value) for this agent's subprocess.

    Returns (None, None) for claude-code, claude-code-agentic, and
    codex-agentic — all three use their respective CLI's local auth
    (OAuth / subscription / `codex login`). Exits with a clear message if
    a required env var is missing for an API-key provider.
    """
    provider = agent.get("provider") or "anthropic"
    if provider in ("claude-code", "claude-code-agentic", "codex-agentic"):
        return None, None
    if provider not in _PROVIDER_TO_DEFAULT_ENV:
        sys.exit(f"Agent {agent['name']}: unknown provider {provider!r}")

    target = _PROVIDER_TO_DEFAULT_ENV[provider]
    source = agent.get("api_key_env") or target
    value = os.environ.get(source, "")
    if not value:
        sys.exit(
            f"Agent {agent['name']}: environment variable {source} is unset or empty."
        )
    return target, value


# ── First-run bootstrap ────────────────────────────────────────────


def _ensure_root_swarm_cache(server_url: str) -> None:
    """Run `setup.py sync` once at the host root so .swarm-cache.json exists
    before _seed_worktree tries to copy it into each worktree.

    `setup.py sync` is idempotent: a no-op if the cache is already current and
    its server_url matches `fleet.config.json`. We always run it because on a
    fresh contributor clone there is no cache yet, and run_loop.py reads the
    cache before its own per-iteration sync would run."""
    cache = ROOT / ".swarm-cache.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            cached_url = (cached.get("server_url") or "").rstrip("/")
        except (json.JSONDecodeError, OSError):
            cached_url = ""
        # Stale cache from a different swarm: drop it so sync writes a fresh one.
        if cached_url and cached_url != server_url.rstrip("/"):
            cache.unlink()

    print(f"  [fleet] syncing swarm state from {server_url}…")
    result = subprocess.run(
        [sys.executable, str(ROOT / "setup.py"), "sync"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        sys.exit(f"  [fleet] setup.py sync failed:\n{err}")
    if not cache.exists():
        out = (result.stdout or "").strip()
        sys.exit(
            f"  [fleet] sync ran but produced no .swarm-cache.json — the swarm "
            f"server may be unreachable.\n"
            f"  Tried: {server_url}\n"
            f"  setup.py output:\n{out}"
        )


# ── Streaming ──────────────────────────────────────────────────────


def _stream_output(name: str, color: str, proc: subprocess.Popen) -> None:
    prefix = f"{color}[{name}]{_RESET} " if color else f"[{name}] "
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(prefix + line)
        sys.stdout.flush()


# ── Subcommands ────────────────────────────────────────────────────


def cmd_list(agents: list[dict]) -> int:
    known = _existing_worktree_paths()
    print(f"  {'name':20s}  {'worktree':10s}  {'agent_id':40s}  path")
    for agent in agents:
        name = agent["name"]
        path = WORKTREES_DIR / name
        present = "ok" if str(path) in known else "missing"
        agent_id = "<unregistered>"
        wt_agent = path / "agent.config.json"
        if wt_agent.exists():
            try:
                data = json.loads(wt_agent.read_text())
                agent_id = data.get("agent_id") or "<unregistered>"
            except json.JSONDecodeError:
                pass
        print(f"  {name:20s}  {present:10s}  {agent_id:40s}  {path}")
    return 0


def cmd_clean(agents: list[dict]) -> int:
    for agent in agents:
        name = agent["name"]
        path = WORKTREES_DIR / name
        branch = f"fleet/{name}"
        if path.exists():
            try:
                _git(["worktree", "remove", "--force", str(path)])
                print(f"  removed worktree {path}")
            except RuntimeError as e:
                print(f"  could not remove {path}: {e}", file=sys.stderr)
        if _branch_exists(branch):
            try:
                _git(["branch", "-D", branch])
                print(f"  deleted branch {branch}")
            except RuntimeError as e:
                print(f"  could not delete branch {branch}: {e}", file=sys.stderr)
    _git(["worktree", "prune"])
    return 0


def cmd_run(
    agents: list[dict],
    only: list[str] | None,
    server_url: str,
    username: str,
    swarm_password: str,
) -> int:
    if only:
        names = {a["name"] for a in agents}
        unknown = [n for n in only if n not in names]
        if unknown:
            sys.exit(f"Unknown agent name(s) in --only: {', '.join(unknown)}")
        agents = [a for a in agents if a["name"] in only]

    # Resolve every API key up front so missing secrets fail fast before any
    # worktree work or subprocess starts.
    key_envs = [_resolve_api_key(a) for a in agents]

    # Make sure .swarm-cache.json exists at root before any worktree is seeded.
    # _seed_worktree copies the root cache into each worktree; without it,
    # run_loop.py's first call to load_config() would bail with the legacy
    # "Run `python setup.py sync` first" error that contributors aren't
    # expected to know how to fix.
    _ensure_root_swarm_cache(server_url)

    use_color = sys.stdout.isatty()
    procs: list[tuple[str, subprocess.Popen, threading.Thread]] = []

    for i, agent in enumerate(agents):
        name = agent["name"]
        print(f"  [fleet] preparing {name}…")
        path = _ensure_worktree(name)
        _seed_worktree(path, agent, server_url, username, swarm_password)

        env = os.environ.copy()
        target, value = key_envs[i]
        if target and value:
            env[target] = value
        # Stdout is piped (not a TTY), so Python would block-buffer the child's
        # output and the fleet would look silent until buffers fill. Force
        # line-buffered I/O so [BENCH]/registration prints stream live.
        env["PYTHONUNBUFFERED"] = "1"

        color = _COLORS[i % len(_COLORS)] if use_color else ""
        cmd = [sys.executable, "scripts/run_loop.py"]
        proc = subprocess.Popen(
            cmd, cwd=path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        t = threading.Thread(
            target=_stream_output, args=(name, color, proc), daemon=True,
        )
        t.start()
        procs.append((name, proc, t))
        print(f"  [fleet] spawned {name} (pid {proc.pid}) in {path}")

    print(f"  [fleet] {len(procs)} agent(s) running. Ctrl-C to stop.")

    stopping = False

    def _shutdown(_signum, _frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("\n  [fleet] shutdown signal — terminating agents…")
        for _, p, _t in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 10
        for nm, p, _t in procs:
            remaining = max(0.0, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                print(f"  [fleet] killing {nm} (didn't exit in 10s)")
                p.kill()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while any(p.poll() is None for _, p, _ in procs):
        time.sleep(1)

    for name, p, t in procs:
        t.join(timeout=2)
        print(f"  [fleet] {name} exited with code {p.returncode}")
    return 0


# ── Entry point ────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run multiple swarm agents from one repo via git worktrees.",
    )
    p.add_argument(
        "--only", action="append",
        help="Run only this agent (repeatable). Default: all agents in fleet.config.json.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--list", action="store_true",
        help="Print agent / worktree status and exit.",
    )
    g.add_argument(
        "--clean", action="store_true",
        help="Remove every fleet worktree and its throwaway branch, then exit.",
    )
    args = p.parse_args()

    server_url, username, swarm_password, agents = _load_fleet()
    if args.list:
        return cmd_list(agents)
    if args.clean:
        return cmd_clean(agents)
    return cmd_run(agents, args.only, server_url, username, swarm_password)


if __name__ == "__main__":
    sys.exit(main())
