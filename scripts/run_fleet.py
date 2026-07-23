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

# Python-version preflight — fires before any other import. `%` formatting and
# a bare `sys` import keep the message readable on Python 2.x / very old 3.x.
# Without this, contributors on older Python hit a confusing
# `TypeError: 'type' object is not subscriptable` from some downstream module
# instead of a clear "upgrade Python" pointer.
import sys
if sys.version_info < (3, 9):
    sys.stderr.write(
        "TIG swarm scripts require Python 3.9 or newer. You're running %d.%d.%d.\n"
        "Install a current Python from https://www.python.org/downloads/ and re-run.\n"
        % sys.version_info[:3]
    )
    sys.exit(1)

import argparse
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from proc_utils import group_kwargs, kill_tree, term_tree
import secrets_local

ROOT = Path(__file__).resolve().parent.parent
FLEET_CONFIG_PATH = ROOT / "fleet.config.json"
# Last-known server-hosted fleet plan, so `config_source: server` runners can
# restart while the coordination server is briefly unreachable. Gitignored.
FLEET_CACHE_PATH = ROOT / ".fleet-cache.json"
WORKTREES_DIR = ROOT / "worktrees"

# Windows PowerShell `Set-Content` writes UTF-8 with a BOM by default. Strict
# `json.loads(path.read_text())` then errors with "Expecting value: line 1
# column 1" because the BOM is not whitespace. Reading via `utf-8-sig` strips
# the BOM transparently and is a no-op for normal UTF-8.
def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON via tmp file + os.replace so concurrent readers (run_loop
    re-reads agent.config.json every iteration) never observe a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# Windows console crashes on the box-drawing / ellipsis characters this script
# prints when the active code page isn't UTF-8 ("UnicodeEncodeError: 'charmap'
# codec can't encode …"). Force the parent's stdout/stderr to UTF-8 with
# replacement so the contributor doesn't have to remember `python -X utf8`.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Fields on a fleet entry that are forwarded into the worktree's
# agent.config.json. run_loop.py reads its provider/model/compute defaults
# from there — no CLI flags needed on the subprocess.
_AGENT_CONFIG_KEYS = (
    "provider", "model", "api_base", "compute",
    "c3_hardware", "c3_time", "c3_cloud_provider", "c3_no_build",
    # Distributed C3 benchmarking (see c3_compute.py + c3_pool.py). The
    # fleet-wide C3 concurrent-job cap: also the balanced shard count per
    # benchmark. Best set once at the top level (all agents share ONE C3 key and
    # thus one FCFS slot pool); a per-agent value still overrides the pool size.
    "c3_max_parallel_jobs",
    # C3 warm-image fast path (c3_compute._warm_c3_image): boolean opt-in,
    # explicit image ref, and Docker Hub namespace override. Best set once at
    # the top level; per-agent values still override.
    "c3_warm_images", "c3_warm_image", "tig_dockerhub",
    # Per-agent C3 API key (raw value). Omit to inherit the top-level fleet
    # `c3_api_key`, the C3_API_KEY env var, or the `c3 login` session — in that
    # order. Lets each agent bill C3 to a different key without separate fleets.
    "c3_api_key",
    # Honor hand-set agent_id / agent_name in a fleet entry — useful if a user
    # wants to point a new clone at an existing dashboard agent without
    # re-registering. Normal flow: run_loop.py writes these after the first
    # /api/agents/register call so restarts resume the same identity.
    "agent_id", "agent_name",
    "log_prompts",
    # Opt-in stricter Rust prompt for smaller/cheaper models (prompts.py).
    "detailed_prompts",
    # Per-agent kill switch for tacit-knowledge writing (default True). Set
    # false to stop this agent appending `- LLM:` lessons to its
    # tacit_knowledge_personal.md (driver-side and in-band paths both gated).
    "tacit_write",
    # Contributor-owned behavior role (explorer/exploiter). Materialized at
    # spawn AND re-synced live by the monitor loop so editing it in
    # fleet.config.json takes effect on the agent's next iteration.
    "role",
    # Contributor-owned seeding override (true/false; omit for auto). true:
    # fresh trajectories start from working code (seed pool → best peer →
    # stub); false: always the bare stub. Absent, the server decides by model
    # tier/role (frontier explorers get the stub on CPU challenges, everyone
    # else a seed). Hot-reloads like `role`.
    "seeded_start",
    # API-mode edit strategy for SINGLE-FILE algorithms: "full" (default,
    # whole-file replacement) or "search_replace" (soft SEARCH/REPLACE blocks).
    # Multi-file algorithms and exploiters always use search/replace regardless.
    "edit_mode",
    # Hyperparameter-search knobs (host-tunable; see
    # docs/hyperparameter-search-plan.md). Set them once at the top level of
    # fleet.config.json and every agent inherits them as fleet-wide defaults.
    "hpo_min_improvements", "hpo_first_tune_improvements",
    "hpo_num_suggested_configs", "hpo_search_budget",
    "hpo_seed",
    # Cleaner knobs (docs/cleaner-agent-plan.md) — same passthrough pattern.
    "cleaner_trigger_chars", "cleaner_target_pct", "cleaner_score_delta_pct",
    "cleaner_cooldown_iters",
    # Freeze guard: consecutive token-spending iterations without a successful
    # benchmark before the agent exits (run_loop.py's
    # _NO_BENCHMARK_FREEZE_LIMIT). Default 10; 0 disables.
    "no_benchmark_freeze_limit",
)

