#!/usr/bin/env python3
"""Interactive wizard that generates fleet.config.json from the example.

Walks contributors through the minimum decisions needed to join a swarm:
the host's connection details, which LLM provider/model to run, how many
agents to spawn, and whether their API key is exported. Tech-savvy users
can skip this and hand-edit fleet.config.json themselves — the wizard
just gets you a working file faster.

Usage:
    python scripts/init_fleet.py            # interactive wizard
    python scripts/init_fleet.py --force    # overwrite existing fleet.config.json
"""

from __future__ import annotations

# Python-version preflight — fires before any other import in case those
# imports use PEP 585 / PEP 604 runtime forms that don't exist on older Python.
# `%` formatting and a bare `sys` import keep the message readable even on
# Python 2.x or very old 3.x (Ubuntu 20.04 still ships 3.8 as system python,
# RHEL 8 ships 3.6). Without this, contributors on those versions hit a
# confusing `TypeError: 'type' object is not subscriptable` from some
# downstream module instead of a clear "upgrade Python" pointer.
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
import random
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLEET_CONFIG_PATH = ROOT / "fleet.config.json"
EXAMPLE_PATH = ROOT / "fleet.config.example.json"

# Windows console crashes on the box-drawing characters / checkmark glyphs this
# wizard prints when the active code page isn't UTF-8 ("UnicodeEncodeError:
# 'charmap' codec can't encode …"). Force the stream to UTF-8 with replacement
# so contributors don't have to remember `python -X utf8`. No-op on Linux/macOS
# where the default already is UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# Keep in sync with DEFAULT_MODELS in scripts/llm_backends.py and the
# provider list in scripts/run_loop.py. Tuple: (label, default_model,
# api_key_env or None, short_name_stub, supports_c3, blurb).
PROVIDERS: list[tuple[str, str, str, str | None, str, bool, str]] = [
    ("anthropic",
     "Anthropic (Claude API)",
     "claude-opus-4-7",
     "ANTHROPIC_API_KEY",
     "claude",
     True,
     "Claude via Anthropic's API. Needs ANTHROPIC_API_KEY."),
    ("openai",
     "OpenAI (GPT API)",
     "gpt-5",
     "OPENAI_API_KEY",
     "gpt",
     True,
     "GPT via OpenAI's API. Needs OPENAI_API_KEY."),
    ("google",
     "Google (Gemini API)",
     "gemini-2.5-pro",
     "GOOGLE_API_KEY",
     "gemini",
     True,
     "Gemini via Google's API. Needs GOOGLE_API_KEY."),
    ("venice",
     "Venice.ai (OpenAI-compatible)",
     "zai-org-glm-5",
     "VENICE_API_KEY",
     "venice",
     True,
     "Venice.ai — OpenAI-compatible. Needs VENICE_API_KEY."),
    ("openrouter",
     "OpenRouter (multi-model proxy, OpenAI-compatible)",
     "anthropic/claude-3.5-sonnet",
     "OPENROUTER_API_KEY",
     "openrouter",
     True,
     "OpenRouter — gateway to many providers under one key. "
     "Use publisher/model strings like `anthropic/claude-3.5-sonnet` "
     "or `meta-llama/llama-3.1-70b-instruct`. Needs OPENROUTER_API_KEY."),
    ("claude-code",
     "Claude CLI — one-shot mode",
     "claude-opus-4-7",
     None,
     "claude-cli",
     True,
     "Uses the `claude` CLI's own login. No API key needed."),
    ("claude-code-agentic",
     "Claude CLI — agentic (tooled, sandboxed)",
     "claude-opus-4-7",
     None,
     "claude-agentic",
     True,
     "Agentic Claude CLI — more capable, 5-20x more tokens. Subscription only."),
    ("codex-agentic",
     "Codex CLI — agentic",
     "",
     None,
     "codex-agentic",
     True,
     "Agentic Codex CLI — uses `codex login`. Subscription only."),
]


# ── Agent name generator ──────────────────────────────────────────


