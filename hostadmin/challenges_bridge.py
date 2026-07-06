"""Lazy loader for the canonical challenge definitions in server/challenges.py.

Per-challenge defaults for the wizard prompts. The canonical definitions
live in server/challenges.py, loaded lazily (by file path, so embedders
like run.py / control_server.py don't get server/ pushed onto sys.path)
the first time a command actually needs challenge metadata. Commands that
can run without server/ (e.g. `setup.py sync` in a trimmed clone) keep
working; the ones that can't fail with a clean message instead of an
ImportError at module load."""

from __future__ import annotations

import sys

from .config_io import ROOT

_challenge_registry_cache: dict | None = None
_challenges_cache: dict[str, dict] | None = None


def _load_challenge_registry() -> dict:
    """Import server/challenges.py on first use and return its CHALLENGES
    registry. Raises RuntimeError with a clean message when server/ is
    missing from this clone."""
    global _challenge_registry_cache
    if _challenge_registry_cache is None:
        path = ROOT / "server" / "challenges.py"
        if not path.is_file():
            raise RuntimeError(
                "server/challenges.py not found — this command needs the "
                "repo's server/ directory (canonical challenge definitions). "
                "Re-clone the full repository and retry."
            )
        import importlib.util
        spec = importlib.util.spec_from_file_location("_tig_server_challenges", path)
        module = importlib.util.module_from_spec(spec)
        # Register before exec: dataclasses (used by challenges.py) resolves
        # the defining module through sys.modules.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _challenge_registry_cache = module.CHALLENGES
    return _challenge_registry_cache


def get_challenges() -> dict[str, dict]:
    """{challenge: {scoring_direction, track_keys, strategy_tags, is_gpu}}."""
    global _challenges_cache
    if _challenges_cache is None:
        _challenges_cache = {
            name: {
                "scoring_direction": d.scoring_direction,
                "track_keys": list(d.track_keys),
                "strategy_tags": list(d.strategy_tags),
                "is_gpu": d.is_gpu,
            }
            for name, d in _load_challenge_registry().items()
        }
    return _challenges_cache


def get_cpu_challenges() -> dict[str, dict]:
    return {k: v for k, v in get_challenges().items() if not v["is_gpu"]}


def get_gpu_challenges() -> dict[str, dict]:
    return {k: v for k, v in get_challenges().items() if v["is_gpu"]}


def __getattr__(name: str):
    """PEP 562 — keep `setup.CHALLENGES` / `CPU_CHALLENGES` / `GPU_CHALLENGES`
    (used by control_server.py and tests) working under the lazy load.
    setup.py's compat layer delegates attribute lookups here, so the names
    resolve through this module's lazy loader."""
    if name == "CHALLENGES":
        return get_challenges()
    if name == "CPU_CHALLENGES":
        return get_cpu_challenges()
    if name == "GPU_CHALLENGES":
        return get_gpu_challenges()
    if name == "_CHALLENGE_REGISTRY":
        return _load_challenge_registry()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
