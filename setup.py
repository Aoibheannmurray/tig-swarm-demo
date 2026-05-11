#!/usr/bin/env python3
"""TIG Swarm setup wizard.

Two modes:

  python setup.py create      Owner: stand up a new swarm on Railway. Drives
                              the `railway` CLI to create a project + service
                              + volume, sets env vars, deploys the server,
                              then pushes swarm-wide config (challenge, tracks,
                              timeout, …) to the live URL. Prints a share link
                              for contributors.

  python setup.py join URL    Contributor: point this clone at an existing
                              swarm URL. Templates the URL into AGENTS.md /
                              scripts and creates a stub tacit_knowledge_personal.md
                              for the agent's private hints.

Re-running either mode is safe — `create` always provisions a brand-new
Railway project (overwriting the local `.railway/` link), and `join`
overwrites the same set of templated files.

Files this script reads / writes:
  - AGENTS.md, README.md, scripts/publish.py
    (templated: ${SERVER_URL} -> the chosen URL)
  - swarm.config.json (owner-only mirror of what's stored on the server)
  - CHALLENGE.md (per-challenge docs, from src/<challenge>/README.md)
  - tacit_knowledge_personal.md (per-contributor, gitignored)
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
from pathlib import Path

ROOT = Path(__file__).parent

# Files that carry swarm-specific values (URL, active challenge name) the
# wizard rewrites in-place.  benchmark.py and publish.py are intentionally
# excluded — they contain challenge-generic code (function names, data keys,
# docstrings for all five challenges) that must not be rewritten.  They read
# the active challenge from swarm.config.json at runtime instead.
TEMPLATED_FILES = [
    ROOT / "README.md",
]
AGENTS_TEMPLATE = ROOT / "AGENTS.md.template"
AGENTS_OUTPUT = ROOT / "AGENTS.md"

# Heuristic URL patterns that the wizard treats as "the swarm URL" and
# replaces with the new one. Catches the canonical Railway domain and raw
# IP-form URLs from older self-host setups. Without this, a clone that has
# a baked URL not matching prior swarm.config.json's `server_url` (e.g.
# someone committed their templated state, or migrated between hosting
# styles) silently fails to re-template.
_RAILWAY_URL_RE = re.compile(r"https?://[a-zA-Z0-9-]+\.up\.railway\.app")
_RAW_IP_URL_RE = re.compile(r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?")

# The literal placeholder strings the tracked files carry. NEVER replace
# arbitrary URLs — too easy to clobber rustup / GitHub / localhost dev URLs
# that happen to live in the same files.
PLACEHOLDER_URL = "${SERVER_URL}"
PLACEHOLDER_CHALLENGE = "${CHALLENGE_NAME}"
PLACEHOLDER_ALGO = "${ALGORITHM_PATH}"
PLACEHOLDER_TIMEOUT = "${TIMEOUT}"

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

DEFAULT_TIMEOUT = 30
DEFAULT_INSTANCES_PER_TRACK = 2
DEFAULT_TRACKS_PER_CHALLENGE = {
    "satisfiability": {"n_vars=100000,ratio=4150": 2},
    "vehicle_routing": {"n_nodes=600": 2},
    "knapsack": {"n_items=1000,budget=10": 2},
    "job_scheduling": {"n=20,s=HYBRID_FLOW_SHOP": 2},
    "energy_arbitrage": {"s=BASELINE": 2},
    "hypergraph": {"n_h_edges=10000": 2},
    "neuralnet_optimizer": {"n_hidden=4": 2},
}


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


def _strip_conditional_blocks(text: str, is_gpu: bool) -> str:
    """Process <!-- IF_GPU/IF_CPU --> blocks: keep matching, strip non-matching."""
    if is_gpu:
        text = re.sub(r'<!-- IF_GPU -->\n?', '', text)
        text = re.sub(r'<!-- END_GPU -->\n?', '', text)
        text = re.sub(r'<!-- IF_CPU -->.*?<!-- END_CPU -->\n?', '', text, flags=re.DOTALL)
    else:
        text = re.sub(r'<!-- IF_CPU -->\n?', '', text)
        text = re.sub(r'<!-- END_CPU -->\n?', '', text)
        text = re.sub(r'<!-- IF_GPU -->.*?<!-- END_GPU -->\n?', '', text, flags=re.DOTALL)
    return text


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
    timeout: int | None = None,
) -> None:
    """Substitute swarm-specific placeholders into every tracked file that
    contains them. AGENTS.md is regenerated from AGENTS.md.template each
    time (with GPU/CPU conditional blocks resolved); other files are
    updated in-place using prior values from swarm.config.json.
    """
    prior = prior or {}
    prior_url = prior.get("server_url")
    prior_challenge = prior.get("challenge")
    prior_algo = prior.get("algorithm_path")

    ch_def = _CHALLENGE_REGISTRY.get(challenge) if challenge else None
    is_gpu = ch_def.is_gpu if ch_def else False

    if AGENTS_TEMPLATE.exists():
        text = AGENTS_TEMPLATE.read_text()
        text = text.replace(PLACEHOLDER_URL, server_url)
        if challenge:
            text = text.replace(PLACEHOLDER_CHALLENGE, challenge)
        text = text.replace(PLACEHOLDER_TIMEOUT, str(timeout or DEFAULT_TIMEOUT))
        text = _strip_conditional_blocks(text, is_gpu)
        AGENTS_OUTPUT.write_text(text)
        print(f"  generated {AGENTS_OUTPUT.relative_to(ROOT)}")

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


def write_swarm_config(cfg: dict) -> None:
    out = ROOT / "swarm.config.json"
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"  wrote {out.relative_to(ROOT)}")


def read_prior_swarm_config() -> dict | None:
    out = ROOT / "swarm.config.json"
    if not out.exists():
        return None
    try:
        return json.loads(out.read_text())
    except Exception:
        return None


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
            f"this setup, or POST swarm.config.json yourself once it's up."
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
    server. Used by switch / sync / join to mirror the active challenge's
    sub-config to top-level swarm.config.json keys so benchmark.py's
    offline fallback keeps working."""
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
            for key in meta["track_keys"]:
                tracks[key] = prompt_int(
                    f"  instances for {key}", DEFAULT_INSTANCES_PER_TRACK, minimum=0
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


def open_in_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    print(f"  opening {path.relative_to(ROOT)} in {editor} (Ctrl-X to exit nano)…")
    try:
        os.system(f"{editor} {path}")
    except Exception as e:
        print(f"  could not launch editor: {e}; edit {path} by hand")


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
    swarm's actual config (the server reads stagnation_threshold from the
    same swarm.config.json the wizard writes)."""
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


def init_personal_tacit_knowledge(stagnation_threshold: int = 2) -> Path:
    """Create the contributor's private tacit_knowledge file if missing.
    The agent later renames it to tacit_knowledge_<agent_name>.md after it
    learns its own name from POST /api/agents/register."""
    path = ROOT / "tacit_knowledge_personal.md"
    if path.exists():
        return path
    path.write_text(
        tacit_header(stagnation_threshold)
        + "- (replace this with your own hint, or run setup again)\n"
    )
    print(f"  created {path.relative_to(ROOT)} (gitignored — edit it any time)")
    return path


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


def run_create() -> int:
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
    workspace = _pick_workspace(user)

    swarm_type = prompt_choice(
        "\nWhat type of swarm is this?",
        ["cpu", "gpu"],
        default="cpu",
    )
    is_gpu_swarm = swarm_type == "gpu"
    challenge_set = GPU_CHALLENGES if is_gpu_swarm else CPU_CHALLENGES
    n_challenges = len(challenge_set)
    type_label = "GPU" if is_gpu_swarm else "CPU"
    print(f"  -> {type_label} swarm ({n_challenges} challenges available)")

    swarm_name = prompt(
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

    use_defaults_ans = prompt(
        f"Use defaults for all {n_challenges} challenges? "
        f"({DEFAULT_INSTANCES_PER_TRACK} instances per track, "
        f"default timeout per challenge, empty initial algorithm) [Y/n]",
        default="Y",
    )
    use_defaults = use_defaults_ans.strip().lower() not in ("n", "no")

    initial_algorithms = read_initial_algorithms()
    challenges_cfg = collect_per_challenge_configs(
        initial_algorithms, use_defaults=use_defaults, challenge_set=challenge_set,
    )

    challenge_names = list(challenge_set.keys())
    default_active = challenge_names[0]
    active_challenge = prompt_choice(
        "\nWhich challenge should this swarm START with as the active challenge?",
        challenge_names,
        default=default_active,
    )
    challenge_meta = challenge_set[active_challenge]
    print(f"  -> active = {active_challenge} (contributors auto-follow this)")

    if use_defaults:
        # Sensible defaults for the global stagnation knobs; the host can
        # tweak via curl /api/swarm_config later if they want.
        stagnation_threshold = 2
        stagnation_limit = 10
        hypothesis_recall_threshold = 3
    else:
        stagnation_threshold = prompt_int(
            "Stagnation threshold (iterations without improvement before hints/inspiration)",
            2, minimum=1,
        )
        stagnation_limit = prompt_int(
            "Stagnation limit (iterations without improvement before trajectory reset, 0=disabled)",
            10, minimum=0,
        )
        hypothesis_recall_threshold = prompt_int(
            "Hypothesis recall threshold (iterations without improvement before "
            "showing prior failed hypotheses for the current program)",
            3, minimum=1,
        )

    tk_path = init_personal_tacit_knowledge(stagnation_threshold)
    gather_tacit_knowledge(tk_path, stagnation_threshold)

    admin_key = secrets.token_urlsafe(16)

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
    _railway_set_variables(swarm_name, {"DATA_DIR": "/data", "ADMIN_KEY": admin_key})

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
            "  Check `railway logs` for errors. You can re-run\n"
            f"  `python setup.py join {server_url}` once it's up to finish wiring."
        )

    n_with_code = sum(1 for v in initial_algorithms.values() if v.get("algorithm_code", "").strip())
    n_total = len(initial_algorithms)
    print(f"  read initial algorithms from initial_algorithms/ "
          f"({n_with_code}/{n_total} have content; the rest broadcast empty)")

    # Top-level `tracks` and `timeout` mirror the active challenge's
    # sub-config so `scripts/benchmark.py`'s offline fallback (which reads
    # swarm.config.json when the server is unreachable) keeps working.
    active_sub = challenges_cfg[active_challenge]
    active_def = _CHALLENGE_REGISTRY[active_challenge]
    cfg = {
        "swarm_name": swarm_name,
        "owner_name": os.environ.get("USER", "owner"),
        "server_url": server_url,
        "admin_key": admin_key,
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

    prior = read_prior_swarm_config()
    print("\nWriting local files…")
    template_files(
        server_url,
        challenge=active_challenge,
        algorithm_path=cfg["algorithm_path"],
        prior=prior,
        timeout=active_sub["timeout"],
    )
    write_challenge_md(active_challenge)
    write_swarm_config(cfg)
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
    print("\n  Share this with anyone who wants to join:\n")
    print(f"    git clone {repo_url}")
    print(f"    cd {repo_dir_hint}")
    print(f"    python setup.py join {server_url}")
    print("\n  Admin key (keep private — gates /api/admin/*):")
    print(f"    {admin_key}")
    print("\n  Manage the service in Railway: https://railway.com/dashboard")
    print()
    return 0


# ── Switch / sync subcommands ─────────────────────────────────────────


def run_switch(challenge: str) -> int:
    """Owner-only: change the swarm's active challenge.

    POSTs the new active_challenge to /api/swarm_config (admin-key gated),
    re-templates the owner's local files so they can also work on the new
    challenge, and updates swarm.config.json. Contributors auto-follow on
    their next iteration via `setup.py sync` (Step 0 of the agent loop).

    Switching is restricted to challenges of the same type (CPU/GPU) as
    the swarm was created with."""
    if challenge not in CHALLENGES:
        print(f"unknown challenge: {challenge}")
        print(f"choose from: {', '.join(CHALLENGES)}")
        return 1
    prior = read_prior_swarm_config()
    if not prior:
        print("no swarm.config.json found — run `python setup.py create` (host) "
              "or `python setup.py join <URL>` (contributor) first.")
        return 1
    swarm_type = prior.get("swarm_type", "cpu")
    is_gpu_swarm = swarm_type == "gpu"
    target_is_gpu = _CHALLENGE_REGISTRY[challenge].is_gpu
    if target_is_gpu != is_gpu_swarm:
        allowed = GPU_CHALLENGES if is_gpu_swarm else CPU_CHALLENGES
        label = "GPU" if is_gpu_swarm else "CPU"
        print(f"This is a {label} swarm — cannot switch to "
              f"{'GPU' if target_is_gpu else 'CPU'} challenge '{challenge}'.")
        print(f"Available challenges: {', '.join(allowed)}")
        return 1
    if prior.get("role") != "owner":
        print("Only the swarm owner can change the active challenge.")
        print(f"Ask the swarm owner to run `python setup.py switch {challenge}`.")
        print("Your local clone will auto-follow the owner's choice when "
              "you next run `python setup.py sync`.")
        return 2
    server_url = prior.get("server_url")
    admin_key = prior.get("admin_key")
    if not server_url or not admin_key:
        print("missing server_url or admin_key in swarm.config.json — "
              "re-run `setup.py create`.")
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

    # 2. Re-template the owner's local clone so they can also work on the new challenge.
    new_algo_path = f"src/{challenge}/algorithm/mod.rs"
    ch_def = _CHALLENGE_REGISTRY[challenge]
    # Mirror the new challenge's sub-config to top-level so benchmark.py's
    # offline fallback uses the right tracks/timeout.
    sub = fetch_challenge_sub_config(server_url, challenge)
    template_files(
        server_url, challenge=challenge,
        algorithm_path=new_algo_path, prior=prior,
        timeout=sub.get("timeout") if sub else None,
    )
    write_challenge_md(challenge)
    cfg = dict(prior)
    cfg["active_challenge"] = challenge
    cfg["challenge"] = challenge
    cfg["algorithm_path"] = new_algo_path
    if ch_def.is_gpu:
        cfg["kernel_path"] = f"src/{challenge}/algorithm/kernels.cu"
        cfg["is_gpu"] = True
    else:
        cfg.pop("kernel_path", None)
        cfg.pop("is_gpu", None)
    if sub:
        cfg["tracks"] = sub.get("tracks", {})
        cfg["timeout"] = sub.get("timeout", 5)
        cfg["scoring_direction"] = sub.get("scoring_direction", "max")
    write_swarm_config(cfg)

    print(f"\nActive challenge → {challenge} (broadcast to all contributors).")
    if prior.get("active_challenge") and prior["active_challenge"] != challenge:
        print(f"  Prior trajectories on {prior['active_challenge']} are preserved")
        print(f"  server-side and resume on switch-back.")
    print("  All contributors auto-follow on their next iteration.")
    print("\nTell your running agent (or your driver script):")
    print("  'Re-read AGENTS.md and continue the loop on the new challenge.'")
    return 0


def run_sync() -> int:
    """Contributor (or owner): pull live config from the server and re-template
    the local clone if the active_challenge has changed since last sync.

    Idempotent — no-op when in sync. Called by the agent loop at Step 0 so
    the contributor's local files auto-follow the owner's challenge choice."""
    prior = read_prior_swarm_config()
    if not prior:
        print("no swarm.config.json found — run `python setup.py join <URL>` first.")
        return 1
    server_url = prior.get("server_url")
    if not server_url:
        print("missing server_url in swarm.config.json — re-run setup.")
        return 1
    try:
        with urllib.request.urlopen(
            f"{server_url.rstrip('/')}/api/swarm_config", timeout=4
        ) as r:
            live = json.load(r)
    except Exception as e:
        print(f"  could not reach {server_url} ({e}); skipping sync.")
        return 0

    new_challenge = live.get("active_challenge") or live.get("challenge")
    if not new_challenge:
        print("server returned no active_challenge; nothing to sync.")
        return 0
    local_challenge = prior.get("active_challenge") or prior.get("challenge")
    if new_challenge == local_challenge:
        print(f"already in sync (active_challenge = {new_challenge}).")
        return 0

    new_algo_path = f"src/{new_challenge}/algorithm/mod.rs"
    ch_def = _CHALLENGE_REGISTRY.get(new_challenge)
    sub = fetch_challenge_sub_config(server_url, new_challenge)
    template_files(
        server_url, challenge=new_challenge,
        algorithm_path=new_algo_path, prior=prior,
        timeout=sub.get("timeout") if sub else None,
    )
    write_challenge_md(new_challenge)
    cfg = dict(prior)
    cfg["active_challenge"] = new_challenge
    cfg["challenge"] = new_challenge
    cfg["algorithm_path"] = new_algo_path
    live_swarm_type = live.get("swarm_type")
    if live_swarm_type:
        cfg["swarm_type"] = live_swarm_type
    if ch_def and ch_def.is_gpu:
        cfg["kernel_path"] = f"src/{new_challenge}/algorithm/kernels.cu"
        cfg["is_gpu"] = True
    else:
        cfg.pop("kernel_path", None)
        cfg.pop("is_gpu", None)
    if sub:
        cfg["tracks"] = sub.get("tracks", {})
        cfg["timeout"] = sub.get("timeout", 5)
        cfg["scoring_direction"] = sub.get("scoring_direction", "max")
    write_swarm_config(cfg)
    print(f"\nSynced to {new_challenge} (was {local_challenge}).")
    print("  Your prior trajectory on this challenge (if any) will resume server-side.")
    print("  Tell your running agent (or driver script): 're-read AGENTS.md'.")
    return 0


_LLM_CHOICES = [
    "claude_code",
    "claude_api",
    "openai_api",
    "gemini_api",
    "other",
]


def _prompt_contributor_identity(prior: dict | None) -> tuple[str | None, str | None]:
    """Ask the contributor for an optional display name and which LLM is
    driving their agent. Both are optional (Enter to accept defaults).

    The chosen values are stored in swarm.config.json under
    `contributor_name` / `contributor_llm` and forwarded to the server on
    `/api/agents/register` so the dashboard can show "alice (gemini_api)"
    instead of every contributor showing as a generated codename."""
    print(
        "\n── Your identity (optional) ──\n"
        "Pick a name for your agent and tell the swarm which LLM is driving it.\n"
        "Both default to the values from a previous run / a server-generated\n"
        "codename — press Enter to skip either prompt.\n"
    )
    prior_name = (prior or {}).get("contributor_name") or ""
    prior_llm = (prior or {}).get("contributor_llm") or "claude_code"
    name_default = prior_name or "(let server generate)"
    raw_name = input(f"Agent name [{name_default}]: ").strip()
    if raw_name == "" and prior_name:
        chosen_name: str | None = prior_name
    elif raw_name == "":
        chosen_name = None
    else:
        chosen_name = raw_name
    llm = prompt_choice(
        "Which LLM drives this agent?", _LLM_CHOICES, default=prior_llm,
    )
    if llm == "other":
        llm = input("Model name (shown on the dashboard): ").strip() or "other"
    return chosen_name, llm


def run_join(server_url: str) -> int:
    print(f"TIG Swarm — joining {server_url}")
    print("=" * 48)

    prior = read_prior_swarm_config()
    challenge = None
    algorithm_path = None
    stagnation_threshold = 2
    swarm_type = "cpu"
    timeout = None
    try:
        with urllib.request.urlopen(f"{server_url.rstrip('/')}/api/swarm_config", timeout=4) as r:
            swarm = json.load(r)
        challenge = swarm.get("active_challenge") or swarm.get("challenge")
        stagnation_threshold = swarm.get("stagnation_threshold", 2)
        swarm_type = swarm.get("swarm_type", "cpu")
        timeout = swarm.get("timeout")
        if challenge:
            algorithm_path = f"src/{challenge}/algorithm/mod.rs"
    except Exception as e:
        print(f"  couldn't fetch swarm config from {server_url}: {e}")
        print("  AGENTS.md / CHALLENGE.md will only have the URL templated; rerun this command once the server is up.")

    contributor_name, contributor_llm = _prompt_contributor_identity(prior)

    type_label = "GPU" if swarm_type == "gpu" else "CPU"
    print(f"  swarm type: {type_label}")

    template_files(
        server_url,
        challenge=challenge,
        algorithm_path=algorithm_path,
        prior=prior,
        timeout=timeout,
    )
    if challenge:
        write_challenge_md(challenge)

    # Stash a minimal record so a future re-run can swap the URL/challenge
    # without leaving stale strings in the templated files. `active_challenge`
    # is the field `setup.py sync` checks on every agent iteration to detect
    # owner-driven challenge switches.
    cfg_out = {
        "server_url": server_url,
        "role": "contributor",
        "swarm_type": swarm_type,
        "active_challenge": challenge or (prior or {}).get("active_challenge"),
        "challenge": challenge or (prior or {}).get("challenge"),
        "algorithm_path": algorithm_path or (prior or {}).get("algorithm_path"),
        # Identity persisted across runs. The agent loop's register curl
        # reads these from swarm.config.json and forwards them to
        # /api/agents/register so the dashboard can show the LLM type
        # alongside the agent's name.
        "contributor_name": contributor_name,
        "contributor_llm": contributor_llm,
    }
    if challenge:
        ch_def = _CHALLENGE_REGISTRY.get(challenge)
        if ch_def and ch_def.is_gpu:
            cfg_out["kernel_path"] = f"src/{challenge}/algorithm/kernels.cu"
            cfg_out["is_gpu"] = True
    # Mirror the active challenge's sub-config to top-level so benchmark.py's
    # offline fallback finds the right tracks/timeout.
    if challenge:
        sub = fetch_challenge_sub_config(server_url, challenge)
        if sub:
            cfg_out["tracks"] = sub.get("tracks", {})
            cfg_out["timeout"] = sub.get("timeout", 5)
            cfg_out["scoring_direction"] = sub.get("scoring_direction", "max")
    write_swarm_config(cfg_out)

    tk_path = init_personal_tacit_knowledge(stagnation_threshold)
    gather_tacit_knowledge(tk_path, stagnation_threshold)

    print(
        "\nDone — this clone is now wired into the swarm. Pick how you want\n"
        "to drive the optimization loop:\n"
        "\n  1. Coding agent (Claude Code, Codex, Gemini CLI, Cursor, Aider, …):\n"
        "     Open Claude Code in this directory and have it read AGENTS.md\n"
        "     to start contributing.\n"
        "\n  2. Direct API calls (Anthropic, OpenAI, Google, OpenAI-compatible, …):\n"
        "     Export your API key and run this command to start contributing:\n"
        "\n         export ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY, GOOGLE_API_KEY\n"
        "         python scripts/run_loop.py --provider anthropic\n"
        "\n     `python scripts/run_loop.py --help` lists the other providers,\n"
        "     OpenAI-compatible endpoints, and how to resume an existing agent.\n"
        "     The README has the full rundown.\n"
        "\nEdit tacit_knowledge_personal.md any time with private hints — they\n"
        "only ever live on your machine.\n"
        "\nWhen the swarm owner switches the active challenge, your loop's\n"
        "Step 0 (`python setup.py sync`) picks up the change automatically on\n"
        "the next iteration.\n"
    )
    return 0


# ── Entrypoint ──────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(prog="setup.py")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser(
        "create",
        help="Owner: provision a new swarm on Railway (drives the railway CLI).",
    )
    join = sub.add_parser("join", help="Contributor: point this clone at a swarm URL.")
    join.add_argument("server_url", help="The swarm owner's server URL.")
    switch = sub.add_parser(
        "switch",
        help=("Owner: change the swarm's active challenge "
              "(broadcast to all contributors)."),
    )
    switch.add_argument(
        "challenge", choices=list(CHALLENGES.keys()),
        help="The challenge to switch the swarm to.",
    )
    sub.add_parser(
        "sync",
        help=("Contributor: pull live config from the server and re-template "
              "this clone if the active challenge changed (idempotent; "
              "called by the agent loop at Step 0)."),
    )
    args = parser.parse_args()

    if args.mode == "create":
        return run_create()
    if args.mode == "join":
        return run_join(args.server_url)
    if args.mode == "switch":
        return run_switch(args.challenge)
    if args.mode == "sync":
        return run_sync()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
