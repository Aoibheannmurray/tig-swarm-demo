#!/usr/bin/env python3
"""TIG Swarm host-admin CLI.

Contributors do not need this script — they edit fleet.config.json and run
`python scripts/run_fleet.py`. This file is for host operations only.

Subcommands:

  python setup.py create      Host: stand up a new swarm on Railway. Drives
                              the `railway` CLI to create a project + service
                              + volume, sets env vars, deploys the server,
                              then pushes swarm-wide config (challenge, tracks,
                              timeout, …) to the live URL. Scaffolds a
                              fleet.config.json so the host can immediately
                              participate.

  python setup.py switch <challenge>
                              Host: change the active challenge. Broadcasts
                              to all contributors (they auto-follow on their
                              next iteration via `setup.py sync`).

  python setup.py sync        Refresh .swarm-cache.json from the live server.
                              Idempotent; called by scripts/run_loop.py at the
                              top of every iteration.

  python setup.py tacit [<agent-name>]
                              Interactive helper to fill in an agent's tacit
                              knowledge file. The one piece of the old wizard
                              that survives (paste-a-block UX is awkward in
                              JSON).

Files this script reads / writes:
  - README.md, CHALLENGE.md (templated with the active challenge)
  - swarm.admin.json (host-only: admin_key, stagnation knobs)
  - .swarm-cache.json (machine-managed mirror of /api/swarm_config)
  - fleet.config.json (scaffolded by `create`; user-editable thereafter)
  - .railway/config.json (managed by the `railway` CLI; gitignored)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess as sp
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

# Files that carry swarm-specific values (URL, active challenge name) and
# get rewritten in-place by `setup.py create` / `setup.py sync`. benchmark.py
# and publish.py are intentionally excluded — they contain challenge-generic
# code (function names, data keys, docstrings for all five challenges) that
# must not be rewritten. They read the active challenge from .swarm-cache.json
# at runtime instead.
TEMPLATED_FILES = [
    ROOT / "README.md",
]

# Heuristic URL patterns treated as "the swarm URL" and rewritten when the
# server URL changes. Catches the canonical Railway domain and raw IP-form
# URLs from older self-host setups. Without this, a clone whose baked URL
# doesn't match the current `.swarm-cache.json` server_url (e.g. someone
# committed their templated state, or migrated between hosting styles) would
# silently fail to re-template.
_RAILWAY_URL_RE = re.compile(r"https?://[a-zA-Z0-9-]+\.up\.railway\.app")
_RAW_IP_URL_RE = re.compile(r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?")

# The literal placeholder strings the tracked files carry. NEVER replace
# arbitrary URLs — too easy to clobber rustup / GitHub / localhost dev URLs
# that happen to live in the same files.
PLACEHOLDER_URL = "${SERVER_URL}"
PLACEHOLDER_CHALLENGE = "${CHALLENGE_NAME}"
PLACEHOLDER_ALGO = "${ALGORITHM_PATH}"

# Per-challenge defaults for the wizard prompts. The canonical definitions
# live in server/challenges.py; this dict is built from there at module
# load. Keep it local to setup.py so existing call sites (post_config)
# don't need to be aware of the import.
sys.path.insert(0, str(ROOT / "server"))
from challenges import CHALLENGES as _CHALLENGE_REGISTRY  # noqa: E402

CHALLENGES: dict[str, dict] = {
    name: {
        "scoring_direction": d.scoring_direction,
        "track_keys": list(d.track_keys),
        "strategy_tags": list(d.strategy_tags),
        "is_gpu": d.is_gpu,
    }
    for name, d in _CHALLENGE_REGISTRY.items()
}

CPU_CHALLENGES = {k: v for k, v in CHALLENGES.items() if not v["is_gpu"]}
GPU_CHALLENGES = {k: v for k, v in CHALLENGES.items() if v["is_gpu"]}

DEFAULT_INSTANCES_PER_TRACK = 2
DEFAULT_TRACKS_PER_CHALLENGE = {
    "satisfiability": {"n_vars=100000,ratio=4150": 2},
    "vehicle_routing": {"n_nodes=600": 2},
    "knapsack": {"n_items=1000,budget=10": 2},
    "job_scheduling": {"n=20,s=FLOW_SHOP": 2},
    "energy_arbitrage": {"s=BASELINE": 2},
    "hypergraph": {"n_h_edges=10000": 2},
    "neuralnet_optimizer": {"n_hidden=4": 2},
    "vector_search": {"n_queries=10": 2},
}

AGENT_CONFIG_PATH = ROOT / "agent.config.json"


# ── Helpers ──────────────────────────────────────────────────────────


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        ans = input(f"{label}{suffix}: ").strip()
        if ans:
            return ans
        if default is not None:
            return default


def prompt_choice(label: str, choices: list[str], default: str) -> str:
    print(label)
    for i, c in enumerate(choices, 1):
        marker = " (default)" if c == default else ""
        print(f"  {i}. {c}{marker}")
    while True:
        ans = input(f"Pick 1-{len(choices)} [{default}]: ").strip()
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            return choices[int(ans) - 1]
        if ans in choices:
            return ans
        print("  invalid choice; try again")


def prompt_int(label: str, default: int, minimum: int = 0) -> int:
    while True:
        ans = input(f"{label} [{default}]: ").strip()
        if not ans:
            return default
        try:
            v = int(ans)
        except ValueError:
            print("  expected integer")
            continue
        if v < minimum:
            print(f"  must be >= {minimum}")
            continue
        return v


def _swap(text: str, placeholder: str, prior: str | None, new: str, is_url: bool = False) -> str:
    """Replace the placeholder and the previously-templated value with `new`.

    When `is_url` is True, also sweep Railway / raw-IP URLs — this catches
    stale baked URLs that don't match `prior`. The regex pass is skipped
    for non-URL substitutions (challenge name, algorithm path) so it can't
    clobber just-substituted server URLs."""
    text = text.replace(placeholder, new)
    if prior and prior != placeholder and prior != new:
        text = text.replace(prior, new)
    if is_url:
        text = _RAILWAY_URL_RE.sub(new, text)
        text = _RAW_IP_URL_RE.sub(new, text)
    return text


def template_files(
    server_url: str,
    challenge: str | None = None,
    algorithm_path: str | None = None,
    prior: dict | None = None,
) -> None:
    """Substitute swarm-specific placeholders into every tracked file that
    contains them, using prior values from .swarm-cache.json to undo
    previously-templated state."""
    prior = prior or {}
    prior_url = prior.get("server_url")
    prior_challenge = prior.get("challenge")
    prior_algo = prior.get("algorithm_path")

    for path in TEMPLATED_FILES:
        if not path.exists():
            print(f"  skipping {path} (missing)")
            continue
        text = path.read_text()
        new = _swap(text, PLACEHOLDER_URL, prior_url, server_url, is_url=True)
        if challenge:
            new = _swap(new, PLACEHOLDER_CHALLENGE, prior_challenge, challenge)
        if algorithm_path:
            new = _swap(new, PLACEHOLDER_ALGO, prior_algo, algorithm_path)
        if new != text:
            path.write_text(new)
            print(f"  templated {path.relative_to(ROOT)}")


# Three role-scoped files replace the legacy swarm.config.json:
#   swarm.admin.json  — host-only secrets and tuning (admin_key, stagnation knobs)
#   .swarm-cache.json — machine-managed mirror of /api/swarm_config
#   fleet.config.json — user-edited list of agents to spawn
_ADMIN_FIELDS = (
    "admin_key", "swarm_password", "owner_name", "swarm_name", "challenges",
    "stagnation_threshold", "stagnation_limit",
    "hypothesis_recall_threshold",
)
_CACHE_FIELDS = (
    "server_url", "active_challenge", "challenge", "swarm_type",
    "tracks", "timeout", "scoring_direction",
    "algorithm_path", "kernel_path", "is_gpu",
    # write_swarm_cache stamps synced_at itself, so the field is set even
    # when cfg doesn't carry one in.
    "synced_at",
)


def write_swarm_admin(cfg: dict) -> None:
    """Slice the host-only fields out of `cfg` and write them to
    swarm.admin.json. Skips silently when no admin fields are present."""
    payload = {k: cfg[k] for k in _ADMIN_FIELDS if k in cfg}
    if not payload:
        return
    (ROOT / "swarm.admin.json").write_text(json.dumps(payload, indent=2) + "\n")


def write_swarm_cache(cfg: dict) -> None:
    """Slice the server-derived fields out of `cfg` and write them to
    .swarm-cache.json. Stamps `synced_at` so benchmark.py can show freshness."""
    payload = {k: cfg[k] for k in _CACHE_FIELDS if k in cfg}
    if not payload:
        return
    payload["synced_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / ".swarm-cache.json").write_text(json.dumps(payload, indent=2) + "\n")


def read_swarm_cache() -> dict:
    path = ROOT / ".swarm-cache.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def read_swarm_admin() -> dict:
    path = ROOT / "swarm.admin.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def resolve_server_url() -> str | None:
    """Find server_url in the new layout. Tries agent.config.json (worktree)
    first, then fleet.config.json (root), then .swarm-cache.json as a
    last-resort fallback. Returns None when nothing is configured yet.

    The user-edited configs win over the cache because the cache is a payload
    mirror of /api/swarm_config from a *specific* server — if the user points
    the swarm at a new URL, a leftover cache from the old swarm must not keep
    redirecting sync back to the dead server.
    """
    if AGENT_CONFIG_PATH.exists():
        try:
            agent = json.loads(AGENT_CONFIG_PATH.read_text())
            if isinstance(agent, dict) and agent.get("server_url"):
                return agent["server_url"]
        except json.JSONDecodeError:
            pass
    fleet_path = ROOT / "fleet.config.json"
    if fleet_path.exists():
        try:
            fleet = json.loads(fleet_path.read_text())
            if isinstance(fleet, dict) and fleet.get("server_url"):
                return fleet["server_url"]
        except json.JSONDecodeError:
            pass
    cache = read_swarm_cache()
    if cache.get("server_url"):
        return cache["server_url"]
    return None


def _arg_value(args: argparse.Namespace | None, name: str):
    return getattr(args, name, None) if args is not None else None


def _arg_enabled(args: argparse.Namespace | None, name: str) -> bool:
    return bool(getattr(args, name, False)) if args is not None else False


# Challenges whose mainnet algorithm format (single mod.rs + optional
# kernels.cu) matches what the swarm's inactive_algorithms pool expects.
# Server enforces the same set; host-side it gates the wizard prompt so
# we don't ask about challenges we can't actually seed.
SEED_INACTIVE_SUPPORTED: set[str] = {"knapsack", "satisfiability"}

_MAINNET_API = "https://mainnet-api.tig.foundation"


def _mainnet_get(url: str, *, timeout: int = 8) -> object:
    """GET + JSON-decode a mainnet API endpoint.

    Bare `urllib.request.urlopen` ships `Python-urllib/3.X` which the CDN
    in front of mainnet-api.tig.foundation rejects with HTTP 403, so we
    set an explicit User-Agent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tig-swarm-demo-setup",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _top_mainnet_algorithm(challenge: str) -> tuple[str, int] | None:
    """Return `(algorithm_name, adoption_fp)` for the highest-adoption
    successfully-compiled mainnet algorithm on `challenge`, or None if
    none qualifies / the API is unreachable.

    `adoption_fp` is the raw 1e16-scaled fixed-point integer the API
    returns; divide by 1e16 for a percentage."""
    try:
        block = _mainnet_get(f"{_MAINNET_API}/get-block")["block"]
        block_id = block["id"]
        challenges_resp = _mainnet_get(
            f"{_MAINNET_API}/get-challenges?block_id={block_id}"
        )
        algos_resp = _mainnet_get(
            f"{_MAINNET_API}/get-algorithms?block_id={block_id}"
        )
    except Exception as e:
        print(f"  mainnet unreachable ({e})")
        return None

    # challenge_id -> challenge_name. The upstream response carries the
    # human-readable name under `config.name`.
    id_to_name: dict[str, str] = {
        c["id"]: c["config"]["name"] for c in challenges_resp["challenges"]
    }
    target_cid = next((cid for cid, name in id_to_name.items() if name == challenge), None)
    if target_cid is None:
        return None

    # Only consider algorithms that compiled successfully upstream.
    compile_ok: dict[str, bool] = {
        b["algorithm_id"]: bool(b.get("details", {}).get("compile_success"))
        for b in algos_resp.get("binarys", [])
    }

    best: tuple[str, int] | None = None
    for algo in algos_resp["codes"]:
        if (algo.get("details") or {}).get("challenge_id") != target_cid:
            continue
        if not compile_ok.get(algo["id"]):
            continue
        try:
            adoption = int((algo.get("block_data") or {}).get("adoption") or 0)
        except (TypeError, ValueError):
            adoption = 0
        if adoption <= 0:
            continue
        name = (algo.get("details") or {}).get("name")
        if not name:
            continue
        if best is None or adoption > best[1]:
            best = (name, adoption)
    return best