# Top-level fleet keys that become fleet-wide defaults inherited by every agent
# (via setdefault, so a per-agent override still wins).
_FLEET_WIDE_DEFAULT_KEYS = (
    "hpo_min_improvements", "hpo_first_tune_improvements",
    "hpo_num_suggested_configs", "hpo_search_budget",
    "hpo_seed",
    "cleaner_trigger_chars", "cleaner_target_pct", "cleaner_score_delta_pct",
    "cleaner_cooldown_iters",
    "no_benchmark_freeze_limit",
    # Distributed C3 benchmarking: set once at the top level and every agent
    # inherits (per-agent entry still overrides via setdefault).
    "c3_max_parallel_jobs",
    # C3 warm-image fast path — same fleet-wide inheritance.
    "c3_warm_images", "c3_warm_image", "tig_dockerhub",
)

# Fleet-entry fields the monitor loop re-syncs into a running worktree's
# agent.config.json when they change. Only role and seeded_start are
# hot-reloadable today — identity/provider/model are fixed for the life of
# a process.
_HOT_RELOAD_KEYS = ("role", "seeded_start")

_PROVIDER_TO_DEFAULT_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "venice": "VENICE_API_KEY",
}

_COLORS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[31m"]
_RESET = "\033[0m"


# ── Config ─────────────────────────────────────────────────────────


SHARED_TACIT_DEFAULT = "tacit_knowledge.md"


def _resolve_tacit_source(
    agent: dict, fleet_tacit: str | None,
) -> tuple[Path, bool]:
    """Resolve where an agent's tacit-knowledge file lives. Precedence:
    per-agent override > top-level fleet default > repo-root shared file.

    Returns (path, explicit) — `explicit=True` means the path was set by
    the user (per-agent or top-level), so a missing file is a config
    error; `explicit=False` means we fell through to the implicit
    `tacit_knowledge.md` default, so a missing file just means no notes
    yet."""
    explicit_rel = agent.get("tacit_knowledge") or fleet_tacit
    rel = explicit_rel or SHARED_TACIT_DEFAULT
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / path
    return path, bool(explicit_rel)


def _fetch_server_config(
    server_url: str, username: str, swarm_password: str, *, timeout: int = 20,
) -> dict | None:
    """GET /api/contributor/config with the contributor's credentials.

    Returns the stored `{agents, tacit, …}` plan, {} when the contributor has
    saved nothing yet (server 404), or None on any transport/HTTP error so the
    caller can fall back to the on-disk cache. Stdlib-only HTTP to keep the
    launcher dependency-free."""
    import urllib.error
    import urllib.request

    url = f"{server_url.rstrip('/')}/api/contributor/config"
    req = urllib.request.Request(url, headers={
        "X-Username": username,
        "X-Swarm-Password": swarm_password,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}  # authenticated, just nothing saved yet
        print(f"  [fleet] server config fetch failed (HTTP {e.code})", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"  [fleet] server config fetch failed ({e})", file=sys.stderr)
        return None
    cfg = body.get("config") or {}
    if body.get("tacit"):
        cfg["tacit"] = body["tacit"]
    return cfg


def _load_server_config(
    server_url: str, username: str, swarm_password: str,
) -> dict:
    """Server-hosted fleet plan for `config_source: server`, with a durable
    cache. On a successful fetch the plan is cached to `.fleet-cache.json`;
    when the server is unreachable the cache is used so a restart still
    launches. Returns {} only when both the server and the cache are empty."""
    fetched = _fetch_server_config(server_url, username, swarm_password)
    if fetched is not None:
        if fetched.get("agents"):
            try:
                _write_json_atomic(FLEET_CACHE_PATH, fetched)
            except OSError:
                pass  # cache is best-effort; the fetched plan still launches
        return fetched
    # Fetch failed — fall back to the last good plan.
    if FLEET_CACHE_PATH.exists():
        try:
            cached = _read_json(FLEET_CACHE_PATH)
            print("  [fleet] using cached fleet config (server unreachable)")
            return cached
        except (OSError, ValueError):
            pass
    return {}


def _load_fleet() -> tuple[str, str, str, list[dict], str | None]:
    if not FLEET_CONFIG_PATH.exists():
        sys.exit(
            f"fleet.config.json not found at {FLEET_CONFIG_PATH}.\n\n"
            f"Run the wizard to generate one (recommended):\n"
            f"    python scripts/init_fleet.py\n\n"
            f"Or hand-edit:\n"
            f"    cp fleet.config.example.json fleet.config.json"
        )
    data = _read_json(FLEET_CONFIG_PATH)

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

    # Resolve the fleet plan. A local `agents` array always wins (escape hatch
    # + full back-compat). Otherwise, when the config opts into server-hosted
    # mode (`"config_source": "server"`, written by `run.py --join`), fetch the
    # plan authored in the hosted contributor console — falling back to the
    # last-cached copy when the server is unreachable.
    agents = data.get("agents") or []
    if not agents and data.get("config_source") == "server":
        server_cfg = _load_server_config(server_url, username, swarm_password)
        agents = server_cfg.get("agents") or []
        # Surface the server's fleet-wide knobs + tacit as if they were local
        # top-level keys, so the merge logic below is source-agnostic.
        for key, value in server_cfg.items():
            if key not in ("agents",):
                data.setdefault(key, value)
    if not agents:
        if data.get("config_source") == "server":
            sys.exit(
                "No fleet configured yet. Open your swarm's join page and add "
                "agents under “My fleet”, then re-run `python run.py`."
            )
        sys.exit("fleet.config.json has no agents.")

    names: list[str] = []
    for entry in agents:
        name = entry.get("name")
        if not name:
            sys.exit("Every agent in fleet.config.json must have a 'name'.")
        names.append(name)
    if len(set(names)) != len(names):
        sys.exit("fleet.config.json has duplicate agent names.")

    fleet_tacit = data.get("tacit_knowledge") or None

    # Server-hosted tacit knowledge (from the contributor console) materializes
    # into the default shared file so agents see it on stagnation exactly like
    # locally-authored notes. Only in server mode, and only when the console
    # actually holds notes — never clobber a non-empty local file.
    server_tacit = data.get("tacit")
    if data.get("config_source") == "server" and server_tacit:
        tacit_path = ROOT / SHARED_TACIT_DEFAULT
        try:
            if not (tacit_path.exists() and tacit_path.read_text(
                    encoding="utf-8-sig").strip()):
                tacit_path.write_text(server_tacit, encoding="utf-8")
        except OSError:
            pass

    # Top-level `c3_api_key` is a fleet-wide default: every agent that doesn't
    # set its own inherits it. setdefault() (not overwrite) keeps per-agent keys
    # winning. Agents with neither fall through to C3_API_KEY / `c3 login`.
    fleet_c3_api_key = data.get("c3_api_key") or None
    if fleet_c3_api_key:
        for entry in agents:
            entry.setdefault("c3_api_key", fleet_c3_api_key)

    # Fleet-wide hyperparameter-search defaults: a top-level key is inherited by
    # every agent that doesn't set its own. setdefault keeps per-agent overrides.
    for key in _FLEET_WIDE_DEFAULT_KEYS:
        if key in data:
            for entry in agents:
                entry.setdefault(key, data[key])

    return server_url, username, swarm_password, agents, fleet_tacit


# ── Git worktree helpers ───────────────────────────────────────────


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _normalize_path(p: str | Path) -> str:
    """Canonical form for cross-platform path comparison.

    Git on Windows emits forward-slash paths (`C:/Users/.../worktrees/foo`)
    while `str(WORKTREES_DIR / name)` uses backslashes, so a literal `in` check
    fails on every valid worktree. Normalize via `os.path.normcase` (handles
    case-insensitive NTFS + slash flipping) and `os.path.normpath` (collapses
    `..`, double separators)."""
    return os.path.normcase(os.path.normpath(str(p)))


def _existing_worktree_paths() -> set[str]:
    out = _git(["worktree", "list", "--porcelain"])
    paths: set[str] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.add(_normalize_path(line[len("worktree "):]))
    return paths


def _branch_exists(branch: str) -> bool:
    return bool(_git(["branch", "--list", branch]))


def _git_in(path: Path, args: list[str]) -> str:
    """Run git inside a specific worktree (``_git`` is pinned to ROOT)."""
    result = subprocess.run(
        ["git", "-C", str(path)] + args, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git -C {path} {' '.join(args)} failed:\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _refresh_worktree(path: Path, name: str, log=print) -> None:
    """Reset an agent worktree's code to the repo's current HEAD commit.

    Worktrees persist across fleet restarts (identity + cargo cache live
    there), and their `fleet/<name>` branch stays wherever it was when the
    worktree was FIRST created — so without this reset, a stopped-and-
    restarted fleet keeps running months-old orchestration code and bug
    fixes silently never reach the agents (this is how the vrp-swarm
    benchmark-heartbeat fix sat inert for two weeks). `reset --hard` moves
    only TRACKED files: agent.config.json, .swarm-cache.json, and
    tacit_knowledge_personal.md are untracked and survive, and algorithm
    files are rewritten from server state at the top of every iteration.

    Best-effort: a failed reset (e.g. a stale index.lock) logs a warning and
    launches on the existing code — the pre-refresh status quo — rather than
    blocking the fleet.
    """
    try:
        head = _git(["rev-parse", "HEAD"])
        current = _git_in(path, ["rev-parse", "HEAD"])
        _git_in(path, ["reset", "--hard", head])
        if current != head:
            log(f"  [fleet] {name}: worktree code refreshed "
                f"{current[:7]} -> {head[:7]}")
    except (RuntimeError, OSError) as e:
        log(f"  [fleet] WARNING: could not refresh {name}'s worktree to "
            f"current HEAD ({e}); launching with its existing code")


def _ensure_worktree(name: str, log=print) -> Path:
    path = WORKTREES_DIR / name
    branch = f"fleet/{name}"
    known = _existing_worktree_paths()

    if path.exists() and _normalize_path(path) not in known:
        # Stale directory not tracked as a worktree — prune and rebuild.
        _git(["worktree", "prune"])
        shutil.rmtree(path)

    if not path.exists():
        WORKTREES_DIR.mkdir(exist_ok=True)
        if _branch_exists(branch):
            _git(["worktree", "add", str(path), branch])
        else:
            _git(["worktree", "add", "-b", branch, str(path)])

    # Both branches above need the refresh: a reused worktree AND a fresh
    # `worktree add <existing fleet/... branch>` both check out wherever that
    # branch last pointed, not current HEAD.
    _refresh_worktree(path, name, log=log)
    return path


def _seed_worktree(
    path: Path, agent: dict, fleet_tacit: str | None,
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
            cached = _read_json(root_cache)
            cached_url = (cached.get("server_url") or "").rstrip("/")
        except (json.JSONDecodeError, OSError):
            cached_url = ""
        if cached_url and cached_url == fleet_server_url.rstrip("/"):
            shutil.copy2(root_cache, wt_cache)

    wt_agent = path / "agent.config.json"
    existing: dict = {}
    if wt_agent.exists():
        try:
            parsed = _read_json(wt_agent)
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
    _write_json_atomic(wt_agent, merged)

    src, explicit = _resolve_tacit_source(agent, fleet_tacit)
    if src.exists():
        shutil.copy2(src, path / "tacit_knowledge_personal.md")
    elif explicit:
        # User explicitly named this file; missing = configuration error.
        sys.exit(f"Agent {agent['name']}: tacit_knowledge file not found: {src}")
    # else: implicit shared default doesn't exist yet — no notes this run.


# ── API keys ───────────────────────────────────────────────────────


def _resolve_api_key(agent: dict) -> tuple[str | None, str | None]:
    """Return (env_var_to_set, value) for this agent's subprocess.

    Returns (None, None) for claude-code, claude-code-agentic, and
    codex-agentic — all three use their respective CLI's local auth
    (OAuth / subscription / `codex login`).

    Key source order: a set environment variable, then the local
    `secrets.local.json` store, then (on an interactive terminal only) a
    one-time prompt that saves the pasted key to that store. Exits with an
    actionable message when none of those yields a key.
    """
    provider = agent.get("provider") or "anthropic"
    if provider in ("claude-code", "claude-code-agentic", "codex-agentic"):
        return None, None
    if provider not in _PROVIDER_TO_DEFAULT_ENV:
        sys.exit(f"Agent {agent['name']}: unknown provider {provider!r}")

    target = _PROVIDER_TO_DEFAULT_ENV[provider]
    source = agent.get("api_key_env") or target
    value = secrets_local.prompt_and_store(
        source, label=f"{source} for agent {agent['name']}",
    )
    if not value:
        sys.exit(
            f"Agent {agent['name']}: no API key for {source}.\n"
            f"  Fix any one of: export {source}=<your-key>; add it in the\n"
            f"  web setup (python run.py --ui → Keys); or re-run in a terminal\n"
            f"  to be prompted for it once (saved to secrets.local.json)."
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
            cached = _read_json(cache)
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
        encoding="utf-8", errors="replace",
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


def _stream_output(
    name: str,
    color: str,
    proc: subprocess.Popen,
    on_output=None,
) -> None:
    prefix = f"{color}[{name}]{_RESET} " if color else f"[{name}] "
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(prefix + line)
        sys.stdout.flush()
        # Mirror each line (without the ANSI prefix) to an optional consumer —
        # the control-ui companion feeds these to its live-log WebSocket.
        if on_output is not None:
            try:
                on_output(name, line.rstrip("\n"))
            except Exception:  # a UI consumer must never kill the fleet
                pass


# ── Subcommands ────────────────────────────────────────────────────


def cmd_list(agents: list[dict]) -> int:
    known = _existing_worktree_paths()
    print(f"  {'name':20s}  {'worktree':10s}  {'agent_id':40s}  path")
    for agent in agents:
        name = agent["name"]
        path = WORKTREES_DIR / name
        present = "ok" if _normalize_path(path) in known else "missing"
        agent_id = "<unregistered>"
        wt_agent = path / "agent.config.json"
        if wt_agent.exists():
            try:
                data = _read_json(wt_agent)
                agent_id = data.get("agent_id") or "<unregistered>"
            except json.JSONDecodeError:
                pass
        print(f"  {name:20s}  {present:10s}  {agent_id:40s}  {path}")
    return 0


def _remove_cargo_volumes(agent_name: str) -> None:
    """Best-effort cleanup of the per-agent cargo target volumes.

    `benchmark.py` creates `tig-cargo-cache-{cpu,gpu}-<safe_name>` lazily on
    first run. We don't know which (or whether either) was actually
    materialized, so we try both and silently swallow "no such volume"
    errors. A docker daemon that isn't running is also fine — the volumes
    will just stay there until the user starts Docker and removes them
    manually."""
    # Shared with the volume creator (benchmark.py) so the names always
    # match. Imported lazily so run/list paths don't pull benchmark.py's
    # import graph; benchmark.py is import-safe (its module-level
    # resolve_server_url call is required=False, so no I/O failure exits).
    from benchmark import _safe_volume_suffix
    safe = _safe_volume_suffix(agent_name)
    for suffix in ("cpu", "gpu"):
        vol = f"tig-cargo-cache-{suffix}-{safe}"
        result = subprocess.run(
            ["docker", "volume", "rm", vol],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            print(f"  removed docker volume {vol}")
        # Silent on failure: most calls hit the "no such volume" path
        # because most agents only used one of cpu/gpu (or none yet).


def _clean_one(name: str, docker_available: bool) -> None:
    """Remove a single agent's worktree, fleet branch, and cargo volumes."""
    path = WORKTREES_DIR / name
    branch = f"fleet/{name}"
    if path.exists():
        try:
            _git(["worktree", "remove", "--force", str(path)])
            print(f"  removed worktree {path}")
        except RuntimeError as e:
            # Not a registered worktree, or git refused — fall back to a plain
            # directory delete so a half-removed/orphaned dir doesn't linger.
            try:
                shutil.rmtree(path)
                print(f"  removed orphaned directory {path}")
            except OSError as oe:
                print(f"  could not remove {path}: {e}; rmtree also failed: {oe}",
                      file=sys.stderr)
    if _branch_exists(branch):
        try:
            _git(["branch", "-D", branch])
            print(f"  deleted branch {branch}")
        except RuntimeError as e:
            print(f"  could not delete branch {branch}: {e}", file=sys.stderr)
    if docker_available:
        _remove_cargo_volumes(name)


def _describe_exit(rc: int | None) -> str:
    """Human-readable process exit description.

    A negative returncode means the child was killed by signal -rc. SIGTERM/
    SIGINT here is the fleet's own clean teardown, not a crash — say so,
    because a bare "exited with code -15" reads like a fault to contributors.
    """
    if rc is not None and rc < 0:
        try:
            sig = signal.Signals(-rc).name
        except ValueError:
            sig = f"signal {-rc}"
        clean = " (clean shutdown)" if -rc in (signal.SIGTERM, signal.SIGINT) else ""
        return f"stopped via {sig}{clean}"
    return f"exited with code {rc}"


def _fleet_branch_names() -> set[str]:
    """All local `fleet/<name>` branches, returned as bare `<name>`s."""
    out = _git(["branch", "--list", "fleet/*", "--format=%(refname:short)"])
    return {line[len("fleet/"):] for line in out.splitlines()
            if line.startswith("fleet/")}


def cmd_clean(agents: list[dict]) -> int:
    docker_available = shutil.which("docker") is not None

    # Names from the current config…
    config_names = [a["name"] for a in agents]

    # …plus any worktree dir or fleet/ branch left behind by a *previous*
    # config (e.g. the agent was renamed). Matching only current config names
    # would strand those forever, so glob the filesystem and the branch list.
    orphan_names: set[str] = set()
    if WORKTREES_DIR.exists():
        orphan_names |= {p.name for p in WORKTREES_DIR.iterdir() if p.is_dir()}
    try:
        orphan_names |= _fleet_branch_names()
    except RuntimeError as e:
        print(f"  could not list fleet branches: {e}", file=sys.stderr)
    orphan_names -= set(config_names)

    for name in config_names:
        _clean_one(name, docker_available)
    for name in sorted(orphan_names):
        print(f"  [clean] orphaned agent (not in current config): {name}")
        _clean_one(name, docker_available)

    _git(["worktree", "prune"])
    return 0


def _sync_hot_reload_to_worktrees(
    agents: list[dict], entries: dict[str, dict] | None = None,
) -> None:
    """Patch hot-reloadable fields (role, seeded_start) into each running
    worktree's agent.config.json when they've changed.

    Desired values come from `entries` (name→entry) when provided — the
    server-hosted plan in `config_source: server` mode — otherwise from the
    local fleet.config.json, the classic behavior.

    Best-effort: a transient read/parse/write error on one agent is logged and
    skipped, never crashing the fleet monitor. Only fields in _HOT_RELOAD_KEYS
    are touched; everything else in agent.config.json (identity, provider,
    runtime defaults run_loop wrote) is preserved."""
    if entries is None:
        try:
            fleet = _read_json(FLEET_CONFIG_PATH)
        except Exception:
            return
        entries = {
            a.get("name"): a
            for a in (fleet.get("agents") or [])
            if a.get("name")
        }
    for agent in agents:
        name = agent.get("name")
        entry = entries.get(name)
        if not entry:
            continue
        wt_cfg_path = WORKTREES_DIR / name / "agent.config.json"
        try:
            current = _read_json(wt_cfg_path)
        except Exception:
            continue
        if not isinstance(current, dict):
            continue
        changed_keys = []
        for key in _HOT_RELOAD_KEYS:
            desired = entry.get(key)
            if desired is not None and current.get(key) != desired:
                current[key] = desired
                changed_keys.append(key)
        if changed_keys:
            try:
                # run_loop may have registered and persisted agent_id/name/
                # token between our read above and this write — re-read and
                # keep those so the hot-reload write can't clobber a freshly
                # issued agent_token (which would force a re-register).
                try:
                    latest = _read_json(wt_cfg_path)
                except Exception:
                    latest = {}
                if isinstance(latest, dict):
                    for ident_key in ("agent_id", "agent_name", "agent_token"):
                        if latest.get(ident_key) and not current.get(ident_key):
                            current[ident_key] = latest[ident_key]
                _write_json_atomic(wt_cfg_path, current)
                synced = ", ".join(
                    f"{k} -> {current.get(k)}" for k in changed_keys
                )
                print(f"  [fleet] {name}: {synced}")
            except OSError as e:
                print(f"  [fleet] {name}: hot-reload sync failed: {e}", file=sys.stderr)


# C3 control-plane defaults. The concurrency cap is read LIVE from C3, never
# from a hardcoded tier table — the cap C3 actually enforces is the truth (e.g.
# an account may carry a custom limit, or C3 may retune a tier). Override the
# host with C3_API_ENDPOINT (self-hosted / test control planes).
_C3_API_ENDPOINT = "https://api.cthree.cloud"
_C3_API_TIMEOUT_SECS = 20


def _c3_access_token() -> str | None:
    """Fall back to the `c3 login` OAuth token when no API key is configured
    (local dev). The runner and API-key fleets never need this."""
    try:
        creds = json.loads((Path.home() / ".c3" / "credentials.json").read_text())
    except (OSError, ValueError):
        return None
    tok = creds.get("access_token")
    return tok if isinstance(tok, str) and tok else None


def _c3_api_get(base: str, path: str, token: str) -> dict | None:
    """GET a C3 control-plane JSON endpoint with a bearer token. None on any
    transport/decode error — the caller degrades gracefully."""
    # A User-Agent is required — the control plane's edge 403s the default
    # `Python-urllib` agent.
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "tig-swarm-fleet/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_C3_API_TIMEOUT_SECS) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return data if isinstance(data, dict) else None


def _select_concurrency_limit(subscription: dict, tiers: dict) -> int | None:
    """The actual concurrent-job cap C3 enforces, from live API payloads. An
    account-specific `concurrency_limit` on the subscription wins; otherwise the
    current tier's `concurrency_limit` from the tiers catalog. None if neither is
    a positive int (so the caller can fall back)."""
    override = subscription.get("concurrency_limit")
    if isinstance(override, int) and override > 0:
        return override
    tier = (subscription.get("tier") or "").strip().lower()
    if not tier:
        return None
    for entry in (tiers or {}).get("tiers", []):
        if (entry.get("tier") or "").strip().lower() == tier:
            cap = entry.get("concurrency_limit")
            return cap if isinstance(cap, int) and cap > 0 else None
    return None


def _query_c3_plan_cap(c3_key: str | None) -> int | None:
    """Return the concurrent-job cap C3 *actually* enforces for this account, by
    reading it live from the control plane (`/v2/billing/subscription` for the
    current/overridden plan, `/v2/billing/tiers` for the plan's limit). None if
    it can't be determined (no auth, offline, unexpected payload) — the caller
    then falls back to the configured value."""
    token = c3_key or _c3_access_token()
    if not token:
        return None
    base = os.environ.get("C3_API_ENDPOINT") or _C3_API_ENDPOINT
    subscription = _c3_api_get(base, "/v2/billing/subscription", token)
    if not subscription:
        return None
    # Account override short-circuits before we even fetch the tier catalog.
    override = subscription.get("concurrency_limit")
    if isinstance(override, int) and override > 0:
        return override
    tiers = _c3_api_get(base, "/v2/billing/tiers", token)
    return _select_concurrency_limit(subscription, tiers or {})


def _resolve_fleet_c3_key(agents: list[dict]) -> str | None:
    """The single C3 key the fleet shares: env override, then any agent's
    resolved `c3_api_key` (top-level default already merged in by _load_fleet),
    then the local secrets store."""
    return (
        os.environ.get("C3_API_KEY")
        or next((a.get("c3_api_key") for a in agents if a.get("c3_api_key")), None)
        or secrets_local.resolve("C3_API_KEY")
    )


def cmd_run(
    agents: list[dict],
    only: list[str] | None,
    server_url: str,
    username: str,
    swarm_password: str,
    fleet_tacit: str | None = None,
    stop_event: "threading.Event | None" = None,
    on_output=None,
    on_status=None,
) -> int:
    """Launch and supervise the fleet until every agent exits or a stop is
    requested.

    Interactive/CLI use (the default) installs SIGINT/SIGTERM handlers and runs
    in the foreground. The control-ui companion instead runs this in a worker
    thread and passes `stop_event` (set it to request a graceful shutdown),
    `on_output(agent_name, line)` (live log lines), and `on_status(event, info)`
    (lifecycle events: 'spawned', 'running', 'exited', 'stopped'). Signal
    handlers are only installed when running on the main thread — installing
    them elsewhere raises ValueError."""
    if only:
        names = {a["name"] for a in agents}
        unknown = [n for n in only if n not in names]
        if unknown:
            sys.exit(f"Unknown agent name(s) in --only: {', '.join(unknown)}")
        agents = [a for a in agents if a["name"] in only]

    # Mirror fleet-level bootstrap milestones to BOTH the terminal (CLI users)
    # and the control-ui log stream (`on_output`). Without this, everything up
    # to the first agent subprocess print — key resolution, the C3 subscription
    # query, per-agent worktree creation/seeding — is terminal-only, so the web
    # companion shows "launched" with an empty log panel and looks hung during a
    # bootstrap that can take a while (git worktree + `setup.py sync` + a live
    # C3 API round-trip). Routing them through `on_output` gives the UI real
    # progress and makes any stall visible instead of silent.
    def _fleet_log(msg: str) -> None:
        print(msg)
        if on_output is not None:
            try:
                on_output("fleet", msg.strip())
            except Exception:  # a UI consumer must never kill the fleet
                pass

    _fleet_log("  [fleet] preparing to launch — resolving keys and swarm state…")

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

    # One coherent fleet-wide C3 slot-pool cap (c3_pool.py). All agents share ONE
    # C3 key, so the cap is the subscription's max concurrent-job limit — query
    # it live from C3 rather than trusting a hand-set number. We stamp it onto
    # every agent's c3_max_parallel_jobs before seeding worktrees.
    def _agent_cap(a: dict) -> int:
        try:
            return max(1, int(a.get("c3_max_parallel_jobs", 3)))
        except (TypeError, ValueError):
            return 3

    c3_agents = [a for a in agents if (a.get("compute") or "local") == "c3"]
    fleet_pool_size = None
    if c3_agents:
        _fleet_log("  [fleet] querying C3 subscription for the concurrency cap…")
        fleet_pool_size = _query_c3_plan_cap(_resolve_fleet_c3_key(agents))
    if fleet_pool_size:
        _fleet_log(f"  [fleet] C3 subscription cap: {fleet_pool_size} concurrent job(s) "
                   f"(fleet-wide pool size + shards per benchmark)")
        for a in agents:
            a["c3_max_parallel_jobs"] = fleet_pool_size
    else:
        # No C3 agents, or the query failed (offline / auth) — fall back to the
        # configured value so the fleet still launches with a sane, coherent cap.
        fleet_pool_size = max((_agent_cap(a) for a in agents), default=3)
        if c3_agents:
            _fleet_log(f"  [fleet] WARNING: could not read C3 subscription cap; "
                       f"falling back to configured c3_max_parallel_jobs={fleet_pool_size}")

    for i, agent in enumerate(agents):
        name = agent["name"]
        _fleet_log(f"  [fleet] preparing {name}… (worktree + swarm state; first "
                   f"run compiles, which can take a few minutes)")
        path = _ensure_worktree(name, log=_fleet_log)
        _seed_worktree(path, agent, fleet_tacit, server_url, username, swarm_password)

        env = os.environ.copy()
        target, value = key_envs[i]
        if target and value:
            env[target] = value
        # C3 cloud benchmarking reads C3_API_KEY from the environment (a raw
        # per-agent `c3_api_key` in config still wins downstream). Inject it
        # from the local secrets store when the launching shell didn't export
        # it, so `--join` contributors never have to `export C3_API_KEY`.
        if (agent.get("compute") or "local") == "c3" and not agent.get("c3_api_key"):
            c3_key = secrets_local.resolve("C3_API_KEY")
            if c3_key:
                env["C3_API_KEY"] = c3_key
        # Fleet-wide C3 slot pool (c3_pool.py): every agent in this fleet shares
        # ONE C3 key and thus one plan cap, so point them all at one pool dir
        # under the repo/clone root (shared by every worktree, isolated per
        # contributor on the hosted runner) and one agreed pool size. Agents
        # coordinate FCFS through it.
        env["C3_POOL_DIR"] = str(ROOT / ".c3-pool")
        env["C3_POOL_SIZE"] = str(fleet_pool_size)
        # C3 warm-image opt-in, delivered via env as well as agent.config.json:
        # worktrees live on persistent fleet/<name> branches whose scripts/ may
        # predate the config passthrough, but c3_compute._warm_c3_image has
        # honored these env vars from the start.
        # Tri-state: warm images default ON in c3_compute, so an explicit
        # `c3_warm_images: false` has to be forwarded too — otherwise the
        # opt-out is silently dropped and the agent runs warm anyway.
        if agent.get("c3_warm_images") is not None:
            env["TIG_C3_WARM_IMAGES"] = (
                "1" if agent["c3_warm_images"] else "0"
            )
        if agent.get("c3_warm_image"):
            env["TIG_C3_WARM_IMAGE"] = str(agent["c3_warm_image"])
        if agent.get("tig_dockerhub"):
            env["TIG_DOCKERHUB"] = str(agent["tig_dockerhub"])
        # Stdout is piped (not a TTY), so Python would block-buffer the child's
        # output and the fleet would look silent until buffers fill. Force
        # line-buffered I/O so [BENCH]/registration prints stream live.
        env["PYTHONUNBUFFERED"] = "1"

        color = _COLORS[i % len(_COLORS)] if use_color else ""
        cmd = [sys.executable, "scripts/run_loop.py"]
        # encoding/errors: child run_loop.py prints cargo + benchmark output
        # that occasionally contains non-UTF-8 bytes (Windows console code page
        # spillover, Rust panic backtraces with raw bytes). Without
        # errors="replace" the parent crashes the moment it tries to decode a
        # stray byte, killing the whole fleet.
        # group_kwargs(): each agent loop gets its own process group (POSIX)
        # so teardown can kill its docker/cargo/CLI grandchildren too, not
        # just the run_loop.py process itself.
        proc = subprocess.Popen(
            cmd, cwd=path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            encoding="utf-8", errors="replace",
            **group_kwargs(),
        )
        t = threading.Thread(
            target=_stream_output, args=(name, color, proc, on_output),
            daemon=True,
        )
        t.start()
        procs.append((name, proc, t))
        _fleet_log(f"  [fleet] spawned {name} (pid {proc.pid}) in {path}")
        if on_status is not None:
            on_status("spawned", {"name": name, "pid": proc.pid})

    _fleet_log(f"  [fleet] {len(procs)} agent(s) running. Ctrl-C to stop.")
    if on_status is not None:
        on_status("running", {"count": len(procs)})

    stopping = False

    def _terminate_all(reason: str) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"\n  [fleet] {reason} — terminating agents…")
        # Tree-wide TERM/KILL (see proc_utils): each agent's whole process
        # group dies, so in-flight docker/cargo grandchildren don't linger.
        for _, p, _t in procs:
            if p.poll() is None:
                term_tree(p)
        deadline = time.time() + 10
        for nm, p, _t in procs:
            remaining = max(0.0, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                print(f"  [fleet] killing {nm} (didn't exit in 10s)")
                kill_tree(p)

    def _shutdown(_signum, _frame):
        _terminate_all("shutdown signal")

    # Signal handlers can only be installed on the main thread. The companion
    # runs cmd_run in a worker thread and drives shutdown via stop_event instead.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

    # Is this a server-hosted fleet? In that mode the source of truth for
    # hot-reloadable fields is the contributor console, not the local file.
    try:
        server_sourced = (
            _read_json(FLEET_CONFIG_PATH).get("config_source") == "server"
        )
    except Exception:
        server_sourced = False

    # Re-sync hot-reloadable fields (role) into each running worktree's
    # agent.config.json on a cadence, so a contributor can change an agent's
    # role mid-run — by editing fleet.config.json locally, or in the hosted
    # console when server-sourced. run_loop.py re-reads agent.config.json every
    # iteration and picks the change up. The server is polled on a slower
    # cadence than the local file sync to keep request volume modest.
    _SYNC_EVERY_S = 5
    _SERVER_REFETCH_EVERY_S = 60
    server_entries: dict[str, dict] | None = None
    ticks = 0
    while any(p.poll() is None for _, p, _ in procs):
        if stop_event is not None and stop_event.is_set():
            _terminate_all("stop requested")
            break
        time.sleep(1)
        ticks += 1
        if server_sourced and ticks % _SERVER_REFETCH_EVERY_S == 0:
            fresh = _fetch_server_config(server_url, username, swarm_password)
            if fresh and fresh.get("agents"):
                server_entries = {
                    a.get("name"): a
                    for a in fresh["agents"] if a.get("name")
                }
        if ticks % _SYNC_EVERY_S == 0:
            _sync_hot_reload_to_worktrees(agents, server_entries)

    for name, p, t in procs:
        t.join(timeout=2)
        print(f"  [fleet] {name} {_describe_exit(p.returncode)}")
        if on_status is not None:
            on_status("exited", {"name": name, "returncode": p.returncode})

    _sync_tacit_back(agents, fleet_tacit)
    if on_status is not None:
        on_status("stopped", {})
    return 0


def _sync_tacit_back(agents: list[dict], fleet_tacit: str | None) -> None:
    """After the fleet stops, copy `- LLM:` bullets each agent appended to
    its worktree's tacit_knowledge_personal.md back to the source file the
    agent resolves to. Multiple agents that share one source (the default
    when no per-agent override is set) all funnel into the same file —
    their LLM lessons get collated and deduped against existing content.
    Lines already present (matched verbatim) are skipped, so the sync is
    idempotent across restarts."""
    # group worktree LLM lines by destination path
    by_source: dict[Path, list[str]] = {}
    for agent in agents:
        name = agent.get("name")
        if not name:
            continue
        wt_path = WORKTREES_DIR / name / "tacit_knowledge_personal.md"
        if not wt_path.exists():
            continue
        src_path, _ = _resolve_tacit_source(agent, fleet_tacit)
        try:
            worktree_text = wt_path.read_text()
        except OSError as e:
            print(f"  [fleet] tacit sync-back skipped for {name}: {e}")
            continue
        llm_lines = [
            ln for ln in worktree_text.splitlines()
            if ln.startswith("- LLM:")
        ]
        if not llm_lines:
            continue
        by_source.setdefault(src_path, []).extend(llm_lines)

    for src_path, candidate_lines in by_source.items():
        if not src_path.exists():
            continue
        try:
            src_text = src_path.read_text()
        except OSError as e:
            print(f"  [fleet] tacit sync-back skipped for {src_path}: {e}")
            continue
        existing = {
            ln for ln in src_text.splitlines() if ln.startswith("- LLM:")
        }
        seen: set[str] = set()
        new_lines: list[str] = []
        for ln in candidate_lines:
            if ln in existing or ln in seen:
                continue
            seen.add(ln)
            new_lines.append(ln)
        if not new_lines:
            continue
        suffix = "" if src_text.endswith("\n") else "\n"
        src_path.write_text(src_text + suffix + "\n".join(new_lines) + "\n")
        try:
            shown = src_path.relative_to(ROOT)
        except ValueError:
            shown = src_path
        print(f"  [fleet] synced {len(new_lines)} LLM lesson(s) → {shown}")


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

    server_url, username, swarm_password, agents, fleet_tacit = _load_fleet()
    if args.list:
        return cmd_list(agents)
    if args.clean:
        return cmd_clean(agents)
    return cmd_run(
        agents, args.only, server_url, username, swarm_password, fleet_tacit,
    )


if __name__ == "__main__":
    sys.exit(main())
