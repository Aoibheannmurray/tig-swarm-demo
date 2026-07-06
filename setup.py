#!/usr/bin/env python3
"""TIG Swarm host-admin CLI.

NOT Python packaging — never `pip install` this. It runs as
`python setup.py <subcommand>` (and is shelled out to by
scripts/run_loop.py / scripts/run_fleet.py).

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

The implementation lives in the root-level `hostadmin/` package; this file
is the thin argparse dispatcher plus a back-compat import surface — every
name that used to be defined here is still importable as `setup.<name>`
(run.py, control_server.py, and scripts/test_fleet_core.py rely on that).
"""

from __future__ import annotations

import argparse
import sys
import types

# hostadmin/ sits next to this file at the repo root, so it exists in every
# git worktree the swarm spawns (`python setup.py sync` runs inside them) and
# resolves via sys.path[0] whether this file is run as a script or imported
# with the root on sys.path.
from hostadmin import challenges_bridge as _challenges_bridge
from hostadmin import config_io as _config_io
from hostadmin import contributors as _contributors
from hostadmin import http as _http
from hostadmin import prompting as _prompting
from hostadmin import railway as _railway
from hostadmin import swarm as _swarm
from hostadmin import tacit as _tacit

from hostadmin.challenges_bridge import get_challenges
from hostadmin.contributors import run_invite, run_list, run_revoke
from hostadmin.railway import RailwayError
from hostadmin.swarm import run_create, run_switch, run_sync
from hostadmin.tacit import run_tacit

# Attribute delegation order for the back-compat surface below. swarm first:
# it re-imports most cross-module helpers, so it holds the broadest namespace.
_COMPAT_MODULES = (
    _swarm,
    _railway,
    _tacit,
    _contributors,
    _config_io,
    _challenges_bridge,
    _http,
    _prompting,
)


# ── Entrypoint ──────────────────────────────────────────────────────


def _challenge_choices() -> list[str] | None:
    """Challenge names for argparse `choices`, or None (unrestricted) when
    server/ is missing from this clone — commands that actually need the
    registry validate and fail cleanly themselves."""
    try:
        return list(get_challenges().keys())
    except RuntimeError:
        return None


def add_create_setup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", help="Railway workspace name.")
    parser.add_argument("--swarm-name", help="Railway project/service name.")
    parser.add_argument("--swarm-type", choices=["cpu", "gpu"], help="Swarm hardware class.")
    parser.add_argument("--active-challenge", choices=_challenge_choices(), help="Initial active challenge.")
    parser.add_argument("--use-defaults", action="store_true", help="Use default tracks/timeouts for every challenge.")
    parser.add_argument("--stagnation-threshold", type=int, help="Iterations before hints/inspiration.")
    parser.add_argument("--stagnation-limit", type=int, help="Iterations before trajectory reset; 0 disables.")
    parser.add_argument("--hypothesis-recall-threshold", type=int, help="Iterations before prior failed hypotheses are shown.")
    parser.add_argument("--yes", action="store_true", help="Accept defaults for any optional prompts.")
    parser.add_argument(
        "--seed-inactive-pool", action="store_true",
        help=(
            "After deploy, seed the server's inactive_algorithms pool with the "
            "current top-earning TIG mainnet algorithm for every challenge in "
            "the swarm (multi-file aware; incompatible ones are skipped)."
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
        "challenge", choices=_challenge_choices(),
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
        "username", nargs="?", default=None,
        help=(
            "Optional. Contributor's username (anything identifying them — "
            "paste into fleet.config.json). If omitted, a random "
            "adjective-noun slug is generated for you."
        ),
    )
    revoke = sub.add_parser(
        "revoke",
        help="Host: revoke a contributor by username (blocks future registers, kills active agents).",
    )
    revoke.add_argument(
        "username",
        help="Username to revoke (as printed by `setup.py invite`).",
    )
    sub.add_parser(
        "list",
        help="Host: list contributors (joined agents, active count, revoked state).",
    )
    args = parser.parse_args()

    if args.mode == "create":
        try:
            return run_create(args)
        except RailwayError as exc:
            # CLI behavior preserved: Railway failures still exit 2 with the
            # message; programmatic callers (control_server) catch the raise.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except RuntimeError as exc:  # e.g. server/ missing from this clone
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.mode == "switch":
        return run_switch(args.challenge)
    if args.mode == "sync":
        return run_sync()
    if args.mode == "tacit":
        return run_tacit(args.agent_name)
    if args.mode == "invite":
        return run_invite(args.username)
    if args.mode == "revoke":
        return run_revoke(args.username)
    if args.mode == "list":
        return run_list()

    print(
        "setup.py is the host-admin tool.\n"
        "  contributors:  edit fleet.config.json, then run "
        "`python scripts/run_fleet.py`.\n"
        "  hosts:         `python setup.py create` to provision a new swarm.\n"
        "  invite a contributor:  `python setup.py invite [<username>]`.\n"
        "  revoke a contributor:  `python setup.py revoke <username>`.\n"
        "  list contributors:     `python setup.py list`.\n"
        "  switch challenge:      `python setup.py switch <challenge>`.\n"
        "  edit tacit knowledge:  `python setup.py tacit [<agent-name>]`.",
        file=sys.stderr,
    )
    return 1


# ── Back-compat import surface ───────────────────────────────────────
#
# `setup` used to be a single 2,500-line module holding every helper. After
# the split into hostadmin/, this module class keeps two contracts alive
# without any caller changing:
#
#   * Attribute lookup — `setup.read_swarm_admin`, `setup.create_swarm`,
#     `setup._railway_run`, … resolve from whichever hostadmin submodule now
#     defines them, and the lazy names (`setup.CHALLENGES` /
#     `CPU_CHALLENGES` / `GPU_CHALLENGES` / `_CHALLENGE_REGISTRY`, used by
#     control_server.py and tests) resolve through
#     hostadmin.challenges_bridge's PEP 562 loader, preserving the clean
#     degradation when server/ is absent from a trimmed clone.
#
#   * setattr patching — scripts/test_fleet_core.py monkeypatches
#     side-effect helpers via `setattr(setup, name, stub)` and expects
#     `setup.create_swarm` to see the stubs. A write here is forwarded into
#     every hostadmin submodule whose namespace holds that name, so the
#     late-bound lookups inside hostadmin (e.g. create_swarm calling
#     `_railway_provision`) see it too.


class _SetupModule(types.ModuleType):
    def __getattr__(self, name: str):
        for mod in _COMPAT_MODULES:
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for mod in _COMPAT_MODULES:
            if name in vars(mod):
                setattr(mod, name, value)


sys.modules[__name__].__class__ = _SetupModule


if __name__ == "__main__":
    sys.exit(main())
