"""Host-admin implementation package, and the API embedders import.

`setup.py` at the repo root is the **CLI** — `python setup.py <create|switch|
sync|tacit|invite|revoke|list>`. This package is the **library**: run.py,
control_server.py and the tests import `hostadmin` directly.

Everything an embedder needs is re-exported below, so a caller does not have
to know which submodule currently defines a helper (and a helper can move
between submodules without breaking callers). `__all__` is the public surface;
anything not listed is internal to the package.

Historically `setup.py` played both roles, via a `types.ModuleType` subclass
whose `__getattr__` searched every submodule and whose `__setattr__` forwarded
writes back into them — the latter existing purely so a test could monkeypatch
side-effect helpers. That made `import setup` a module whose attribute
semantics were unlike any other module in the repo. The re-exports here replace
the read half; tests patch `hostadmin.<submodule>` directly for the write half.

This package must live at the repo root: the swarm runs `python setup.py
sync` inside throwaway git worktrees, and a root-level tracked package is
present in every worktree automatically.

Modules:
  config_io.py         swarm.admin.json / .swarm-cache.json / fleet config
                       read+write, placeholder templating, atomic JSON writes
  prompting.py         interactive prompt helpers for the wizards
  http.py              shared POST-JSON helper + mainnet API GET
  challenges_bridge.py lazy loader for server/challenges.py (degrades cleanly
                       when server/ is absent from a trimmed clone)
  railway.py           Railway CLI wrappers + RailwayError
  tacit.py             tacit-knowledge wizard (also used by run.py / the
                       control-ui companion)
  contributors.py      invite / revoke / list host-admin commands
  swarm.py             create / switch / sync flows (Railway provisioning,
                       config push, pool seeding)
"""

from __future__ import annotations

from .config_io import (
    ROOT,
    read_swarm_admin,
    read_swarm_cache,
    resolve_server_url,
    write_swarm_admin,
    write_swarm_cache,
)
from .contributors import (
    build_join_link,
    derive_password,
    record_issued,
    run_invite,
    run_list,
    run_revoke,
    run_set_runner,
)
from .http import post_json
from .railway import (
    RailwayError,
    _railway_check_auth,
    _railway_find_project,
    tool_argv,
)
from .swarm import (
    DEFAULT_TRACKS_PER_CHALLENGE,
    collect_per_challenge_configs,
    create_swarm,
    push_config_to_server,
    read_authored_seeds,
    read_initial_algorithms,
    run_create,
    run_create_runner,
    run_switch,
    run_sync,
    seed_pool_from_authored,
    seed_pool_from_mainnet,
    switch_challenge,
    verify_seed_pool,
)
from .tacit import (
    TACIT_QUESTIONS,
    _has_user_content,
    gather_tacit_knowledge,
    run_tacit,
    tacit_header,
)

# CHALLENGES / CPU_CHALLENGES / GPU_CHALLENGES are NOT imported eagerly: they
# come from server/challenges.py, which a trimmed clone may not have. Commands
# that don't need challenge metadata (notably `setup.py sync`, which the swarm
# runs inside worktrees) must keep working there, so resolution stays lazy and
# raises a clean message rather than an ImportError at package load.
from . import challenges_bridge as _challenges_bridge

_LAZY = ("CHALLENGES", "CPU_CHALLENGES", "GPU_CHALLENGES", "_CHALLENGE_REGISTRY")


def __getattr__(name: str):
    """PEP 562 — resolve the lazily-loaded challenge registries on first use."""
    if name in _LAZY:
        return getattr(_challenges_bridge, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    # config_io
    "ROOT", "read_swarm_admin", "read_swarm_cache", "resolve_server_url",
    "write_swarm_admin", "write_swarm_cache",
    # contributors
    "build_join_link", "derive_password", "record_issued",
    "run_invite", "run_list", "run_revoke", "run_set_runner",
    # http
    "post_json",
    # railway
    "RailwayError", "_railway_check_auth", "_railway_find_project",
    "tool_argv",
    # swarm
    "DEFAULT_TRACKS_PER_CHALLENGE", "collect_per_challenge_configs",
    "create_swarm", "push_config_to_server", "read_authored_seeds",
    "read_initial_algorithms", "run_create", "run_create_runner",
    "run_switch", "run_sync", "seed_pool_from_authored",
    "seed_pool_from_mainnet", "switch_challenge", "verify_seed_pool",
    # tacit
    "TACIT_QUESTIONS", "_has_user_content", "gather_tacit_knowledge",
    "run_tacit", "tacit_header",
    # lazy (challenges_bridge)
    *_LAZY,
]
