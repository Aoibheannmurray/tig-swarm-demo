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

# Reuse the server's single source of truth for model tiering so the wizard's
# `detailed_prompts` default can't drift from the seeding logic. server/tiers.py
# is a pure module (constants + functions, no server imports), so importing it
# client-side is safe.
sys.path.insert(0, str(ROOT / "server"))
import tiers  # noqa: E402

# Flat scripts/ import (see scripts/CLAUDE.md) — one definition of "is this
# endpoint on my own machine?", shared with run_fleet/run_loop so the wizard's
# advice and the launcher's key handling can't disagree.
from llm_backends import is_local_api_base  # noqa: E402

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


# OpenRouter's OpenAI-compatible endpoint. Mirrors OPENROUTER_API_BASE in
# scripts/llm_backends.py; the wizard writes it as an explicit api_base.
_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

# DeepSeek's OpenAI-compatible endpoint. Like OpenRouter, the wizard writes it
# as provider `openai` with an explicit api_base.
_DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"

# `custom` is not a vendor — it is "whatever OpenAI-compatible endpoint you
# point me at": a local llama.cpp / vLLM / Ollama / LM Studio server, or a
# private gateway. Nothing about it can be defaulted, so the contributor
# supplies all three moving parts (model id, api_base, key env var) and
# build_fleet_config writes them through as provider `openai` — the same remap
# OpenRouter and DeepSeek get, just with a URL we don't know in advance.
_CUSTOM_API_KEY_ENV = "CUSTOM_LLM_API_KEY"

# Providers whose endpoint URL the contributor supplies. Surfaced by
# get_providers() as `needs_api_base` so the setup UI knows to ask for one
# rather than hardcoding a provider key.
NEEDS_API_BASE = frozenset({"custom"})

# Setup-level provider key → what actually lands in fleet.config.json.
# OpenRouter / DeepSeek / a custom endpoint are all OpenAI-compatible, so they
# are written as provider `openai` plus an api_base; only the fixed-endpoint
# ones can name the URL here (custom's comes from the contributor).
#
# Surfaced through get_providers() as `wire_provider` / `api_base` because the
# setup keys are NOT valid config values: run_fleet exits with "unknown
# provider 'deepseek'" if one is written straight into fleet.config.json. Any
# UI that edits the config directly has to apply this same mapping — and
# recognise it in reverse to tell which vendor an `openai` entry really is.
_WIRE_REMAP: dict[str, tuple[str, str | None]] = {
    "openrouter": ("openai", _OPENROUTER_API_BASE),
    "deepseek": ("openai", _DEEPSEEK_API_BASE),
    "custom": ("openai", None),
}


def resolve_wire_provider(key: str) -> tuple[str, str | None]:
    """(provider, api_base) as written into fleet.config.json for a setup-level
    provider key. Unmapped keys pass through unchanged with no api_base."""
    return _WIRE_REMAP.get(key, (key, None))


def wire_providers() -> frozenset[str]:
    """Every provider value that may legally appear in fleet.config.json."""
    return frozenset(resolve_wire_provider(p[0])[0] for p in PROVIDERS)

