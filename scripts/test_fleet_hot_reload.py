#!/usr/bin/env python3
"""Self-running tests for the fleet's hot-reload sync.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_fleet_hot_reload.py

Hot reload is the path that lets a contributor retune a RUNNING fleet: edit
fleet.config.json (or the hosted plan), and run_fleet's monitor patches the
changed fields into each worktree's agent.config.json, which run_loop re-reads
on its next iteration. No restart.

Covers:
  - the two halves of the wiring stay in step: every key run_loop re-reads per
    iteration (LIVE_CONFIG_KEYS) is one run_fleet actually propagates
    (_HOT_RELOAD_KEYS), and _HOT_RELOAD_KEYS carries nothing run_loop reads
    only at startup.
  - _sync_hot_reload_to_worktrees: patches changed keys, leaves everything else
    alone, preserves freshly-registered identity, tolerates a bad worktree.
  - fleet-wide top-level knobs are inherited by the sync, not just at launch —
    these are documented as "set them once at the top level".
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import run_fleet
import run_loop

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    status = "ok  " if cond else "FAIL"
    if not cond:
        _failures += 1
    print(f"  [{status}] {label}")


# Read once at startup by run_loop (see the arg/agent_config resolution before
# the loop) — propagating these would log a change the running process never
# makes, so they must stay OUT of _HOT_RELOAD_KEYS.
_STARTUP_ONLY_KEYS = (
    "provider", "model", "api_base", "compute", "c3_hardware", "c3_time",
    "c3_max_parallel_jobs", "c3_api_key", "log_prompts", "detailed_prompts",
    "agent_id", "agent_name", "agent_token",
)


def test_key_lists_agree() -> None:
    print("key lists")

    missing = [k for k in run_loop.LIVE_CONFIG_KEYS
               if k not in run_fleet._HOT_RELOAD_KEYS]
    check(not missing,
          f"every run_loop.LIVE_CONFIG_KEYS key is propagated (missing: {missing})")

    # role/seeded_start are re-read explicitly at the top of run_loop's loop
    # rather than via LIVE_CONFIG_KEYS, so they're expected extras.
    extra = [k for k in run_fleet._HOT_RELOAD_KEYS
             if k not in run_loop.LIVE_CONFIG_KEYS
             and k not in ("role", "seeded_start")]
    check(not extra, f"no unexplained keys in _HOT_RELOAD_KEYS (extra: {extra})")

    leaked = [k for k in _STARTUP_ONLY_KEYS if k in run_fleet._HOT_RELOAD_KEYS]
    check(not leaked, f"no startup-only keys hot-reloaded (leaked: {leaked})")

    # Every hot-reloadable knob must also be forwarded at spawn, or agent one
    # gets it live but a restarted fleet silently drops back to defaults.
    unspawned = [k for k in run_fleet._HOT_RELOAD_KEYS
                 if k not in run_fleet._AGENT_CONFIG_KEYS]
    check(not unspawned,
          f"hot-reload keys are materialized at spawn too (missing: {unspawned})")


def _worktree(tmp: Path, name: str, config: dict) -> Path:
    # Agent names here are deliberately git-safe already, so the worktree dir
    # is the name verbatim whether or not run_fleet slugs it.
    wt = tmp / "worktrees" / name
    wt.mkdir(parents=True)
    (wt / "agent.config.json").write_text(json.dumps(config), encoding="utf-8")
    return wt


def test_sync_patches_changed_keys() -> None:
    print("_sync_hot_reload_to_worktrees")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        orig_wt_dir = run_fleet.WORKTREES_DIR
        run_fleet.WORKTREES_DIR = tmp / "worktrees"
        try:
            wt = _worktree(tmp, "agent-1", {
                "agent_id": "a1", "agent_token": "tok",
                "provider": "anthropic", "model": "claude-opus-5",
                "role": "explorer", "hpo_search_budget": 4,
                "cleaner_cooldown_iters": 3,
            })
            agents = [{"name": "agent-1"}]
            entries = {"agent-1": {
                "name": "agent-1",
                "role": "exploiter",           # changed
                "hpo_search_budget": 12,       # changed
                "cleaner_cooldown_iters": 3,   # unchanged
                "no_benchmark_freeze_limit": 0,  # newly set, and falsy
                "provider": "openai",          # startup-only: must be ignored
            }}
            run_fleet._sync_hot_reload_to_worktrees(agents, entries)

            after = json.loads((wt / "agent.config.json").read_text())
            check(after["role"] == "exploiter", "changed role synced")
            check(after["hpo_search_budget"] == 12, "changed hpo knob synced")
            check(after["no_benchmark_freeze_limit"] == 0,
                  "falsy value synced (0 is a real setting, not 'absent')")
            check(after["provider"] == "anthropic",
                  "startup-only provider left untouched")
            check(after["model"] == "claude-opus-5", "unrelated keys preserved")
            check(after["agent_token"] == "tok", "identity preserved")

            # An absent key means "no opinion", not "reset to default" — the
            # worktree keeps its value rather than losing it mid-run.
            run_fleet._sync_hot_reload_to_worktrees(
                agents, {"agent-1": {"name": "agent-1"}})
            after2 = json.loads((wt / "agent.config.json").read_text())
            check(after2["hpo_search_budget"] == 12,
                  "key dropped from config leaves the live value alone")

            # A worktree that isn't there yet (or is mid-write) must not crash
            # the monitor — it supervises the whole fleet.
            run_fleet._sync_hot_reload_to_worktrees(
                [{"name": "never-spawned"}],
                {"never-spawned": {"name": "never-spawned", "role": "exploiter"}})
            check(True, "missing worktree tolerated")
        finally:
            run_fleet.WORKTREES_DIR = orig_wt_dir


def test_identity_written_during_sync_is_kept() -> None:
    """run_loop may register and persist agent_id/token between the monitor's
    read and its write. The write must not clobber a freshly-issued token —
    that would force a re-register and split the agent's history."""
    print("identity race")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        orig_wt_dir = run_fleet.WORKTREES_DIR
        orig_read = run_fleet._read_json
        run_fleet.WORKTREES_DIR = tmp / "worktrees"
        try:
            wt = _worktree(tmp, "agent-1", {"role": "explorer"})
            cfg_path = wt / "agent.config.json"

            reads = {"n": 0}

            def _racing_read(path: Path):
                data = orig_read(path)
                # After the monitor's first read of this file, simulate run_loop
                # registering: it writes identity fields we don't have.
                if Path(path) == cfg_path:
                    reads["n"] += 1
                    if reads["n"] == 1:
                        registered = dict(data)
                        registered.update({"agent_id": "fresh",
                                           "agent_token": "fresh-tok"})
                        cfg_path.write_text(json.dumps(registered),
                                            encoding="utf-8")
                return data

            run_fleet._read_json = _racing_read
            run_fleet._sync_hot_reload_to_worktrees(
                [{"name": "agent-1"}],
                {"agent-1": {"name": "agent-1", "role": "exploiter"}})
            after = json.loads(cfg_path.read_text())
            check(after["role"] == "exploiter", "role still synced")
            check(after.get("agent_token") == "fresh-tok",
                  "identity written mid-sync is not clobbered")
        finally:
            run_fleet._read_json = orig_read
            run_fleet.WORKTREES_DIR = orig_wt_dir