_ADJECTIVES = [
    "amber", "arctic", "bold", "brave", "breezy", "bright", "cosmic",
    "crimson", "curious", "daring", "dapper", "dizzy", "dusty", "eager",
    "electric", "feisty", "fluffy", "frosty", "fuzzy", "gentle", "giddy",
    "glowing", "golden", "grumpy", "happy", "humble", "jazzy", "jolly",
    "lively", "loyal", "lucky", "mellow", "merry", "mighty", "misty",
    "moody", "noble", "nimble", "perky", "plucky", "quiet", "quirky",
    "rapid", "rascal", "rusty", "sassy", "scrappy", "shiny", "silly",
    "silver", "sleek", "smug", "snappy", "sneaky", "sparkly", "speedy",
    "spicy", "stormy", "sturdy", "sunny", "swift", "tipsy", "tricky",
    "vivid", "witty", "wobbly", "zany", "zesty",
]

_NOUNS = [
    "axolotl", "badger", "beetle", "buffalo", "capybara", "cheetah",
    "chinchilla", "coyote", "dingo", "dolphin", "ferret", "fox", "gecko",
    "gibbon", "giraffe", "goose", "gopher", "hamster", "hedgehog", "heron",
    "hippo", "ibex", "iguana", "jackal", "jaguar", "kestrel", "koala",
    "kraken", "lemur", "lynx", "macaw", "magpie", "manatee", "meerkat",
    "mongoose", "moose", "narwhal", "newt", "ocelot", "octopus", "okapi",
    "opossum", "orca", "osprey", "otter", "panda", "pangolin", "panther",
    "parrot", "pelican", "penguin", "platypus", "puffin", "quokka", "raccoon",
    "raven", "salamander", "seal", "skunk", "sloth", "stoat", "tapir",
    "toucan", "turtle", "viper", "walrus", "weasel", "wombat", "yak", "zebra",
]


def _generate_agent_names(count: int, rng: random.Random | None = None) -> list[str]:
    """Return `count` unique <adjective>-<noun> names.

    Falls back to numeric suffixes (foo-1, foo-2) if asked for more names than
    the adjective×noun combinations can produce uniquely — practically only
    matters if someone asks for thousands of agents."""
    rng = rng or random.Random()
    capacity = len(_ADJECTIVES) * len(_NOUNS)
    if count <= capacity:
        seen: set[str] = set()
        names: list[str] = []
        while len(names) < count:
            n = f"{rng.choice(_ADJECTIVES)}-{rng.choice(_NOUNS)}"
            if n in seen:
                continue
            seen.add(n)
            names.append(n)
        return names
    # Pathological large count: keep names unique via numeric suffixes.
    base = [f"{rng.choice(_ADJECTIVES)}-{rng.choice(_NOUNS)}" for _ in range(count)]
    return [f"{name}-{i + 1}" for i, name in enumerate(base)]


# ── Input helpers ──────────────────────────────────────────────────


def _prompt(label: str, default: str | None = None, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            sys.exit("\naborted")
        if raw:
            return raw
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("    (required)")


def _prompt_choice(label: str, choices: list[tuple[str, str]], default_idx: int = 0) -> str:
    """Show a numbered menu and return the selected key."""
    print(f"\n  {label}")
    for i, (_, blurb) in enumerate(choices, 1):
        marker = " (default)" if i - 1 == default_idx else ""
        print(f"    {i}) {blurb}{marker}")
    while True:
        try:
            raw = input(f"  choose [1-{len(choices)}, default {default_idx + 1}]: ").strip()
        except EOFError:
            sys.exit("\naborted")
        if not raw:
            return choices[default_idx][0]
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][0]
        print("    (invalid)")


def _prompt_int(label: str, default: int, minimum: int = 1) -> int:
    while True:
        raw = _prompt(label, default=str(default))
        if raw.isdigit() and int(raw) >= minimum:
            return int(raw)
        print(f"    (must be an integer >= {minimum})")


def _prompt_yes_no(label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"  {label} [{suffix}]: ").strip().lower()
        except EOFError:
            sys.exit("\naborted")
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("    (answer y or n)")


# ── Host connection paste ─────────────────────────────────────────