# Keep in sync with DEFAULT_MODELS in scripts/llm_backends.py and the
# provider list in scripts/run_loop.py. Tuple: (label, default_model,
# api_key_env or None, short_name_stub, supports_c3, blurb, popular_models).
#
# Labels and blurbs are UI copy — they render in the setup app's provider
# dropdown, so they name ONE thing ("Claude API", not "Anthropic (Claude
# API)") and the OpenAI-compatible/api_base plumbing stays in the docs.
#
# The Claude CLI rows lead with `opus` / `sonnet` / `haiku`: `claude --model`
# documents these as "an alias for the latest model", so they follow each new
# release without anyone editing this table — the closest thing to a live
# catalog that CLI offers (it has no `models list` command, unlike Codex's
# `codex debug models`). The dated ids stay below them for pinning a version.
#
# `popular_models` is a short, hand-kept shortlist (default first) shown as the
# "Recommended" group in the setup app's model dropdown. It is NOT the full
# catalog: the UI fetches that live from the provider (llm_backends.list_models
# via /local-api/models). The shortlist is what we can offer before a key is
# saved or when a catalog fetch fails. Codex's installed CLI exposes a live
# catalog; Claude CLI providers still rely entirely on this shortlist.
PROVIDERS: list[tuple[str, str, str, str | None, str, bool, str, list[str]]] = [
    ("anthropic",
     "Claude API",
     "claude-opus-4-8",
     "ANTHROPIC_API_KEY",
     "claude",
     True,
     "Claude, called directly over Anthropic's API.",
     ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]),
    ("openai",
     "OpenAI API",
     "gpt-5",
     "OPENAI_API_KEY",
     "gpt",
     True,
     "GPT, called directly over OpenAI's API.",
     ["gpt-5", "gpt-5-mini", "o3"]),
    ("google",
     "Gemini API",
     "gemini-2.5-pro",
     "GEMINI_API_KEY",
     "gemini",
     True,
     "Gemini, called directly over Google's API.",
     ["gemini-2.5-pro", "gemini-2.5-flash"]),
    ("venice",
     "Venice.ai",
     "zai-org-glm-5",
     "VENICE_API_KEY",
     "venice",
     True,
     "Private, uncensored inference over an OpenAI-compatible API.",
     ["zai-org-glm-5", "qwen3-235b", "deepseek-r1-671b"]),
    ("openrouter",
     "OpenRouter",
     "qwen/qwen3-coder",
     "OPENROUTER_API_KEY",
     "openrouter",
     True,
     "One key, many providers. Models are `publisher/model` strings.",
     ["qwen/qwen3-coder", "anthropic/claude-opus-4.8",
      "deepseek/deepseek-chat", "moonshotai/kimi-k2"]),
    ("deepseek",
     "DeepSeek",
     "deepseek-v4-pro",
     "DEEPSEEK_API_KEY",
     "deepseek",
     True,
     "DeepSeek's own API — strong models at a low price.",
     ["deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"]),
    ("claude-code",
     "Claude CLI",
     "claude-opus-4-8",
     None,
     "claude-cli",
     True,
     "Uses the `claude` CLI's own login. No API key needed.",
     ["opus", "sonnet", "haiku",
      "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]),
    ("claude-code-agentic",
     "Claude CLI (agentic)",
     "claude-opus-4-8",
     None,
     "claude-agentic",
     True,
     "Tooled, sandboxed Claude CLI — more capable, 5-20x more tokens.",
     ["opus", "sonnet",
      "claude-opus-4-8", "claude-sonnet-5"]),
    ("codex-agentic",
     "Codex CLI (agentic)",
     "gpt-5.6-sol",
     None,
     "codex-agentic",
     True,
     "Agentic Codex CLI — uses `codex login`. Subscription only.",
     ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
      "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"]),
    # Last on purpose: it's the escape hatch, not a recommendation. No default
    # model and no shortlist — only the contributor's own server knows what it
    # serves, and the UI lists it live from api_base/v1/models.
    ("custom",
     "Custom / local LLM",
     "",
     _CUSTOM_API_KEY_ENV,
     "local-llm",
     True,
     "Your own OpenAI-compatible endpoint — llama.cpp, vLLM, Ollama, "
     "LM Studio, or a private gateway.",
     []),
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
    """Extract connection details from a pasted blob.

    Pulls the server_url / username / swarm_password triplet. Tolerates the
    JSON-style snippet that `setup.py invite` produces (`"server_url":
    "https://…",`) as well as bare `key: value` or `key=value` forms. Unknown
    keys and surrounding braces are ignored."""
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


def _mask_secret(value: str) -> str:
    """Show enough of a secret to recognize it without printing the whole thing."""
    return value[:6] + "…" + value[-4:] if len(value) > 12 else "set"


def _prompt_host_connection(
    defaults: dict | None = None,
) -> tuple[str, str, str]:
    """Collect `server_url`, `username`, and `swarm_password`. When `defaults`
    contains all three (re-runs against an existing fleet.config.json), offer to
    keep them as-is so the user doesn't have to paste / retype. On 'no' (or
    partial / missing defaults), fall through to the paste-block flow, with
    whatever defaults we have wired into the per-field prompts as fallbacks."""
    defaults = defaults or {}
    have_all = all(
        defaults.get(k) for k in ("server_url", "username", "swarm_password")
    )
    if have_all:
        print("Existing connection settings:")
        print(f"  server_url     = {defaults['server_url']}")
        print(f"  username       = {defaults['username']}")
        print(f"  swarm_password = {_mask_secret(defaults['swarm_password'])}")
        print()
        if _prompt_yes_no(
            "Keep these connection settings?", default=True,
        ):
            return (
                defaults["server_url"],
                defaults["username"],
                defaults["swarm_password"],
            )

    print("Paste the lines your swarm host sent you. Example:")
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


# ── Compute backend selection ─────────────────────────────────────


# C3 hardware choices offered in the wizard. The C3 backend forwards explicit
# profile picks verbatim, while `auto` chooses CPU hardware for CPU challenges
# and GPU hardware for GPU challenges at benchmark time.
_C3_HARDWARE_CHOICES = [
    ("auto", "Auto (CPU challenges: cpu-d3-4vcpu-16gb; GPU challenges: NVIDIA L40)"),
    ("cpu-d3-4vcpu-16gb", "CPU: AMD EPYC Genoa 4 vCPU / 16 GiB"),
    ("cpu-e2-4vcpu-16gb", "CPU: Intel Ice Lake 4 vCPU / 16 GiB"),
    ("cpu-e2-48vcpu-192gb", "CPU: Intel Ice Lake 48 vCPU / 192 GiB"),
    ("cpu-d3-96vcpu-384gb", "CPU: AMD EPYC Genoa 96 vCPU / 384 GiB"),
    ("l40", "NVIDIA L40"),
    ("h100", "NVIDIA H100"),
]

_C3_INSTALL_URL = "https://cthree.cloud/install.sh"

# Hardware keys accepted by build_fleet_config() — the choice keys above.
_C3_HARDWARE_KEYS = {key for key, _ in _C3_HARDWARE_CHOICES}


def _select_compute(supports_c3: bool) -> tuple[str, str | None]:
    """Pick where each benchmark runs.

    GPU swarm agents default to C3 cloud GPUs; CPU-only or offline setups can
    pick local Docker. Providers that can't run on C3 skip the question and
    stay local. Returns ``(compute, c3_hardware)`` — the hardware is ``None``
    for local compute. The C3 API key is collected separately (only when C3 is
    chosen) by :func:`_prompt_c3_api_key`."""
    if not supports_c3:
        return "local", None

    print("\nCompute backend")
    print("─" * 40)
    choice = _prompt_choice(
        "Where should each benchmark run?",
        [
            ("c3",
             "C3 cloud hardware — runs benchmarks remotely "
             "(needs the c3 CLI + an API key)"),
            ("local",
             "Local Docker — runs benchmarks on this machine"),
        ],
        default_idx=0,
    )
    if choice == "local":
        return "local", None

    if shutil.which("c3") is None:
        print(
            f"\n  note: the `c3` CLI isn't on PATH yet — install it from "
            f"{_C3_INSTALL_URL}\n  before launching the fleet (the config is "
            "still written either way)."
        )
    else:
        print(
            "\n  C3 needs you to be logged in (`c3 login`) with an API key "
            "created\n  (`c3 apikey create tig-swarm`). The next prompt "
            "handles the key."
        )

    hardware = _prompt_choice("Which C3 hardware?", _C3_HARDWARE_CHOICES, default_idx=0)
    return "c3", hardware


def _prompt_c3_api_key(existing_key: str | None = None) -> str | None:
    """Collect the C3 API key in its own wizard section. Only called when the
    user picked C3 compute.

    Preference order, smoothest first:

      1. ``existing_key`` (a re-run carrying a key forward from
         fleet.config.json) — offer to keep it.
      2. An exported ``C3_API_KEY`` — detected here (cheap env read, no
         subprocess) and used automatically, so there's nothing to paste. We
         return ``None`` in this case so the key stays in the environment
         rather than being copied into fleet.config.json.
      3. A pasted key — stored in fleet.config.json.

    Returns ``None`` when nothing is pasted, so C3 falls back to the
    ``C3_API_KEY`` env var or an existing ``c3 login`` session at launch."""
    print("\nC3 API key")
    print("─" * 40)
    if existing_key and _prompt_yes_no(
        f"Keep the existing C3 API key ({_mask_secret(existing_key)})?",
        default=True,
    ):
        return existing_key

    env_key = os.environ.get("C3_API_KEY", "").strip()
    if env_key:
        print(f"  ✓ Found C3_API_KEY in your environment ({_mask_secret(env_key)}).")
        print("    Your agents will use it automatically — nothing to paste.")
        return None

    print("No C3_API_KEY found in your environment. Recommended: create a key")
    print("and export it before launching —")
    if os.name == "nt":
        print("    c3 apikey create tig-swarm")
        print('    set C3_API_KEY=<your-key>   (cmd)   or'
              '   $env:C3_API_KEY="<your-key>"   (PowerShell)')
    else:
        print("    c3 apikey create tig-swarm")
        print("    export C3_API_KEY=<your-key>")
    print("Or paste the key here to store it in fleet.config.json. Leave blank")
    print("to use `c3 login` / set C3_API_KEY later, then re-run the wizard.")
    return _prompt("c3 API key (press Enter to skip)", allow_empty=True).strip() or None


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
    api_base: str | None = None,
    role: str | None = None,
    seeded_start: bool | None = None,
) -> dict:
    entry: dict = {
        "name": name,
        "provider": provider,
    }
    if model:
        entry["model"] = model
    if api_key_env:
        entry["api_key_env"] = api_key_env
    if api_base:
        entry["api_base"] = api_base
    entry["compute"] = compute
    if compute == "c3" and hardware:
        entry["hardware"] = hardware
    # Standard-tier (smaller/cheaper) models get the stricter, more prescriptive
    # Rust prompt by default — their raw output tends not to compile. Keyed off
    # the MODEL via classify_tier, not the provider: OpenRouter is a multi-model
    # gateway that carries both tiny and frontier models, so the provider alone
    # says nothing about capability. Frontier models are left without the flag
    # (they don't need the verbosity). Contributors can override either way by
    # editing the flag in fleet.config.json.
    tier = tiers.classify_tier(provider, model)
    if tier == "standard":
        entry["detailed_prompts"] = True
    # Default role from tier: frontier → explorer (ambitious rewrites, fills the
    # seed pool), standard → exploiter (localized search/replace edits + HPO).
    # An explicit setup pick wins; either way role stays contributor-owned and
    # hot-reloads via fleet.config.json.
    entry["role"] = role if role in ("explorer", "exploiter") else tiers.role_for_tier(tier)
    # Optional seeding override from setup: True = fresh trajectories start
    # from working code (server seed pool → best peer → stub fallback),
    # False = always the bare stub. Omitted = the server's tier/role/GPU
    # auto policy decides.
    if seeded_start is not None:
        entry["seeded_start"] = bool(seeded_start)
    return entry


