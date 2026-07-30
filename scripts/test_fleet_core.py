#!/usr/bin/env python3
"""Self-running tests for the non-interactive cores extracted for the control-ui.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_fleet_core.py

Covers:
  - init_fleet.build_fleet_config: provider remap, tier defaults, C3 handling,
    validation.
  - setup.switch_challenge: error paths (no real server needed).
  - setup.create_swarm: param plumbing + progress_cb streaming, with the
    Railway/network side-effects stubbed out so nothing is provisioned.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import init_fleet
import hostadmin

_failures = 0


# The hostadmin submodules a stub may need to land in. `create_swarm` calls its
# side-effect helpers as module globals, so patching the module that DEFINES a
# helper is not always enough — a name imported into a second submodule is a
# separate binding there, and that copy would still be the real thing.
#
# This used to be implicit: `setup.py` installed a module subclass whose
# __setattr__ forwarded every write into each submodule holding that name, so
# `setattr(setup, ...)` fanned out invisibly. Production code no longer carries
# that machinery — the fan-out lives here, in the only place that wanted it.
_PATCH_TARGETS = (
    hostadmin.swarm, hostadmin.railway, hostadmin.config_io,
    hostadmin.contributors, hostadmin.tacit, hostadmin.http,
)


def _patch_hostadmin(stubs: dict) -> dict[tuple, object]:
    """Point `name` at `stub` in every submodule that binds it.

    Returns the saved originals, keyed by (module, name), for _unpatch.
    """
    saved: dict[tuple, object] = {}
    for name, fn in stubs.items():
        hit = False
        for mod in _PATCH_TARGETS:
            if name in vars(mod):
                saved[(mod, name)] = getattr(mod, name)
                setattr(mod, name, fn)
                hit = True
        if not hit:
            raise AssertionError(
                f"stub {name!r} matches nothing in hostadmin — the helper was "
                "renamed or moved, and this stub is now silently inert."
            )
    return saved


def _unpatch_hostadmin(saved: dict[tuple, object]) -> None:
    for (mod, name), original in saved.items():
        setattr(mod, name, original)


def check(cond: bool, label: str) -> None:
    global _failures
    status = "ok  " if cond else "FAIL"
    if not cond:
        _failures += 1
    print(f"  [{status}] {label}")


def test_build_fleet_config() -> None:
    print("build_fleet_config")

    # OpenRouter → openai + api_base; standard-tier model → exploiter + detailed.
    cfg = init_fleet.build_fleet_config({
        "server_url": "https://x.up.railway.app", "username": "me",
        "swarm_password": "pw", "provider": "openrouter", "count": 2,
        "prefix": "team", "compute": "c3", "hardware": "l40", "c3_api_key": "c3_k",
    })
    a0 = cfg["agents"][0]
    check(a0["provider"] == "openai", "openrouter remapped to provider openai")
    check(a0["api_base"] == "https://openrouter.ai/api/v1", "openrouter api_base set")
    check(a0["role"] == "exploiter" and a0.get("detailed_prompts") is True,
          "standard-tier role/detailed_prompts defaults")
    check([a["name"] for a in cfg["agents"]] == ["team-1", "team-2"], "prefix names")
    check(cfg["c3_api_key"] == "c3_k", "fleet-wide c3_api_key stored")

    # claude-code frontier → explorer, no api_key_env, local compute honored.
    cfg2 = init_fleet.build_fleet_config({
        "server_url": "u", "username": "me", "swarm_password": "pw",
        "provider": "claude-code", "count": 1, "compute": "local",
    })
    a = cfg2["agents"][0]
    check(a["role"] == "explorer", "frontier claude-code → explorer")
    check("api_key_env" not in a, "claude-code carries no api_key_env")
    check(a["compute"] == "local", "local compute honored")
    check("c3_api_key" not in cfg2, "no c3 key on local compute")

    # explicit names win over count.
    cfg3 = init_fleet.build_fleet_config({
        "server_url": "u", "username": "me", "swarm_password": "pw",
        "provider": "claude-code", "names": ["alpha", "beta", "gamma"],
    })
    check([a["name"] for a in cfg3["agents"]] == ["alpha", "beta", "gamma"],
          "explicit names honored")

    # validation.
    for bad, why in [
        ({"provider": "claude-code"}, "missing connection fields"),
        ({"server_url": "u", "username": "u", "swarm_password": "p",
          "provider": "nope"}, "unknown provider"),
        ({"server_url": "u", "username": "u", "swarm_password": "p",
          "provider": "anthropic", "compute": "c3", "hardware": "bogus"},
         "unknown C3 hardware"),
    ]:
        try:
            init_fleet.build_fleet_config(bad)
            check(False, f"raises on {why}")
        except ValueError:
            check(True, f"raises on {why}")


def test_provider_data() -> None:
    print("provider / hardware data")
    provs = init_fleet.get_providers()
    check(len(provs) == len(init_fleet.PROVIDERS), "get_providers count matches PROVIDERS")
    check(all({"key", "label", "supports_c3"} <= set(p) for p in provs),
          "provider dicts are well-formed")
    check(any(h["key"] == "auto" for h in init_fleet.get_c3_hardware_choices()),
          "c3 hardware includes 'auto'")


def test_switch_challenge_errors() -> None:
    print("switch_challenge error paths")
    try:
        hostadmin.switch_challenge("definitely_not_a_challenge")
        check(False, "raises on unknown challenge")
    except ValueError as e:
        check("unknown challenge" in str(e), "raises on unknown challenge")


def test_create_swarm_stubbed() -> None:
    print("create_swarm (stubbed Railway/network)")
    calls: list[str] = []
    progress: list[str] = []

    # Stub every side-effect so nothing is provisioned or written to disk.
    stubs = {
        "_railway_provision": lambda name, ws: (calls.append("provision"), ("proj", "svc", False))[1],
        "_railway_set_variables": lambda name, env: calls.append("setvars"),
        "_railway_add_volume": lambda name, mount: calls.append("volume"),
        "_railway_up": lambda name: calls.append("up"),
        "_railway_domain": lambda name: "https://stub.up.railway.app",
        "_wait_for_server": lambda url: True,
        "push_config_to_server": lambda url, key, cfg: True,
        "read_initial_algorithms": lambda: {},
        "seed_inactive_pool_from_mainnet": lambda *a, **k: calls.append("seed"),
        "read_authored_seeds": lambda: [],
        "template_files": lambda *a, **k: calls.append("template"),
        "write_challenge_md": lambda ch: calls.append("challenge_md"),
        "write_swarm_admin": lambda cfg: calls.append("admin"),
        "write_swarm_cache": lambda cfg: calls.append("cache"),
        "_scaffold_fleet_config": lambda url, pw: calls.append("scaffold"),
        "read_swarm_cache": lambda: {},
    }
    saved = _patch_hostadmin(stubs)
    try:
        active = next(iter(hostadmin.CPU_CHALLENGES))
        challenges_cfg = hostadmin.collect_per_challenge_configs(
            {}, use_defaults=True, challenge_set=hostadmin.CPU_CHALLENGES,
        )
        result = hostadmin.create_swarm({
            "swarm_name": "unit-test-swarm",
            "swarm_type": "cpu",
            "active_challenge": active,
            "challenges_cfg": challenges_cfg,
            "stagnation_threshold": 2,
            "stagnation_limit": 10,
            "hypothesis_recall_threshold": 3,
            "seed_inactive_pool": True,
        }, progress_cb=progress.append)
    finally:
        _unpatch_hostadmin(saved)

    check(result["server_url"] == "https://stub.up.railway.app", "returns server_url")
    check(result["config_ok"] is True, "config_ok True when push succeeds")
    check(result["active_challenge"] == active, "echoes active challenge")
    check(len(result["admin_key"]) > 10 and len(result["swarm_password"]) > 10,
          "generates admin_key + swarm_password")
    for step in ("provision", "setvars", "volume", "up", "seed", "scaffold"):
        check(step in calls, f"deploy step ran: {step}")
    check(len(progress) > 0, "progress_cb received streamed lines")


def main() -> int:
    test_build_fleet_config()
    test_provider_data()
    test_switch_challenge_errors()
    test_create_swarm_stubbed()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all fleet-core checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