_HOST_FIELDS = ("server_url", "username", "swarm_password")


def _parse_host_paste(text: str) -> dict[str, str]:
    """Extract server_url / username / swarm_password from a pasted blob.

    Tolerates the JSON-style snippet that `setup.py invite` produces
    (`"server_url": "https://…",`) as well as bare `key: value` or
    `key=value` forms. Unknown keys and surrounding braces are ignored."""
    found: dict[str, str] = {}
    for key in _HOST_FIELDS:
        # Quoted JSON form first ("key": "value"), then loose form.
        m = re.search(rf'["\']?{key}["\']?\s*[:=]\s*"([^"]+)"', text)
        if not m:
            m = re.search(rf'\b{key}\s*[:=]\s*([^\s,}}]+)', text)
        if m:
            found[key] = m.group(1).strip().strip(",").strip('"').strip("'")
    return found


def _read_paste_block() -> str:
    """Read lines until the user enters a blank line. Empty result == skip."""
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            return ""
        lines.append(line)
    return "\n".join(lines)


def _prompt_host_connection(
    defaults: dict | None = None,
) -> tuple[str, str, str]:
    """Collect `server_url`, `username`, `swarm_password`. When `defaults`
    contains all three (re-runs against an existing fleet.config.json),
    offer to keep them as-is so the user doesn't have to paste / retype.
    On 'no' (or partial / missing defaults), fall through to the paste-
    block flow, with whatever defaults we have wired into the per-field
    prompts as fallbacks."""
    defaults = defaults or {}
    have_all = all(
        defaults.get(k) for k in ("server_url", "username", "swarm_password")
    )
    if have_all:
        pw = defaults["swarm_password"]
        pw_shown = pw[:6] + "…" + pw[-4:] if len(pw) > 12 else "set"
        print("Existing connection settings:")
        print(f"  server_url     = {defaults['server_url']}")
        print(f"  username       = {defaults['username']}")
        print(f"  swarm_password = {pw_shown}")
        print()
        if _prompt_yes_no(
            "Keep these connection settings?", default=True,
        ):
            return (
                defaults["server_url"],
                defaults["username"],
                defaults["swarm_password"],
            )

    print("Paste the three lines your swarm host sent you. Example:")
    print('    "server_url": "https://my-swarm.up.railway.app",')
    print('    "username": "your-name",')
    print('    "swarm_password": "abc123…",')
    print()
    print("When you're done pasting, press Enter for a blank line, then:")
    print("    Mac / Linux:  Ctrl-D")
    print("    Windows:      Ctrl-Z then Enter")
    print("Or just press Enter now to type each value separately.")
    print()
    pasted = _read_paste_block()
    parsed = _parse_host_paste(pasted) if pasted else {}

    if pasted and not parsed:
        print("  (couldn't read any values from that paste — type them in below)")
    elif parsed:
        for key in _HOST_FIELDS:
            if key in parsed:
                print(f"  ✓ {key} = {parsed[key]}")

    server_url = parsed.get("server_url") or _prompt(
        "server_url", default=defaults.get("server_url") or None,
    )
    username = parsed.get("username") or _prompt(
        "username", default=defaults.get("username") or None,
    )
    swarm_password = parsed.get("swarm_password") or _prompt(
        "swarm_password", default=defaults.get("swarm_password") or None,
    )
    return server_url, username, swarm_password


# ── Provider selection ────────────────────────────────────────────


def _select_provider() -> tuple[str, str, str | None, str, bool]:
    choices = [(p[0], f"{p[1]} — {p[6]}") for p in PROVIDERS]
    default_idx = next(
        i for i, p in enumerate(PROVIDERS) if p[0] == "claude-code"
    )
    key = _prompt_choice("Which LLM provider?", choices, default_idx=default_idx)
    spec = next(p for p in PROVIDERS if p[0] == key)
    return spec[0], spec[2], spec[3], spec[4], spec[5]


# ── Main flow ─────────────────────────────────────────────────────