# ── Non-interactive cores (shared by the wizard and the control-ui) ──
#
# The interactive wizard (`run_wizard`) collects answers via input() prompts;
# the local companion UI collects the same answers over HTTP. Both funnel into
# these pure functions so the config-shaping logic (provider remap, tier-based
# role / detailed_prompts, C3 handling) lives in exactly one place.


def get_providers() -> list[dict]:
    """The provider table as JSON-serializable data (for the local-api / UI)."""
    return [
        {
            "key": p[0],
            "label": p[1],
            "default_model": p[2],
            "api_key_env": p[3],
            "name_stub": p[4],
            "supports_c3": p[5],
            "blurb": p[6],
            # The UI asks for an endpoint URL (and lists models from it)
            # instead of showing a vendor's catalog.
            "needs_api_base": p[0] in NEEDS_API_BASE,
            # What this choice becomes in fleet.config.json. A UI that edits
            # the config directly must write these — not `key` — or the fleet
            # dies at launch with "unknown provider"; reading them backwards is
            # also how it tells a DeepSeek agent from a plain OpenAI one.
            "wire_provider": resolve_wire_provider(p[0])[0],
            "api_base": resolve_wire_provider(p[0])[1],
            # Shortlist for the UI's "Recommended" group. The full catalog is
            # fetched live per provider (see /local-api/models).
            "popular_models": list(p[7]),
        }
        for p in PROVIDERS
    ]