def seed_inactive_pool_from_mainnet(
    server_url: str, admin_key: str, challenges: set[str],
) -> None:
    """For each requested challenge in `SEED_INACTIVE_SUPPORTED`, find the
    current top-adoption mainnet algorithm, fetch its source in-memory via
    ``download_algorithm.fetch_algorithm`` (deliberately NOT
    ``download_algorithm`` — we never want to mutate the host's
    ``initial_algorithms/`` directory as a side effect of seeding the
    server's inactive pool), and POST it to ``/api/admin/seed_inactive``
    so the swarm's first stagnation-with-adoption event picks it up.

    The inactive pool's wire format carries a single algorithm_code blob
    (+ optional kernel), so we require the upstream bundle to contain
    exactly one ``mod.rs`` and at most one ``*.cu`` file. Anything else
    (README.md, multi-module .rs files) is grounds to skip — the schema
    can't represent it. Non-code companions (e.g. README.md) on an
    otherwise-single-file algorithm are silently ignored rather than
    blocking the seed.

    Best-effort throughout: network failures, unknown algorithms, and
    server errors are warned-and-skipped rather than aborting setup."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from download_algorithm import fetch_algorithm, DownloadError
    except Exception as e:
        print(f"  could not import download_algorithm.py: {e}; skipping seed.")
        return

    targets = sorted(challenges & SEED_INACTIVE_SUPPORTED)
    if not targets:
        return

    for ch in targets:
        top = _top_mainnet_algorithm(ch)
        if top is None:
            print(f"  {ch}: no compiled mainnet algorithm found; skipping seed.")
            continue
        algo_name, adoption = top
        print(
            f"  {ch}: top algorithm '{algo_name}' "
            f"(adoption {adoption / 1e16:.2f}%); fetching…"
        )
        try:
            files = fetch_algorithm(ch, algo_name)
        except DownloadError as e:
            print(f"  {ch}: fetch of {algo_name} failed ({e}); skipping seed.")
            continue

        rs_files = sorted(p for p in files if p.endswith(".rs"))
        cu_files = sorted(p for p in files if p.endswith(".cu"))
        if rs_files != ["mod.rs"] or len(cu_files) > 1:
            print(
                f"  {ch}: upstream {algo_name} is multi-module "
                f"(.rs={rs_files}, .cu={cu_files}) — inactive-pool seeding "
                f"requires a single mod.rs + at most one .cu; skipping."
            )
            continue
        algorithm_code = files["mod.rs"]
        kernel_code = files[cu_files[0]] if cu_files else None

        payload = {
            "admin_key": admin_key,
            "challenge": ch,
            "algorithm_code": algorithm_code,
            "kernel_code": kernel_code,
            "source_label": "tig-foundation",
        }
        req = urllib.request.Request(
            f"{server_url.rstrip('/')}/api/admin/seed_inactive",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.load(resp)
            print(
                f"  {ch}: seeded inactive pool "
                f"(inactive_id={body.get('inactive_id')}, source={body.get('source')})"
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            print(f"  {ch}: server rejected seed (HTTP {e.code}: {detail}); skipping.")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  {ch}: could not reach {server_url} ({e}); skipping seed.")


def push_config_to_server(server_url: str, admin_key: str, cfg: dict) -> None:
    """POST multi-challenge swarm config to a running server. Best-effort:
    if the server isn't running yet, skip gracefully and tell the user how
    to do it later.

    `cfg["challenges"]` is a dict of {challenge: {tracks, timeout,
    scoring_direction, initial_algorithm_code}}; `cfg["active_challenge"]`
    selects which one contributors auto-follow.
    """
    payload = {
        "admin_key": admin_key,
        "active_challenge": cfg["active_challenge"],
        "challenges": cfg["challenges"],
        "swarm_name": cfg.get("swarm_name", ""),
        "owner_name": cfg.get("owner_name", ""),
        "swarm_type": cfg.get("swarm_type", "cpu"),
        "stagnation_threshold": cfg.get("stagnation_threshold", 2),
        "stagnation_limit": cfg.get("stagnation_limit", 10),
        "hypothesis_recall_threshold": cfg.get("hypothesis_recall_threshold", 3),
    }
    req = urllib.request.Request(
        f"{server_url.rstrip('/')}/api/swarm_config",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            json.load(resp)
        print(f"  POSTed config to {server_url}/api/swarm_config")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(
            f"  could not reach {server_url} ({e}). Start the server and re-run "
            f"`python setup.py create`, or POST /api/swarm_config yourself once it's up."
        )


def read_initial_algorithms() -> dict[str, dict[str, str]]:
    """Read per-challenge initial algorithm files. Missing files map to
    empty strings — agents start from a stub. Returns
    {challenge: {"algorithm_code": ..., "kernel_code": ...}}."""
    out: dict[str, dict[str, str]] = {}
    for ch in CHALLENGES:
        algo_path = ROOT / "initial_algorithms" / f"{ch}.rs"
        kernel_path = ROOT / "initial_algorithms" / f"{ch}.cu"
        out[ch] = {
            "algorithm_code": algo_path.read_text() if algo_path.is_file() else "",
            "kernel_code": kernel_path.read_text() if kernel_path.is_file() else "",
        }
    return out


def fetch_challenge_sub_config(server_url: str, challenge: str) -> dict | None:
    """Pull a challenge's tracks/timeout/scoring_direction from the live
    server. Used by switch / sync to mirror the active challenge's sub-config
    into .swarm-cache.json so benchmark.py's offline fallback keeps working."""
    try:
        with urllib.request.urlopen(
            f"{server_url.rstrip('/')}/api/swarm_config", timeout=4,
        ) as r:
            data = json.load(r)
    except Exception:
        return None
    available = (data.get("available_challenges") or {})
    return available.get(challenge)


