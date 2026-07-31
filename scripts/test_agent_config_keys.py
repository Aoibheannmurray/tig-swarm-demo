#!/usr/bin/env python3
"""Self-running tests for the per-agent config-knob registry.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_agent_config_keys.py

agent_config_keys.py replaced four hand-synced tuples across run_fleet.py and
run_loop.py. The point of the registry is that a knob declared with the wrong
flags is rejected at import instead of failing silently at runtime, so these
tests exercise the rejection, not just the happy path.

Covers:
  - the derived lists follow the flags, and the documented subset relationships
    hold (live ⊆ hot_reload ⊆ every knob; live ⊆ fleet_default)
  - _validate() rejects declarations that cannot work
  - run_fleet / run_loop really consume the registry rather than carrying
    their own copies
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent_config_keys as K
import run_fleet
import run_loop

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def test_derived_lists_follow_flags() -> None:
    print("derived lists")

    every = set(K.AGENT_CONFIG_KEYS)
    hot = set(K.HOT_RELOAD_KEYS)
    live = set(K.LIVE_CONFIG_KEYS)
    fleet = set(K.FLEET_WIDE_DEFAULT_KEYS)

    check(every == {k.name for k in K.KNOBS},
          "AGENT_CONFIG_KEYS is every declared knob")
    check(hot == {k.name for k in K.KNOBS if k.hot_reload}, "HOT_RELOAD_KEYS tracks hot_reload")
    check(live == {k.name for k in K.KNOBS if k.live}, "LIVE_CONFIG_KEYS tracks live")
    check(fleet == {k.name for k in K.KNOBS if k.fleet_default},
          "FLEET_WIDE_DEFAULT_KEYS tracks fleet_default")

    # The relationships the old four-tuple arrangement had to be told about.
    check(live <= hot, "live keys are all hot-reloaded")
    check(hot <= every, "hot-reload keys are all materialized at spawn")
    check(live <= fleet, "live keys are all inheritable from the top level")
    check(hot - live == {"role", "seeded_start"},
          "only role/seeded_start hot-reload outside the per-iteration merge")

    check(set(K.STARTUP_ONLY_KEYS).isdisjoint(hot),
          "startup-only and hot-reload are disjoint")
    check(set(K.STARTUP_ONLY_KEYS) | hot == every,
          "every knob is either startup-only or hot-reloaded")

    check(len(every) == len(K.KNOBS), "no duplicate knob names")


def test_invalid_declarations_are_rejected() -> None:
    """The registry's whole value is that a bad flag combination cannot ship.
    Each of these used to be a silent runtime no-op."""
    print("invalid declarations")

    original = K.KNOBS
    try:
        # live without hot_reload: run_loop would re-read a value the monitor
        # never delivers, so editing it on a running fleet does nothing.
        K.KNOBS = original + (K.Knob("bad_live", fleet_default=True, live=True),)
        try:
            K._validate()
            check(False, "live without hot_reload is rejected")
        except ValueError as e:
            check("hot_reload" in str(e), "live without hot_reload is rejected")

        # live without fleet_default: the documented way to set these knobs is
        # once at the top level, so that path must hot-reload too.
        K.KNOBS = original + (K.Knob("bad_inherit", hot_reload=True, live=True),)
        try:
            K._validate()
            check(False, "live without fleet_default is rejected")
        except ValueError as e:
            check("fleet_default" in str(e), "live without fleet_default is rejected")

        # A duplicate would make the derived lists disagree with themselves.
        K.KNOBS = original + (K.Knob("role"),)
        try:
            K._validate()
            check(False, "duplicate knob name is rejected")
        except ValueError as e:
            check("duplicate" in str(e), "duplicate knob name is rejected")

        # A plain startup-only knob is legal and must NOT be rejected.
        K.KNOBS = original + (K.Knob("some_new_startup_knob"),)
        try:
            K._validate()
            check(True, "an ordinary startup-only knob is accepted")
        except ValueError as e:
            check(False, f"an ordinary startup-only knob is accepted ({e})")
    finally:
        K.KNOBS = original
        K._validate()


def test_modules_consume_the_registry() -> None:
    """If someone re-hardcodes a list, these stop being the same objects and
    the drift the registry prevents is back."""
    print("modules consume the registry")

    check(run_fleet._AGENT_CONFIG_KEYS is K.AGENT_CONFIG_KEYS,
          "run_fleet._AGENT_CONFIG_KEYS is the registry's list")
    check(run_fleet._FLEET_WIDE_DEFAULT_KEYS is K.FLEET_WIDE_DEFAULT_KEYS,
          "run_fleet._FLEET_WIDE_DEFAULT_KEYS is the registry's list")
    check(run_fleet._HOT_RELOAD_KEYS is K.HOT_RELOAD_KEYS,
          "run_fleet._HOT_RELOAD_KEYS is the registry's list")
    check(run_loop.LIVE_CONFIG_KEYS is K.LIVE_CONFIG_KEYS,
          "run_loop.LIVE_CONFIG_KEYS is the registry's list")


def test_registry_is_import_safe() -> None:
    """run_loop imports this from inside a worktree, standalone. It must not
    reach for the network, the filesystem, or any non-stdlib package."""
    print("import safety")

    src = (ROOT / "scripts" / "agent_config_keys.py").read_text(encoding="utf-8")
    for forbidden in ("import os", "import json", "import urllib",
                      "open(", "Path(", "subprocess"):
        check(forbidden not in src, f"registry does not use {forbidden!r}")


def main() -> int:
    test_derived_lists_follow_flags()
    test_invalid_declarations_are_rejected()
    test_modules_consume_the_registry()
    test_registry_is_import_safe()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all config-knob registry checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