def get_c3_hardware_choices() -> list[dict]:
    """C3 hardware options as JSON-serializable data (for the local-api / UI)."""
    return [{"key": key, "label": label} for key, label in _C3_HARDWARE_CHOICES]


def build_fleet_config(params: dict) -> dict:
    """Build a fleet.config.json dict from a params mapping — no I/O, no prompts.

    Shared by the interactive wizard and the local companion UI. Applies the
    same OpenRouter/DeepSeek → `openai` + api_base remap and the same tier-based
    role / detailed_prompts defaults (`_build_agent`) as the wizard, so the two
    entry points can never drift.

    `params` keys: server_url, username, swarm_password (all required);
    provider (a key from get_providers, may be openrouter/deepseek); model
    (optional — falls back to the provider default); either `names` (explicit
    list) or `count` + optional `prefix`; compute (local|c3); hardware
    (optional C3 profile); c3_api_key (optional, stored only for C3);
    api_base + api_key_env (required / optional respectively for the `custom`
    provider, ignored otherwise)."""
    server_url = (params.get("server_url") or "").strip()
    username = (params.get("username") or "").strip()
    swarm_password = (params.get("swarm_password") or "").strip()
    if not (server_url and username and swarm_password):
        raise ValueError("server_url, username and swarm_password are all required")

    provider = params.get("provider") or "claude-code"
    spec = next((p for p in PROVIDERS if p[0] == provider), None)
    if spec is None:
        raise ValueError(f"unknown provider: {provider!r}")
    default_model, api_key_env, supports_c3 = spec[2], spec[3], spec[5]

    # OpenRouter / DeepSeek are OpenAI-compatible: written as provider `openai`
    # with an explicit api_base (see _WIRE_REMAP, which the config editor reads
    # through get_providers() so both paths write the same shape).
    is_custom = provider == "custom"
    provider, api_base = resolve_wire_provider(provider)
    if is_custom:
        # Same remap, but every part comes from the contributor: we have no
        # endpoint to default to, and the key env var is theirs to name (their
        # server may want a token, a placeholder, or nothing at all).
        api_base = (params.get("api_base") or "").strip()
        if not api_base:
            raise ValueError(
                "api_base is required for a custom provider — the URL of your "
                "OpenAI-compatible endpoint, e.g. http://127.0.0.1:8000/v1"
            )
        if not re.match(r"https?://", api_base, re.IGNORECASE):
            raise ValueError(
                f"api_base must be an http:// or https:// URL, got {api_base!r}"
            )
        api_key_env = (params.get("api_key_env") or "").strip() or _CUSTOM_API_KEY_ENV

    model = (params.get("model") or "").strip() or (default_model or "")
    if is_custom and not model:
        raise ValueError(
            "model is required for a custom provider — the id your endpoint "
            "serves, e.g. Qwen3-Coder-Next-Q8_0"
        )

    names = params.get("names")
    if names:
        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            raise ValueError("names, if given, must be non-empty")
    else:
        count = int(params.get("count") or 1)
        if count < 1:
            raise ValueError("count must be >= 1")
        prefix = (params.get("prefix") or "").strip()
        names = (
            [f"{prefix}-{i}" for i in range(1, count + 1)]
            if prefix
            else _generate_agent_names(count)
        )

    # C3 by default: it needs no local Docker or Rust toolchain, which is the
    # smoothest path for a new contributor (the setup UI defaults the same way).
    # Only affects configs created here — a fleet.config.json with no `compute`
    # field still falls back to local at runtime, so existing fleets don't move.
    compute = params.get("compute") or "c3"
    if not supports_c3:
        compute = "local"
    if compute not in ("local", "c3"):
        raise ValueError(f"unknown compute backend: {compute!r}")
    hardware = params.get("hardware") if compute == "c3" else None
    if compute == "c3" and hardware and hardware not in _C3_HARDWARE_KEYS:
        raise ValueError(f"unknown C3 hardware: {hardware!r}")

    # Optional behavior picks from setup. `role`: explorer/exploiter (empty or
    # "auto" = tier default). `seeded_start`: "seed"/"stub"/bool (empty or
    # "auto" = server policy). Both stay hot-editable in fleet.config.json.
    role = (str(params.get("role") or "")).strip().lower() or None
    if role in ("", "auto"):
        role = None
    if role is not None and role not in ("explorer", "exploiter"):
        raise ValueError(f"unknown role: {role!r} (explorer or exploiter)")
    raw_seed = params.get("seeded_start")
    if isinstance(raw_seed, bool):
        seeded_start = raw_seed
    else:
        seed_text = str(raw_seed or "").strip().lower()
        if seed_text in ("", "auto"):
            seeded_start = None
        elif seed_text in ("seed", "true"):
            seeded_start = True
        elif seed_text in ("stub", "false"):
            seeded_start = False
        else:
            raise ValueError(f"unknown seeded_start: {raw_seed!r} (seed, stub, or auto)")

    config: dict = {
        "server_url": server_url,
        "username": username,
        "swarm_password": swarm_password,
    }
    c3_api_key = (params.get("c3_api_key") or "").strip() or None
    if compute == "c3" and c3_api_key:
        config["c3_api_key"] = c3_api_key
    config["agents"] = [
        _build_agent(name, provider, model, api_key_env, compute, hardware,
                     api_base=api_base, role=role, seeded_start=seeded_start)
        for name in names
    ]
    return config