def test_fleet_wide_defaults_reach_the_sync() -> None:
    """The HPO/cleaner/warm-image knobs are documented as 'set them once at the
    top level'. Launch applies that inheritance; so must the monitor, or the
    documented way to configure them is the one way that won't hot-reload."""
    print("fleet-wide defaults")

    entries = run_fleet._hot_reload_entries({
        "hpo_search_budget": 9,
        "cleaner_cooldown_iters": 7,
        "agents": [
            {"name": "a-1"},
            {"name": "a-2", "hpo_search_budget": 2},  # per-agent override
            {"role": "explorer"},                     # nameless: skipped
        ],
    })
    check(set(entries) == {"a-1", "a-2"}, "nameless entries skipped")
    check(entries["a-1"]["hpo_search_budget"] == 9, "top-level knob inherited")
    check(entries["a-1"]["cleaner_cooldown_iters"] == 7,
          "second top-level knob inherited")
    check(entries["a-2"]["hpo_search_budget"] == 2,
          "per-agent value still wins over the fleet-wide default")

    check(all(k in run_fleet._FLEET_WIDE_DEFAULT_KEYS
              for k in run_loop.LIVE_CONFIG_KEYS),
          "every live knob is inheritable from the top level")


def main() -> int:
    test_key_lists_agree()
    test_sync_patches_changed_keys()
    test_identity_written_during_sync_is_kept()
    test_fleet_wide_defaults_reach_the_sync()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all hot-reload checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