def _confirm_overwrite(force: bool) -> None:
    if not FLEET_CONFIG_PATH.exists():
        return
    if force:
        print(f"  --force: overwriting {FLEET_CONFIG_PATH.name}")
        return
    if not _prompt_yes_no(
        f"{FLEET_CONFIG_PATH.name} already exists. Overwrite?",
        default=False,
    ):
        sys.exit("aborted — existing fleet.config.json kept")


def _build_agent(
    name: str,
    provider: str,
    model: str,
    api_key_env: str | None,
    compute: str,
    hardware: str | None,
) -> dict:
    entry: dict = {
        "name": name,
        "provider": provider,
    }
    if model:
        entry["model"] = model
    if api_key_env:
        entry["api_key_env"] = api_key_env
    entry["compute"] = compute
    if compute == "c3" and hardware:
        entry["hardware"] = hardware
    return entry


def run_wizard(force: bool = False) -> int:
    print("\nfleet.config.json wizard")
    print("─" * 40)
    print("Answer a few questions to generate fleet.config.json.")
    print("Press Ctrl-C at any time to abort.\n")

    # Read the existing config (if any) BEFORE _confirm_overwrite so we
    # can carry forward the swarm-connection triplet without forcing the
    # user to paste / retype it on every wizard re-run.
    existing: dict = {}
    if FLEET_CONFIG_PATH.exists():
        try:
            existing = json.loads(FLEET_CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    _confirm_overwrite(force)

    print("\nSwarm connection")
    print("─" * 40)
    server_url, username, swarm_password = _prompt_host_connection(existing)

    print("\nLLM provider")
    print("─" * 40)
    print("Which LLM should your agents call?")
    provider, default_model, api_key_env, name_stub, supports_c3 = _select_provider()

    if default_model:
        model = _prompt("model (press Enter for default)", default=default_model)
    else:
        model = _prompt("model", allow_empty=True)

    print()
    count = _prompt_int("How many agents to run in parallel?", default=1)

    # Benchmarks always run in local Docker for now. Hand-edit fleet.config.json
    # to switch to remote backends like `c3`.
    compute = "local"
    hardware: str | None = None

    names = _generate_agent_names(count)
    config = {
        "server_url": server_url,
        "username": username,
        "swarm_password": swarm_password,
        "agents": [
            _build_agent(name, provider, model, api_key_env, compute, hardware)
            for name in names
        ],
    }

    FLEET_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")

    names_str = ", ".join(a["name"] for a in config["agents"])
    print(
        f"\n  wrote {FLEET_CONFIG_PATH.relative_to(ROOT)} — "
        f"{count} agent(s): {names_str}"
    )
    if api_key_env and not os.environ.get(api_key_env, "").strip():
        print(f"  reminder: export {api_key_env}=<your-key> before launching")
    print()
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing fleet.config.json without asking.")
    return p.parse_args()


_DOCKER_INSTALL_URL = "https://www.docker.com/products/docker-desktop/"


def _preflight_docker() -> None:
    """Fail before the wizard if Docker isn't installed.

    Benchmarks always run in a local Docker container (see
    scripts/benchmark.py). benchmark.py's _ensure_docker_daemon() already
    auto-launches Docker Desktop / OrbStack at fleet time if the daemon
    is stopped, so we only catch the one case it can't recover from:
    `docker` not on PATH at all. Without this, a contributor only finds
    out Docker is missing after picking a provider, exporting an API
    key, and hitting an opaque FileNotFoundError on iteration 1.
    """
    if shutil.which("docker") is None:
        sys.exit(
            "Docker is required to run benchmarks but `docker` was not found "
            "on PATH.\n"
            f"Install Docker Desktop from {_DOCKER_INSTALL_URL}, then re-run "
            "`python scripts/init_fleet.py`."
        )


def main() -> int:
    if not EXAMPLE_PATH.exists():
        sys.exit(
            f"{EXAMPLE_PATH.name} not found at {EXAMPLE_PATH}. "
            "Are you running this from the repo root?"
        )
    _preflight_docker()
    args = parse_args()
    try:
        return run_wizard(force=args.force)
    except KeyboardInterrupt:
        print("\naborted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