def write_fleet_config(config: dict, path: Path | None = None) -> Path:
    """Serialize a fleet config dict to disk (default: root fleet.config.json)."""
    dest = path or FLEET_CONFIG_PATH
    dest.write_text(json.dumps(config, indent=2) + "\n")
    return dest


def run_wizard(force: bool = False) -> int:
    print("\nfleet.config.json wizard")
    print("─" * 40)
    print("Answer a few questions to generate fleet.config.json, then the")
    print("fleet launches. Press Enter at any prompt to accept the default")
    print("shown in [brackets]; press Ctrl-C to abort.")
    print()
    print("This is a GPU swarm — benchmarks run on C3 cloud GPUs. Before you")
    print("start, you should already have:")
    print("  • run `c3 login` and `c3 apikey create tig-swarm`")
    print("  • exported C3_API_KEY and your provider API key in this shell")
    print("Forgot one? Finish the wizard anyway, export the key, then re-run —")
    print("your previous answers come back as the defaults, so you can press")
    print("Enter straight through.\n")

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
    # Keep the raw provider key (openrouter/deepseek included) — build_fleet_config
    # does the OpenAI-compatible remap, so it lives in exactly one place.
    provider, default_model, api_key_env, name_stub, supports_c3 = _select_provider()

    # A custom endpoint has nothing to default: ask for the URL first, since
    # it's what makes the model id meaningful.
    api_base = None
    if provider == "custom":
        print()
        print("  Your endpoint must speak the OpenAI chat-completions API.")
        api_base = _prompt("api_base", default="http://127.0.0.1:8000/v1")
        api_key_env = _prompt(
            "env var holding its API key (any name; leave the default if "
            "your server checks no key)",
            default=_CUSTOM_API_KEY_ENV,
        )

    if default_model:
        model = _prompt("model (press Enter for default)", default=default_model)
    elif provider == "custom":
        model = _prompt("model (the id your endpoint serves)")
    else:
        model = _prompt("model", allow_empty=True)

    print()
    count = _prompt_int("How many agents to run in parallel?", default=1)

    # Agent name. Auto-generated <adjective>-<noun> names are the default, but
    # contributors often want a recognisable name (matching the swarm they're
    # joining). For a single agent the prompt offers the generated name as the
    # default; for several it takes an optional prefix and numbers them
    # (foo-1, foo-2, …). Pressing Enter keeps the auto-generated names.
    generated = _generate_agent_names(count)
    if count == 1:
        names = [_prompt("agent name (press Enter for default)", default=generated[0])]
    else:
        prefix = _prompt(
            "agent name prefix (press Enter for auto-generated names)",
            allow_empty=True,
        )
        names = [f"{prefix}-{i}" for i in range(1, count + 1)] if prefix else generated

    # Where each benchmark runs. GPU swarm agents default to C3 cloud GPUs;
    # local Docker stays one keystroke away. The C3 API key is gathered in its
    # own section right after, only when C3 is chosen. c3_api_key (when
    # supplied) is a fleet-wide default — run_fleet.py copies it onto every
    # agent that lacks its own.
    compute, hardware = _select_compute(supports_c3)
    c3_api_key = (
        _prompt_c3_api_key(existing.get("c3_api_key"))
        if compute == "c3"
        else None
    )

    # Behavior picks. Both are hot-editable in fleet.config.json afterward
    # (`role` / `seeded_start`); "auto" defers to tier/server defaults.
    print()
    role = _prompt_choice("Agent role?", [
        ("auto", "auto — pick by model tier (recommended)"),
        ("explorer", "explorer — writes novel, ambitious algorithms"),
        ("exploiter", "exploiter — small focused edits to working code"),
    ], default_idx=0)
    seeding = _prompt_choice("Starting point for fresh trajectories?", [
        ("auto", "auto — server decides (recommended)"),
        ("seed", "seed — start from working code"),
        ("stub", "stub — start from scratch"),
    ], default_idx=0)

    config = build_fleet_config({
        "server_url": server_url,
        "username": username,
        "swarm_password": swarm_password,
        "provider": provider,   # raw key; remap happens inside build_fleet_config
        "model": model,
        "api_base": api_base,
        "api_key_env": api_key_env if provider == "custom" else None,
        "names": names,
        "compute": compute,
        "hardware": hardware,
        "c3_api_key": c3_api_key,
        "role": role,
        "seeded_start": seeding,
    })
    write_fleet_config(config)

    names_str = ", ".join(a["name"] for a in config["agents"])
    compute_desc = f"c3/{hardware}" if compute == "c3" else compute
    print(
        f"\n  wrote {FLEET_CONFIG_PATH.relative_to(ROOT)} — "
        f"{count} agent(s): {names_str} — compute: {compute_desc}"
    )
    # C3 without a stored key falls back to C3_API_KEY / `c3 login`; remind the
    # user so the first benchmark doesn't fail authentication mid-run.
    if compute == "c3" and not c3_api_key and not os.environ.get("C3_API_KEY", "").strip():
        if os.name == "nt":
            setline = ('set C3_API_KEY=<your-key>   (cmd)   or   '
                       '$env:C3_API_KEY="<your-key>"   (PowerShell)')
        else:
            setline = "export C3_API_KEY=<your-key>"
        print(
            f"  reminder: run `c3 login`, or {setline} before launching"
        )
    # A self-hosted endpoint usually authenticates nothing, so telling its
    # owner to export a key would be inventing a requirement they don't have.
    if (api_key_env and not os.environ.get(api_key_env, "").strip()
            and not is_local_api_base(api_base)):
        # Windows `cmd`/PowerShell don't understand `export`; print the
        # platform-correct set-the-env-var command so it can be pasted as-is.
        if os.name == "nt":
            setline = (f"set {api_key_env}=<your-key>   (cmd)   or   "
                       f"$env:{api_key_env}=\"<your-key>\"   (PowerShell)")
        else:
            setline = f"export {api_key_env}=<your-key>"
        print(f"  reminder: {setline} before launching")
    print()
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing fleet.config.json without asking.")
    return p.parse_args()


def main() -> int:
    if not EXAMPLE_PATH.exists():
        sys.exit(
            f"{EXAMPLE_PATH.name} not found at {EXAMPLE_PATH}. "
            "Are you running this from the repo root?"
        )
    args = parse_args()
    try:
        return run_wizard(force=args.force)
    except KeyboardInterrupt:
        print("\naborted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
