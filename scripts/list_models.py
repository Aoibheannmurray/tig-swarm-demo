#!/usr/bin/env python3
"""List the models a provider exposes — so you know what to put in the
`model` field of fleet.config.json.

Consult this before (or alongside) the wizard / hand-editing your config:

    python scripts/list_models.py                 # providers + their defaults
    python scripts/list_models.py anthropic       # Anthropic's live model list
    python scripts/list_models.py openrouter      # OpenRouter (no key needed)
    python scripts/list_models.py openai --api-base https://api.groq.com/openai/v1

It queries the provider's models endpoint live, so the result is whatever
your account actually has access to today — not a list that goes stale. Reads
the API key from the provider's standard env var (override with --api-key-env).
"""

from __future__ import annotations

# Python-version preflight — mirrors the other scripts so contributors on old
# Python get a clear pointer instead of a downstream TypeError.
import sys
if sys.version_info < (3, 9):
    sys.stderr.write(
        "TIG swarm scripts require Python 3.9 or newer. You're running %d.%d.%d.\n"
        "Install a current Python from https://www.python.org/downloads/ and re-run.\n"
        % sys.version_info[:3]
    )
    sys.exit(1)

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Force UTF-8 so the * marker / box glyphs don't crash a non-UTF-8 Windows
# console. No-op on Linux/macOS. Mirrors init_fleet.py / run_fleet.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from llm_backends import list_models
# Single source of truth for provider keys / defaults / key env vars — the
# same table the wizard offers. PROVIDERS tuple:
# (key, label, default_model, api_key_env, stub, supports_c3, blurb).
from init_fleet import PROVIDERS

_META = {p[0]: {"label": p[1], "default": p[2], "env": p[3]} for p in PROVIDERS}

# Where to browse the full catalog when a key isn't set (or for richer detail
# than the bare IDs — pricing, context windows, deprecation dates).
_DOCS = {
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/models",
    "openai": "https://platform.openai.com/docs/models",
    "google": "https://ai.google.dev/gemini-api/docs/models",
    "venice": "https://docs.venice.ai/overview/models",
    "openrouter": "https://openrouter.ai/models",
}

# OpenRouter's catalog is public, so a key is optional there.
_KEY_OPTIONAL = {"openrouter"}


def _set_cmd(env_var: str) -> str:
    """Platform-correct 'set this env var' instruction."""
    if os.name == "nt":
        return (f"set {env_var}=<your-key>   (cmd)   or   "
                f"$env:{env_var}=\"<your-key>\"   (PowerShell)")
    return f"export {env_var}=<your-key>"


def _env_note(key: str, meta: dict) -> str:
    env = meta["env"]
    if env is None:
        return "CLI login — no HTTP model list (the CLI picks the model)"
    if key in _KEY_OPTIONAL:
        return f"{env} (optional)"
    return f"needs {env}"


def _print_providers() -> int:
    print("\nProviders you can list models for:\n")
    width = max(len(k) for k in _META)
    for key, meta in _META.items():
        default = meta["default"] or "(CLI default)"
        print(f"  {key:<{width}}  default: {default:<32}  {_env_note(key, meta)}")
    print(
        "\nRun  python scripts/list_models.py <provider>  to see one provider's "
        "live model list.\n"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="List a provider's available models for fleet.config.json.",
    )
    ap.add_argument(
        "provider", nargs="?",
        help="Provider key (e.g. anthropic, openai, google, venice, "
             "openrouter). Omit to list all providers and their defaults.",
    )
    ap.add_argument(
        "--api-base",
        help="Override the base URL — for an OpenAI-compatible gateway "
             "(Together, Groq, a local server, etc.) reached via the `openai` "
             "provider.",
    )
    ap.add_argument(
        "--api-key-env",
        help="Env var holding the API key (default: the provider's standard "
             "var, e.g. ANTHROPIC_API_KEY).",
    )
    args = ap.parse_args()

    if not args.provider:
        return _print_providers()

    provider = args.provider.lower()
    if provider not in _META:
        sys.exit(
            f"Unknown provider {provider!r}. Known providers: "
            f"{', '.join(_META)}."
        )

    meta = _META[provider]
    env_var = args.api_key_env or meta["env"]
    api_key = os.environ.get(env_var, "").strip() if env_var else ""

    # Friendly heads-up for API-key providers when the key isn't set — beats a
    # raw HTTP 401 from the endpoint. (CLI providers have env=None and are
    # handled by the ValueError branch below; OpenRouter needs no key.)
    if env_var and not api_key and provider not in _KEY_OPTIONAL:
        docs = _DOCS.get(provider)
        lines = [
            f"{env_var} is not set, so {provider}'s live model list can't be "
            "fetched.",
            f"  {_set_cmd(env_var)}",
        ]
        if docs:
            lines.append(f"  …or browse the catalog at {docs}")
        sys.exit("\n".join(lines))

    try:
        models = list_models(
            provider, api_key=api_key or None, api_base=args.api_base,
        )
    except ValueError as e:
        # CLI-auth provider — no endpoint to query.
        print(str(e))
        return 0
    except RuntimeError as e:
        docs = _DOCS.get(provider)
        extra = f"\n  Browse the full catalog at {docs}" if docs else ""
        sys.exit(f"Could not fetch models for {provider}: {e}{extra}")

    if not models:
        print(f"No models returned for {provider}.")
        return 0

    default = meta["default"]
    print(f"\n{len(models)} model(s) available for {provider} "
          f"(* = wizard default):\n")
    for m in models:
        marker = " *" if m == default else "  "
        print(f"  {marker} {m}")
    print(
        "\nPut one of these in the \"model\" field of fleet.config.json, or "
        "accept the wizard default.\n"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
