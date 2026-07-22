"""Local secret store for the contributor runner.

`secrets.local.json` at the repo root holds API keys by environment-variable
NAME (`{"ANTHROPIC_API_KEY": "sk-…", "C3_API_KEY": "c3_…"}`), so a contributor
never has to `export` anything before `python run.py`. The file is gitignored
and written `0600` (owner-only) on POSIX.

Precedence everywhere: a real environment variable wins over the file. That
keeps CI / power-user `export` flows working unchanged, and lets the file be a
convenience layer rather than an authority.

Stdlib only — this module is imported by run_fleet (subprocess launcher) and by
the --ui companion (control_server), which must not drag in extra deps.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = ROOT / "secrets.local.json"


# Older names we still honour when the canonical one yields nothing, so a
# rename can't silently kill a fleet that was working yesterday. Gemini's key
# was `GOOGLE_API_KEY` until the provider was renamed to "Gemini API"; Google's
# own SDKs accept either name, so `GEMINI_API_KEY` is canonical here.
LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "GEMINI_API_KEY": ("GOOGLE_API_KEY",),
}


def _names_for(name: str) -> tuple[str, ...]:
    """`name` first, then any legacy spelling it replaced."""
    return (name, *LEGACY_ALIASES.get(name, ()))


def load_secrets() -> dict[str, str]:
    """The stored name→value map, or {} when the file is absent/unreadable.
    Never raises: a corrupt file degrades to "no stored secrets" rather than
    breaking every launch."""
    if not SECRETS_PATH.exists():
        return {}
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce to str→str, dropping anything malformed.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}


def resolve(name: str) -> str | None:
    """The effective value for env-var `name`: a set process environment
    variable wins, then the stored file, else None.

    Legacy aliases (see LEGACY_ALIASES) are searched WITHIN each source, not
    after both: environment (canonical, then legacy), then file (canonical,
    then legacy). That keeps the module's env-beats-file rule intact — an
    exported key still overrides the file, whichever spelling it uses — while
    the canonical name wins whenever both appear in the same source."""
    for candidate in _names_for(name):
        env = os.environ.get(candidate)
        if env:
            return env
    secrets = load_secrets()
    for candidate in _names_for(name):
        if secrets.get(candidate):
            return secrets[candidate]
    return None


def store(name: str, value: str) -> None:
    """Upsert one secret, (re)writing the file `0600`. A blank value removes
    the entry (so a UI can clear a key)."""
    secrets = load_secrets()
    if value:
        secrets[name] = value
    else:
        secrets.pop(name, None)
    tmp = SECRETS_PATH.with_name(SECRETS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(secrets, indent=2) + "\n", encoding="utf-8")
    # Restrict BEFORE the rename so the secret is never briefly world-readable
    # at the final path. chmod is a POSIX concept; ignore where unsupported.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, SECRETS_PATH)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


def status() -> dict[str, dict]:
    """Per-stored-name presence + source, for the companion's keys page.
    Values are never returned — only whether a key exists and where it wins
    from (`env` shadows a stored file entry)."""
    stored = load_secrets()
    names = set(stored) | {
        n for n in os.environ if n.endswith("_API_KEY")
    }
    # Report every canonical name whose legacy spelling is present, so the
    # setup UI shows GEMINI_API_KEY as "set" for someone still on
    # GOOGLE_API_KEY instead of prompting for a key they already have.
    for canonical, aliases in LEGACY_ALIASES.items():
        if any(a in stored or os.environ.get(a) for a in aliases):
            names.add(canonical)
    out: dict[str, dict] = {}
    for name in sorted(names):
        in_env = any(os.environ.get(n) for n in _names_for(name))
        in_file = any(n in stored for n in _names_for(name))
        out[name] = {
            "set": in_env or in_file,
            "source": "env" if in_env else ("file" if in_file else "none"),
        }
    return out


def prompt_and_store(name: str, *, label: str | None = None) -> str | None:
    """Interactively ask for a secret once and persist it. No-op (returns the
    existing value) when it already resolves. Returns None when stdin isn't a
    TTY — the caller then falls back to its own actionable error, because a
    piped / coding-agent launch can't answer a prompt."""
    existing = resolve(name)
    if existing:
        return existing
    # No prompting when stdin isn't interactive, OR when we're running under
    # the web companion (TIG_SWARM_NO_PROMPT, set by control_server): its
    # process often HAS a terminal, but the user is in the browser — an
    # input() here would hang the fleet launch on an invisible prompt.
    if not sys.stdin.isatty() or os.environ.get("TIG_SWARM_NO_PROMPT"):
        return None
    what = label or name
    try:
        # A visible prompt (not getpass) so paste is obvious and the user can
        # see they typed something; the value lands in a 0600 file, not scrollback
        # history the way `export` does.
        value = input(f"  Enter {what} (stored locally in secrets.local.json): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not value:
        return None
    store(name, value)
    print(f"  saved {name} to secrets.local.json (gitignored, owner-only)")
    return value