def collect_per_challenge_configs(
    initial_algorithms: dict[str, dict[str, str]],
    *,
    use_defaults: bool,
    challenge_set: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Build the `challenges` payload for POST /api/swarm_config, either by
    accepting defaults across all challenges (use_defaults=True, no prompts)
    or by asking the host for tracks/timeout per challenge.

    `challenge_set` restricts which challenges are configured (defaults to
    all). Used to only configure CPU or GPU challenges based on swarm type.
    """
    challenges: dict[str, dict] = {}
    target = challenge_set if challenge_set is not None else CHALLENGES
    for ch, meta in target.items():
        ch_def = _CHALLENGE_REGISTRY[ch]
        tracks: dict = {"seed": "test"}
        if use_defaults:
            default_tracks = DEFAULT_TRACKS_PER_CHALLENGE.get(ch)
            if default_tracks:
                for key, count in default_tracks.items():
                    tracks[key] = count
            else:
                for key in meta["track_keys"]:
                    tracks[key] = DEFAULT_INSTANCES_PER_TRACK
            timeout = ch_def.default_timeout
        else:
            print(f"\n── {ch} ──")
            ch_track_defaults = DEFAULT_TRACKS_PER_CHALLENGE.get(ch, {})
            for key in meta["track_keys"]:
                tracks[key] = prompt_int(
                    f"  instances for {key}",
                    ch_track_defaults.get(key, 0),
                    minimum=0,
                )
            timeout = prompt_int(
                f"  per-instance timeout for {ch} (seconds)",
                ch_def.default_timeout, minimum=1,
            )
        algo_data = initial_algorithms.get(ch, {})
        sub: dict = {
            "tracks": tracks,
            "timeout": timeout,
            "scoring_direction": meta["scoring_direction"],
            "initial_algorithm_code": algo_data.get("algorithm_code", ""),
            "strategy_tags": meta.get("strategy_tags", []),
        }
        if algo_data.get("kernel_code"):
            sub["initial_kernel_code"] = algo_data["kernel_code"]
        challenges[ch] = sub
    return challenges


def write_challenge_md(challenge: str) -> None:
    src = ROOT / "src" / challenge / "README.md"
    dst = ROOT / "CHALLENGE.md"
    if not src.exists():
        print(f"  warning: no README at {src.relative_to(ROOT)}; skipping CHALLENGE.md")
        return
    dst.write_text(src.read_text())
    print(f"  wrote {dst.relative_to(ROOT)} (from {src.relative_to(ROOT)})")


def tacit_header(stagnation_threshold: int = 2) -> str:
    """Standard header text written into the personal tacit-knowledge file.
    Parameterised on stagnation_threshold so the >= condition matches the
    swarm's actual config (the server reads it from swarm.admin.json on the
    host, POSTed at `setup.py create` time)."""
    return (
        "# Personal tacit knowledge\n\n"
        "Hints only **your local agent** sees. Never sent to the server.\n"
        "Other agents in the swarm cannot read this file.\n\n"
        f"Read by your agent when stagnating (`my_runs_since_improvement >= {stagnation_threshold}`)\n"
        "— at that point the server randomly picks (50/50) between this file\n"
        "and the swarm's `inspiration_code` for the iteration's hint.\n\n"
        "Your local agent occasionally appends its own distilled,\n"
        "challenge-agnostic \"when stuck, try X\" lessons here — specifically\n"
        "when it has stagnated 10 iterations in a row, or every 50 total\n"
        "iterations. These are general algorithmic know-how derived from\n"
        "looking back at every iteration so far, intentionally written so they\n"
        "stay useful even if the swarm switches to a different challenge later.\n\n"
        "## Strategies\n\n"
    )


def gather_tacit_knowledge(tk_path: Path, stagnation_threshold: int = 2) -> None:
    """Populate the personal tacit-knowledge file, all at once.

    Three input modes (no per-hint loop):
      1. Upload from an existing file (give a path; whole file is copied in).
      2. Paste/type every strategy at once in the terminal, ended by Ctrl-D
         (Unix) or Ctrl-Z+Enter (Windows). The whole block is dropped into
         the file verbatim — bullet style is up to the user.
      3. Skip — the stub stays in place.
    """
    print(
        "\n── Tacit knowledge (optional) ──\n"
        "Give your local agent private strategy hints. These are read\n"
        f"when the agent stagnates ({stagnation_threshold}+ iterations without improvement) —\n"
        "at which point the server picks 50/50 between consulting this file and\n"
        "the swarm's `inspiration_code` for the iteration's hint. The file is\n"
        "gitignored and never sent to the server.\n"
    )
    print("How would you like to provide them?")
    print("  1. Upload from an existing tacit-knowledge file (give a path)")
    print("  2. Type or paste every strategy now, all at once")
    print("  3. Skip — leave the existing file in place\n")

    while True:
        choice = input("Choice 1/2/3 [3]: ").strip() or "3"
        if choice in ("1", "2", "3"):
            break
        print("  invalid choice; pick 1, 2, or 3")

    if choice == "3":
        print(f"  no hints added (edit {tk_path.relative_to(ROOT)} any time)")
        return

    if choice == "1":
        src = input("Path to your tacit-knowledge file: ").strip()
        if not src:
            print("  no path given; leaving existing file in place")
            return
        src_path = Path(src).expanduser()
        if not src_path.is_file():
            print(f"  not a file: {src_path}; leaving existing file in place")
            return
        tk_path.write_text(src_path.read_text())
        print(f"  copied {src_path} -> {tk_path.relative_to(ROOT)}")
        return

    # choice == "2": single multi-line paste
    print(
        "\nPaste or type ALL of your strategies below — one per line, any\n"
        "format you like. When finished, press Ctrl-D (Unix/macOS) or\n"
        "Ctrl-Z then Enter (Windows) to submit.\n"
    )
    try:
        text = sys.stdin.read()
    except KeyboardInterrupt:
        print("\n  cancelled — leaving existing file in place")
        return
    text = text.strip()
    if not text:
        print("  no text entered; leaving existing file in place")
        return
    tk_path.write_text(tacit_header(stagnation_threshold) + text + "\n")
    print(f"  wrote your strategies to {tk_path.relative_to(ROOT)}")


def run_tacit(agent_name: str | None = None) -> int:
    """Interactive tacit-knowledge helper. The one piece of the old wizard
    that survives: paste-a-block UX is awkward to express in JSON, so the
    file pointed to by the fleet entry's `tacit_knowledge` field is what
    the user edits via this command.

    With no argument, picks the first agent in fleet.config.json (or the
    only one if there's exactly one). With an argument, picks that named
    agent. Creates the tacit file if missing and hooks it back into
    fleet.config.json's `tacit_knowledge` field if currently unset."""
    fleet_path = ROOT / "fleet.config.json"
    if not fleet_path.exists():
        print(
            "fleet.config.json not found — run `python setup.py create` (host) "
            "or copy fleet.config.example.json (contributor) first."
        )
        return 1
    try:
        fleet = json.loads(fleet_path.read_text())
    except json.JSONDecodeError as e:
        print(f"fleet.config.json is malformed: {e}")
        return 1
    agents = fleet.get("agents") or []
    if not agents:
        print("fleet.config.json has no agents.")
        return 1

    if agent_name:
        match = next((a for a in agents if a.get("name") == agent_name), None)
        if not match:
            print(f"agent {agent_name!r} not found in fleet.config.json.")
            print(f"available: {', '.join(a.get('name', '?') for a in agents)}")
            return 1
    elif len(agents) == 1:
        match = agents[0]
    else:
        names = [a.get("name", "?") for a in agents]
        print(
            "fleet has multiple agents; pass one of: "
            f"{', '.join(names)}"
        )
        return 1

    tk_rel = match.get("tacit_knowledge") or f"tacit_knowledge_{match['name']}.md"
    tk_path = Path(tk_rel)
    if not tk_path.is_absolute():
        tk_path = ROOT / tk_path

    # The stagnation threshold lives in swarm.admin.json on the host and is
    # not visible to a plain contributor — use the documented default.
    stagnation_threshold = read_swarm_admin().get("stagnation_threshold", 2)
    if not tk_path.exists():
        tk_path.parent.mkdir(parents=True, exist_ok=True)
        tk_path.write_text(
            tacit_header(stagnation_threshold)
            + "- (replace this with your own hint, or run setup again)\n"
        )
        print(f"  created {tk_path.relative_to(ROOT)} (gitignored)")

    gather_tacit_knowledge(tk_path, stagnation_threshold)

    # Hook the file path back into the fleet entry if it wasn't already set,
    # so run_fleet.py's _seed_worktree will copy it into the worktree.
    if not match.get("tacit_knowledge"):
        try:
            rel = str(tk_path.relative_to(ROOT))
        except ValueError:
            rel = str(tk_path)
        match["tacit_knowledge"] = rel
        fleet_path.write_text(json.dumps(fleet, indent=2) + "\n")
        print(f"  linked tacit_knowledge → {rel} in fleet.config.json")
    return 0


# ── Railway CLI helpers ──────────────────────────────────────────────


_RAILWAY_INSTALL_HINT = (
    "Install one of these, then re-run:\n"
    "    bash <(curl -fsSL cli.new)        # vendor installer (any OS with bash)\n"
    "    npm i -g @railway/cli             # if you have node\n"
    "    brew install railway              # macOS\n"
    "    cargo install railwayapp --locked # rust\n"
)


def _railway_run(*args: str, check: bool = True) -> sp.CompletedProcess:
    """Run `railway <args>` and capture output. Exit on non-zero unless check=False."""
    try:
        result = sp.run(
            ["railway", *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
    except FileNotFoundError:
        print("Railway CLI not found in PATH.\n" + _RAILWAY_INSTALL_HINT)
        sys.exit(2)
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        print(f"  error: railway {' '.join(args)} failed: {msg}")
        sys.exit(2)
    return result


def _railway_check_installed() -> None:
    if shutil.which("railway") is None:
        print("Railway CLI not found in PATH.\n" + _RAILWAY_INSTALL_HINT)
        sys.exit(2)


def _railway_check_auth() -> dict:
    """Return whoami JSON, or exit telling the user to `railway login`."""
    result = _railway_run("whoami", "--json", check=False)
    if result.returncode != 0:
        print(
            "Not logged in to Railway. Run this in another terminal, complete the\n"
            "browser flow, then re-run `python setup.py create`:\n"
            "    railway login\n"
        )
        sys.exit(2)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _pick_workspace(whoami: dict) -> str | None:
    """Return a workspace name (or None for single/no workspace).

    `railway init --json` requires `--workspace` when the user has more
    than one. Surface a prompt so the wizard can route the new project to
    the right workspace."""
    workspaces = whoami.get("workspaces") or []
    if len(workspaces) <= 1:
        return None
    names = [w.get("name", "") for w in workspaces if w.get("name")]
    if not names:
        return None
    print("\nMultiple Railway workspaces found. Pick one for this swarm:")
    return prompt_choice("  workspace", names, default=names[0])


def _railway_init_project(name: str, workspace: str | None = None) -> dict:
    args = ["init", "-n", name, "--json"]
    if workspace:
        args += ["--workspace", workspace]
    result = _railway_run(*args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"name": name}


def _railway_add_service(name: str) -> dict:
    result = _railway_run("add", "--service", name, "--json")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"name": name}


def _railway_set_variables(service: str, vars: dict[str, str]) -> None:
    args = ["variable", "set", "--service", service, "--skip-deploys"]
    for k, v in vars.items():
        args.append(f"{k}={v}")
    _railway_run(*args)


def _railway_add_volume(service: str, mount_path: str) -> None:
    """Create a persistent volume mounted at `mount_path`.

    The volume attaches to the linked service in `.railway/config.json`
    (set by the preceding `railway add --service`). `volume add` doesn't
    accept `--service`; we rely on the link being correct.

    `volume add` is the one non-idempotent step — it bails if a volume is
    already mounted on the linked service. Treat that as success."""
    result = _railway_run("volume", "add", "--mount-path", mount_path, check=False)
    if result.returncode == 0:
        return
    err = (result.stderr or "").lower()
    if "already" in err and "mount" in err:
        print(f"    volume already mounted at {mount_path}; skipping")
        return
    msg = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    print(f"  error: railway volume add failed: {msg}")
    sys.exit(2)


def _railway_up(service: str) -> None:
    """Deploy. --ci streams build logs and blocks until SUCCESS / FAILED."""
    # Inherit stdout/stderr so the user sees build logs as they stream.
    result = sp.run(
        ["railway", "up", "--service", service, "--ci"],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print("  error: railway up failed. Check the build logs above.")
        sys.exit(2)


def _railway_domain(service: str) -> str:
    """Get (or generate) the public URL for `service`. Idempotent."""
    result = _railway_run("domain", "--service", service, "--json")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  error: couldn't parse `railway domain` output: {result.stdout!r}")
        sys.exit(2)
    if isinstance(data, dict):
        if isinstance(data.get("domain"), str):
            return _ensure_https(data["domain"])
        domains = data.get("domains")
        if isinstance(domains, list) and domains:
            first = domains[0]
            if isinstance(first, str):
                return _ensure_https(first)
            if isinstance(first, dict) and isinstance(first.get("domain"), str):
                return _ensure_https(first["domain"])
    print(f"  error: railway domain returned no usable URL: {data!r}")
    sys.exit(2)


def _ensure_https(domain: str) -> str:
    if domain.startswith(("http://", "https://")):
        return domain.rstrip("/")
    return f"https://{domain}".rstrip("/")


def _wait_for_server(url: str, timeout: int = 60) -> bool:
    """Poll <url>/api/swarm_config until it responds or timeout passes.

    `railway up --ci` returns when the container starts; the FastAPI app
    needs a few extra seconds to bind. Without this poll, the immediate
    POST /api/swarm_config below races the app startup."""
    deadline = time.time() + timeout
    probe = f"{url.rstrip('/')}/api/swarm_config"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=4) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


# ── Modes ────────────────────────────────────────────────────────────


def run_create(args: argparse.Namespace | None = None) -> int:
    """Owner setup: configure a new swarm and deploy it on Railway.

    End-to-end: verify `railway` CLI + auth → wizard prompts → reset any
    prior `.railway/` link in this clone → `railway init` → `railway add
    --service` → `railway variable set` (DATA_DIR, ADMIN_KEY) → `railway
    volume add --mount-path /data` → `railway up --ci` (blocks until the
    deploy is live) → `railway domain --json` → POST swarm-wide config.

    Re-running on a clone that already created a swarm is fine: this
    deletes `.railway/` and creates a fresh project on Railway. The
    previous swarm is unaffected — it lives independently in your Railway
    workspace; manage it via the Railway dashboard."""
    print("TIG Swarm — create a new swarm on Railway")
    print("=" * 48)

    _railway_check_installed()
    user = _railway_check_auth()
    who = user.get("email") or user.get("name") or "unknown"
    print(f"  authed as Railway user: {who}\n")
    workspace = _arg_value(args, "workspace") or _pick_workspace(user)

    yes = _arg_enabled(args, "yes")
    swarm_type = _arg_value(args, "swarm_type")
    if not swarm_type:
        swarm_type = "cpu" if yes else prompt_choice(
            "\nWhat type of swarm is this?",
            ["cpu", "gpu"],
            default="cpu",
        )
    is_gpu_swarm = swarm_type == "gpu"
    challenge_set = GPU_CHALLENGES if is_gpu_swarm else CPU_CHALLENGES
    n_challenges = len(challenge_set)
    type_label = "GPU" if is_gpu_swarm else "CPU"
    print(f"  -> {type_label} swarm ({n_challenges} challenges available)")

    swarm_name = _arg_value(args, "swarm_name")
    if not swarm_name:
        swarm_name = "my-tig-swarm" if yes else prompt(
            "\nSwarm name (used as Railway project + service name; lowercase + dashes)",
            default="my-tig-swarm",
        )

    print(
        f"\nThis swarm hosts all {n_challenges} {type_label} challenges in parallel.\n"
        "The host picks ONE active challenge that contributors automatically\n"
        "work on; you can flip between challenges later via `python setup.py\n"
        "switch` and per-challenge state is preserved on the server (so\n"
        "resuming a previous challenge picks up every agent's prior trajectory).\n"
    )

    use_defaults = _arg_enabled(args, "use_defaults") or yes
    if not use_defaults:
        use_defaults_ans = prompt(
            f"Use defaults for all {n_challenges} challenges? "
            f"({DEFAULT_INSTANCES_PER_TRACK} instances per track, "
            f"default timeout per challenge, empty initial algorithm) [Y/n]",
            default="Y",
        )
        use_defaults = use_defaults_ans.strip().lower() not in ("n", "no")

    # Optional: seed the server's inactive_algorithms pool with the current
    # top-earning TIG mainnet algorithm. Restricted to {knapsack,
    # satisfiability} because those are the only challenges whose mainnet
    # algorithm format slots cleanly into the single-file inactive pool.
    # Resolved here so we honor --yes / --seed-inactive-pool / wizard input,
    # but the actual fetch + POST is deferred until after the server is up.
    seedable = SEED_INACTIVE_SUPPORTED & set(challenge_set.keys())
    seed_inactive_pool = _arg_enabled(args, "seed_inactive_pool")
    if seedable and not seed_inactive_pool and not yes:
        ans = prompt(
            f"Seed the inactive trajectory pool with the current top-earning "
            f"TIG mainnet algorithm for {', '.join(sorted(seedable))}? [y/N]",
            default="N",
        )
        seed_inactive_pool = ans.strip().lower() in ("y", "yes")
    if seed_inactive_pool and not seedable:
        # Host passed the flag on a swarm that doesn't include either
        # supported challenge — warn rather than silently ignoring.
        print(
            "  --seed-inactive-pool requested but neither knapsack nor "
            "satisfiability is in this swarm; nothing to seed."
        )
        seed_inactive_pool = False

    initial_algorithms = read_initial_algorithms()
    challenges_cfg = collect_per_challenge_configs(
        initial_algorithms, use_defaults=use_defaults, challenge_set=challenge_set,
    )

    challenge_names = list(challenge_set.keys())
    default_active = challenge_names[0]
    active_challenge = _arg_value(args, "active_challenge")
    if active_challenge and active_challenge not in challenge_names:
        print(f"{active_challenge} is not available in a {type_label} swarm.")
        print(f"Available challenges: {', '.join(challenge_names)}")
        return 1
    if not active_challenge:
        active_challenge = default_active if yes else prompt_choice(
            "\nWhich challenge should this swarm START with as the active challenge?",
            challenge_names,
            default=default_active,
        )
    challenge_meta = challenge_set[active_challenge]
    print(f"  -> active = {active_challenge} (contributors auto-follow this)")

    if use_defaults:
        # Sensible defaults for the global stagnation knobs; the host can
        # tweak via curl /api/swarm_config later if they want.
        stagnation_threshold = _arg_value(args, "stagnation_threshold") or 2
        stagnation_limit = _arg_value(args, "stagnation_limit")
        stagnation_limit = 10 if stagnation_limit is None else stagnation_limit
        hypothesis_recall_threshold = _arg_value(args, "hypothesis_recall_threshold") or 3
    else:
        stagnation_threshold = _arg_value(args, "stagnation_threshold") or prompt_int(
            "Stagnation threshold (iterations without improvement before hints/inspiration)",
            2, minimum=1,
        )
        stagnation_limit = _arg_value(args, "stagnation_limit")
        if stagnation_limit is None:
            stagnation_limit = prompt_int(
                "Stagnation limit (iterations without improvement before trajectory reset, 0=disabled)",
                10, minimum=0,
            )
        hypothesis_recall_threshold = _arg_value(args, "hypothesis_recall_threshold") or prompt_int(
            "Hypothesis recall threshold (iterations without improvement before "
            "showing prior failed hypotheses for the current program)",
            3, minimum=1,
        )

    admin_key = secrets.token_urlsafe(16)
    swarm_password = secrets.token_urlsafe(16)

    railway_dir = ROOT / ".railway"
    if railway_dir.exists():
        print(f"\nRemoving existing {railway_dir.relative_to(ROOT)} from a prior run.")
        shutil.rmtree(railway_dir)

    print("\nProvisioning on Railway…")
    project = _railway_init_project(swarm_name, workspace=workspace)
    print(f"  project: {project.get('name', swarm_name)}")

    service = _railway_add_service(swarm_name)
    print(f"  service: {service.get('name', swarm_name)}")

    print("  setting environment variables…")
    _railway_set_variables(swarm_name, {
        "DATA_DIR": "/data",
        "ADMIN_KEY": admin_key,
        "SWARM_PASSWORD": swarm_password,
    })

    print("  attaching /data volume…")
    _railway_add_volume(swarm_name, "/data")

    print("  deploying (build logs follow; this takes a few minutes)…\n")
    _railway_up(swarm_name)

    print("\n  fetching public URL…")
    server_url = _railway_domain(swarm_name)
    print(f"  URL: {server_url}")

    print("  waiting for the server to come online…")
    if not _wait_for_server(server_url):
        print(
            "  warning: server did not respond at /api/swarm_config within 60s.\n"
            "  Check `railway logs` for errors. Once it's up, the URL will be\n"
            f"  reachable at {server_url} — point fleet.config.json's server_url at it."
        )

    n_with_code = sum(1 for v in initial_algorithms.values() if v.get("algorithm_code", "").strip())
    n_total = len(initial_algorithms)
    print(f"  read initial algorithms from initial_algorithms/ "
          f"({n_with_code}/{n_total} have content; the rest broadcast empty)")

    # Top-level `tracks` and `timeout` mirror the active challenge's
    # sub-config so `scripts/benchmark.py`'s offline fallback (which reads
    # .swarm-cache.json when the server is unreachable) keeps working.
    active_sub = challenges_cfg[active_challenge]
    active_def = _CHALLENGE_REGISTRY[active_challenge]
    cfg = {
        "swarm_name": swarm_name,
        "owner_name": os.environ.get("USER", "owner"),
        "server_url": server_url,
        "admin_key": admin_key,
        "swarm_password": swarm_password,
        "role": "owner",
        "swarm_type": swarm_type,
        "active_challenge": active_challenge,
        # Active challenge mirrored as `challenge` for back-compat with
        # tooling that still reads the flat key.
        "challenge": active_challenge,
        "challenges": challenges_cfg,
        "stagnation_threshold": stagnation_threshold,
        "stagnation_limit": stagnation_limit,
        "hypothesis_recall_threshold": hypothesis_recall_threshold,
        "scoring_direction": challenge_meta["scoring_direction"],
        "tracks": active_sub["tracks"],
        "timeout": active_sub["timeout"],
        "algorithm_path": f"src/{active_challenge}/algorithm/mod.rs",
    }
    if active_def.is_gpu:
        cfg["kernel_path"] = f"src/{active_challenge}/algorithm/kernels.cu"
        cfg["is_gpu"] = True

    print("  pushing swarm config to the server…")
    push_config_to_server(server_url, admin_key, cfg)

    if seed_inactive_pool:
        print("\nSeeding inactive trajectory pool from TIG mainnet…")
        seed_inactive_pool_from_mainnet(server_url, admin_key, seedable)

    print("\nWriting local files…")
    template_files(
        server_url,
        challenge=active_challenge,
        algorithm_path=cfg["algorithm_path"],
        prior=read_swarm_cache(),
    )
    write_challenge_md(active_challenge)
    write_swarm_admin(cfg)
    write_swarm_cache(cfg)
    _scaffold_fleet_config(server_url, swarm_password)
    repo_url = "<this-repo-url>"
    try:
        result = sp.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, cwd=str(ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            repo_url = result.stdout.strip()
    except Exception:
        pass
    repo_dir_hint = (
        Path(repo_url).stem.replace(".git", "")
        if repo_url != "<this-repo-url>"
        else "tig-swarm-demo"
    )

    print("\n" + "=" * 48)
    print(f"{type_label} SWARM IS LIVE")
    print("=" * 48)
    print(f"\n  Dashboard:  {server_url}/")
    print(f"  Swarm type:  {type_label}")
    print(f"  Active challenge:  {active_challenge}")
    print(f"  All {n_challenges} {type_label} challenges configured and ready (switch via `setup.py switch <name>`).")
    print("\n  Onboard each contributor with:\n")
    print("    python setup.py invite <username>")
    print("    # prints their username + per-contributor swarm_password.")
    print("    # Share both with them; they paste into fleet.config.json with the")
    print(f"    # URL ({server_url}) and run `python scripts/run_fleet.py`.")
    print("\n  Base password (keep private — used by `setup.py invite` to derive")
    print("  per-contributor passwords; rotating it kicks every contributor):")
    print(f"    {swarm_password}")
    print("\n  Admin key (keep private — gates /api/admin/*):")
    print(f"    {admin_key}")
    print("\n  Your own clone has been scaffolded with fleet.config.json —")
    print("  edit the agent entry then run `python scripts/run_fleet.py` to participate.")
    print("\n  Manage the service in Railway: https://railway.com/dashboard")
    print()
    return 0


def _scaffold_fleet_config(server_url: str, swarm_password: str) -> None:
    """After `setup.py create`, leave the host with a working fleet.config.json
    so they can immediately participate via `python scripts/run_fleet.py`.
    Skipped if a fleet.config.json already exists — never clobbers user edits."""
    path = ROOT / "fleet.config.json"
    if path.exists():
        print(f"  fleet.config.json already present — leaving as-is")
        return
    starter = {
        "server_url": server_url,
        "swarm_password": swarm_password,
        "agents": [
            {
                "name": os.environ.get("USER", "agent-1"),
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "api_key_env": "ANTHROPIC_API_KEY",
                "compute": "local",
            }
        ],
    }
    path.write_text(json.dumps(starter, indent=2) + "\n")
    print(f"  scaffolded {path.relative_to(ROOT)} (one agent — edit before launching)")


# ── Switch / sync subcommands ─────────────────────────────────────────


def run_switch(challenge: str) -> int:
    """Host-only: change the swarm's active challenge.

    POSTs to /api/swarm_config (admin-key gated), then refreshes the local
    .swarm-cache.json and re-templates CHALLENGE.md. Contributors auto-follow
    on their next iteration via `setup.py sync`.

    Switching is restricted to challenges of the same type (CPU/GPU) as
    the swarm was created with."""
    if challenge not in CHALLENGES:
        print(f"unknown challenge: {challenge}")
        print(f"choose from: {', '.join(CHALLENGES)}")
        return 1
    admin = read_swarm_admin()
    if not admin.get("admin_key"):
        print("swarm.admin.json not found — `setup.py switch` is host-only; "
              "run `python setup.py create` first.")
        return 1
    server_url = resolve_server_url()
    if not server_url:
        print("no server_url found — run `python setup.py create` first.")
        return 1
    admin_key = admin["admin_key"]
    cache = read_swarm_cache()
    swarm_type = cache.get("swarm_type", "cpu")
    is_gpu_swarm = swarm_type == "gpu"
    target_is_gpu = _CHALLENGE_REGISTRY[challenge].is_gpu
    if target_is_gpu != is_gpu_swarm:
        allowed = GPU_CHALLENGES if is_gpu_swarm else CPU_CHALLENGES
        label = "GPU" if is_gpu_swarm else "CPU"
        print(f"This is a {label} swarm — cannot switch to "
              f"{'GPU' if target_is_gpu else 'CPU'} challenge '{challenge}'.")
        print(f"Available challenges: {', '.join(allowed)}")
        return 1

    # 1. POST the new active_challenge to the server.
    payload = {"admin_key": admin_key, "active_challenge": challenge}
    req = urllib.request.Request(
        f"{server_url.rstrip('/')}/api/swarm_config",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            json.load(resp)
        print(f"  active_challenge → {challenge} on {server_url}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"  could not reach {server_url} ({e}); aborting switch.")
        return 1

    # 2. Refresh the local cache + CHALLENGE.md so the host can also work on
    #    the new challenge from their own clone.
    new_algo_path = f"src/{challenge}/algorithm/mod.rs"
    ch_def = _CHALLENGE_REGISTRY[challenge]
    sub = fetch_challenge_sub_config(server_url, challenge)
    template_files(
        server_url, challenge=challenge,
        algorithm_path=new_algo_path, prior=cache,
    )
    write_challenge_md(challenge)
    refreshed = {
        "server_url": server_url,
        "swarm_type": swarm_type,
        "active_challenge": challenge,
        "challenge": challenge,
        "algorithm_path": new_algo_path,
    }
    if ch_def.is_gpu:
        refreshed["kernel_path"] = f"src/{challenge}/algorithm/kernels.cu"
        refreshed["is_gpu"] = True
    if sub:
        refreshed["tracks"] = sub.get("tracks", {})
        refreshed["timeout"] = sub.get("timeout", 5)
        refreshed["scoring_direction"] = sub.get("scoring_direction", "max")
    write_swarm_cache(refreshed)

    prior_challenge = cache.get("active_challenge")
    print(f"\nActive challenge → {challenge} (broadcast to all contributors).")
    if prior_challenge and prior_challenge != challenge:
        print(f"  Prior trajectories on {prior_challenge} are preserved")
        print(f"  server-side and resume on switch-back.")
    print("  All contributors auto-follow on their next iteration —")
    print("  scripts/run_loop.py runs `setup.py sync` at the top of each loop.")
    return 0


def run_sync() -> int:
    """Pull live config from the server and refresh .swarm-cache.json.

    Idempotent — re-templates CHALLENGE.md only when active_challenge changes.
    Called by scripts/run_loop.py at the top of every iteration so a host's
    challenge switch propagates to running contributors automatically.
    """
    server_url = resolve_server_url()
    if not server_url:
        print(
            "no server_url found — run `python setup.py create` (host) or "
            "edit fleet.config.json (contributor)."
        )
        return 1
    server_url = server_url.rstrip("/")

    try:
        with urllib.request.urlopen(
            f"{server_url}/api/swarm_config", timeout=4
        ) as r:
            live = json.load(r)
    except Exception as e:
        print(f"  could not reach {server_url} ({e}); skipping sync.")
        return 0

    new_challenge = live.get("active_challenge") or live.get("challenge")
    if not new_challenge:
        print("server returned no active_challenge; nothing to sync.")
        return 0

    cache = read_swarm_cache()
    # If the cache was written against a different server, it's a leftover from
    # a prior swarm (e.g. fleet.config.json was repointed). Treat it as absent
    # so we don't take the early-return below and don't feed its stale
    # prior_url into template_files (which would mis-rewrite URLs).
    cache_server = (cache.get("server_url") or "").rstrip("/")
    if cache_server and cache_server != server_url:
        cache = {}
    local_challenge = cache.get("active_challenge") or cache.get("challenge")

    # Build the refreshed cache payload from live server state.
    new_algo_path = f"src/{new_challenge}/algorithm/mod.rs"
    ch_def = _CHALLENGE_REGISTRY.get(new_challenge)
    sub = fetch_challenge_sub_config(server_url, new_challenge)
    refreshed = {
        "server_url": server_url,
        "active_challenge": new_challenge,
        "challenge": new_challenge,
        "algorithm_path": new_algo_path,
    }
    if live.get("swarm_type"):
        refreshed["swarm_type"] = live["swarm_type"]
    if ch_def and ch_def.is_gpu:
        refreshed["kernel_path"] = f"src/{new_challenge}/algorithm/kernels.cu"
        refreshed["is_gpu"] = True
    if sub:
        refreshed["tracks"] = sub.get("tracks", {})
        refreshed["timeout"] = sub.get("timeout", 5)
        refreshed["scoring_direction"] = sub.get("scoring_direction", "max")
    write_swarm_cache(refreshed)

    # Don't early-return when CHALLENGE.md is missing: a fresh fleet worktree
    # inherits .swarm-cache.json from the host clone (so local_challenge ==
    # new_challenge) but CHALLENGE.md is gitignored and gets left behind,
    # which would otherwise leave the LLM with an empty challenge spec.
    challenge_md = ROOT / "CHALLENGE.md"
    if new_challenge == local_challenge and challenge_md.exists():
        print(f"already in sync (active_challenge = {new_challenge}).")
        return 0

    template_files(
        server_url, challenge=new_challenge,
        algorithm_path=new_algo_path, prior=cache,
    )
    write_challenge_md(new_challenge)
    if new_challenge == local_challenge:
        print(f"refreshed CHALLENGE.md (active_challenge unchanged: {new_challenge}).")
        return 0
    print(f"\nSynced to {new_challenge} (was {local_challenge or '<none>'}).")
    print("  Your prior trajectory on this challenge (if any) will resume server-side.")
    print("  scripts/run_loop.py picks up the new CHALLENGE.md on its next iteration.")
    return 0




# ── Entrypoint ──────────────────────────────────────────────────────


def add_create_setup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", help="Railway workspace name.")
    parser.add_argument("--swarm-name", help="Railway project/service name.")
    parser.add_argument("--swarm-type", choices=["cpu", "gpu"], help="Swarm hardware class.")
    parser.add_argument("--active-challenge", choices=list(CHALLENGES.keys()), help="Initial active challenge.")
    parser.add_argument("--use-defaults", action="store_true", help="Use default tracks/timeouts for every challenge.")
    parser.add_argument("--stagnation-threshold", type=int, help="Iterations before hints/inspiration.")
    parser.add_argument("--stagnation-limit", type=int, help="Iterations before trajectory reset; 0 disables.")
    parser.add_argument("--hypothesis-recall-threshold", type=int, help="Iterations before prior failed hypotheses are shown.")
    parser.add_argument("--yes", action="store_true", help="Accept defaults for any optional prompts.")
    parser.add_argument(
        "--seed-inactive-pool", action="store_true",
        help=(
            "After deploy, seed the server's inactive_algorithms pool with the "
            "current top-earning TIG mainnet algorithm for knapsack and/or "
            "satisfiability (only these two challenges are supported)."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description=(
            "Host-admin tool for the TIG swarm. Contributors edit "
            "fleet.config.json and run scripts/run_fleet.py — they do not "
            "need setup.py."
        ),
    )
    sub = parser.add_subparsers(dest="mode")
    create = sub.add_parser(
        "create",
        help="Host: provision a new swarm on Railway (drives the railway CLI).",
    )
    add_create_setup_args(create)
    switch = sub.add_parser(
        "switch",
        help="Host: change the swarm's active challenge (broadcast to all contributors).",
    )
    switch.add_argument(
        "challenge", choices=list(CHALLENGES.keys()),
        help="The challenge to switch the swarm to.",
    )
    sub.add_parser(
        "sync",
        help=("Refresh .swarm-cache.json from the live server (idempotent; "
              "called by scripts/run_loop.py at the top of each iteration)."),
    )
    tacit = sub.add_parser(
        "tacit",
        help="Interactive helper to fill in an agent's tacit_knowledge file.",
    )
    tacit.add_argument(
        "agent_name", nargs="?",
        help="Name of the fleet agent to edit (default: only agent in fleet.config.json).",
    )
    invite = sub.add_parser(
        "invite",
        help="Host: issue a per-contributor swarm password (username + derived hash).",
    )
    invite.add_argument(
        "username",
        help="Contributor's username (anything identifying them — paste into fleet.config.json).",
    )
    args = parser.parse_args()

    if args.mode == "create":
        return run_create(args)
    if args.mode == "switch":
        return run_switch(args.challenge)
    if args.mode == "sync":
        return run_sync()
    if args.mode == "tacit":
        return run_tacit(args.agent_name)
    if args.mode == "invite":
        return run_invite(args.username)

    print(
        "setup.py is the host-admin tool.\n"
        "  contributors:  edit fleet.config.json, then run "
        "`python scripts/run_fleet.py`.\n"
        "  hosts:         `python setup.py create` to provision a new swarm.\n"
        "  invite a contributor:  `python setup.py invite <username>`.\n"
        "  switch challenge:      `python setup.py switch <challenge>`.\n"
        "  edit tacit knowledge:  `python setup.py tacit [<agent-name>]`.",
        file=sys.stderr,
    )
    return 1


def run_invite(username: str) -> int:
    """Issue a per-contributor swarm password by computing
    sha256(username + ':' + base_password). Prints the username + derived
    hash for the host to share out-of-band with the contributor."""
    import hashlib
    username = (username or "").strip()
    if not username:
        print("invite: username must be non-empty", file=sys.stderr)
        return 1
    admin = read_swarm_admin()
    base = (admin.get("swarm_password") or "").strip()
    if not base:
        print(
            "invite: no swarm_password in swarm.admin.json — "
            "run `setup.py create` first (host machine only).",
            file=sys.stderr,
        )
        return 1
    derived = hashlib.sha256(f"{username}:{base}".encode()).hexdigest()
    server_url = admin.get("server_url") or read_swarm_cache().get("server_url") or "<paste server URL>"
    print()
    print(f"  Contributor:    {username}")
    print(f"  Server URL:     {server_url}")
    print(f"  swarm_password: {derived}")
    print()
    print("  Share the three values above with the contributor.")
    print("  They paste server_url, username, and swarm_password into")
    print("  their fleet.config.json, then run `python scripts/run_fleet.py`.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
