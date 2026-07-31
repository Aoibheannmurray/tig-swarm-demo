"""Tests for --join mode config wiring.

Covers run.py's join-link parsing and run_fleet's server-config
resolution/cache, without spinning up a real server (the HTTP fetch is
monkeypatched).

Self-running: `python scripts/test_join_mode.py`.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))


def test_join_link_writes_server_sourced_config():
    import run as run_mod
    tmp = Path(tempfile.mkdtemp())
    orig = run_mod.ROOT
    run_mod.ROOT = tmp
    try:
        run_mod._apply_join_link(
            "https://my-swarm.up.railway.app/join#u=alice&p=deadbeefcafe"
        )
        cfg = json.loads((tmp / "fleet.config.json").read_text())
        assert cfg["server_url"] == "https://my-swarm.up.railway.app", cfg
        assert cfg["username"] == "alice", cfg
        assert cfg["swarm_password"] == "deadbeefcafe", cfg
        assert cfg["config_source"] == "server", cfg
    finally:
        run_mod.ROOT = orig
    print("PASS test_join_link_writes_server_sourced_config")


def test_join_link_preserves_local_agents():
    import run as run_mod
    tmp = Path(tempfile.mkdtemp())
    (tmp / "fleet.config.json").write_text(json.dumps({
        "server_url": "https://old", "username": "old", "swarm_password": "old",
        "agents": [{"name": "local-1", "provider": "anthropic"}],
    }))
    orig = run_mod.ROOT
    run_mod.ROOT = tmp
    try:
        run_mod._apply_join_link("https://new-swarm.app/join#u=bob&p=pw2")
        cfg = json.loads((tmp / "fleet.config.json").read_text())
        # Credentials refreshed…
        assert cfg["server_url"] == "https://new-swarm.app" and cfg["username"] == "bob"
        # …but a hand-authored agents array stays authoritative (no server mode).
        assert cfg["agents"] == [{"name": "local-1", "provider": "anthropic"}], cfg
        assert "config_source" not in cfg, cfg
    finally:
        run_mod.ROOT = orig
    print("PASS test_join_link_preserves_local_agents")


def test_join_link_rejects_junk():
    import run as run_mod
    for bad in ("not a url", "https://swarm.app/join", "https://swarm.app/join#u=alice"):
        try:
            run_mod._apply_join_link(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"should have rejected: {bad!r}")
    print("PASS test_join_link_rejects_junk")


def test_load_server_config_caches_and_falls_back():
    import run_fleet
    tmp = Path(tempfile.mkdtemp())
    run_fleet.FLEET_CACHE_PATH = tmp / ".fleet-cache.json"

    plan = {"agents": [{"name": "srv-1", "provider": "openai", "role": "explorer"}]}
    # 1) Successful fetch caches the plan.
    run_fleet._fetch_server_config = lambda *a, **k: plan
    got = run_fleet._load_server_config("https://s", "u", "p")
    assert got == plan, got
    assert run_fleet.FLEET_CACHE_PATH.exists()

    # 2) Server unreachable → the cached plan is used.
    run_fleet._fetch_server_config = lambda *a, **k: None
    got = run_fleet._load_server_config("https://s", "u", "p")
    assert got["agents"][0]["name"] == "srv-1", got

    # 3) Unreachable AND no cache → empty (caller then errors actionably).
    run_fleet.FLEET_CACHE_PATH = tmp / "nonexistent-cache.json"
    got = run_fleet._load_server_config("https://s", "u", "p")
    assert got == {}, got
    print("PASS test_load_server_config_caches_and_falls_back")


def test_load_server_config_empty_when_nothing_saved():
    import run_fleet
    tmp = Path(tempfile.mkdtemp())
    run_fleet.FLEET_CACHE_PATH = tmp / ".fleet-cache.json"
    # Server reachable but contributor has saved nothing (fetch returns {}).
    run_fleet._fetch_server_config = lambda *a, **k: {}
    got = run_fleet._load_server_config("https://s", "u", "p")
    assert got == {}, got
    # Empty plans are not cached (nothing worth restoring).
    assert not run_fleet.FLEET_CACHE_PATH.exists()
    print("PASS test_load_server_config_empty_when_nothing_saved")


if __name__ == "__main__":
    test_join_link_writes_server_sourced_config()
    test_join_link_preserves_local_agents()
    test_join_link_rejects_junk()
    test_load_server_config_caches_and_falls_back()
    test_load_server_config_empty_when_nothing_saved()
    print("ALL PASS")
