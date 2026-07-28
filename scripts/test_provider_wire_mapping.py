"""Guard: the provider a UI *offers* vs the provider a config may *contain*.

`deepseek`, `openrouter` and `custom` are setup-level answers to "which
vendor?" — they are NOT legal values for an agent's `provider` field. The
wizard has always known that (build_fleet_config rewrites them to `openai` plus
an api_base), but the control-ui's direct config editor wrote the setup key
straight through, so choosing DeepSeek there produced a fleet.config.json that
run_fleet rejected at launch:

    Agent a1: unknown provider 'deepseek'

These tests pin the mapping down at every layer it crosses: the remap helper,
the catalog the UIs read, the config builder, the launch-time provider table,
and the direct-save endpoint's validation.

Self-running: `python scripts/test_provider_wire_mapping.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import init_fleet  # noqa: E402
import run_fleet  # noqa: E402

_BASE = {"server_url": "https://swarm.example", "username": "u",
         "swarm_password": "p"}

# Setup keys that are rewritten on the way into fleet.config.json.
_REMAPPED = {
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "custom": None,          # endpoint supplied by the contributor
}


def test_resolve_wire_provider():
    for key, base in _REMAPPED.items():
        wire, api_base = init_fleet.resolve_wire_provider(key)
        assert wire == "openai", (key, wire)
        assert api_base == base, (key, api_base)
    # Everything else passes through untouched.
    for key in ("anthropic", "openai", "google", "venice", "claude-code"):
        assert init_fleet.resolve_wire_provider(key) == (key, None), key
    print("PASS test_resolve_wire_provider")


def test_catalog_exposes_the_mapping():
    """The UIs can only apply the remap if the catalog tells them about it."""
    catalog = {p["key"]: p for p in init_fleet.get_providers()}
    for key, base in _REMAPPED.items():
        entry = catalog[key]
        assert entry["wire_provider"] == "openai", entry
        assert entry["api_base"] == base, entry
    assert catalog["anthropic"]["wire_provider"] == "anthropic"
    assert catalog["anthropic"]["api_base"] is None
    # ...and enough to fill in the rest of the agent entry, which is what the
    # editor got wrong: a vendor switch has to carry its model and key env too.
    # `custom` is the exception by design — its model id and endpoint are the
    # contributor's to supply, which is why the UI must offer a text box there.
    for p in catalog.values():
        if p["needs_api_base"]:
            continue
        assert p["default_model"], p["key"]
        assert p["popular_models"], p["key"]
    print("PASS test_catalog_exposes_the_mapping")


def test_every_wire_provider_is_launchable():
    """The set the config may contain must be the set run_fleet accepts.

    This is the assertion that would have caught the bug: `deepseek` is in the
    catalog but not in run_fleet's table, so it can never appear in a config.
    """
    cli = ("claude-code", "claude-code-agentic", "codex-agentic")
    launchable = set(run_fleet._PROVIDER_TO_DEFAULT_ENV) | set(cli)
    unknown = init_fleet.wire_providers() - launchable
    assert not unknown, f"wire providers run_fleet would reject: {sorted(unknown)}"
    # And the reverse direction: a setup key that isn't launchable must be one
    # the remap rewrites.
    for p in init_fleet.get_providers():
        if p["key"] not in launchable:
            assert p["wire_provider"] in launchable, p["key"]
    print("PASS test_every_wire_provider_is_launchable")


def test_build_fleet_config_writes_the_wire_shape():
    for key, base in (("deepseek", _REMAPPED["deepseek"]),
                      ("openrouter", _REMAPPED["openrouter"])):
        cfg = init_fleet.build_fleet_config({**_BASE, "provider": key, "count": 1})
        agent = cfg["agents"][0]
        assert agent["provider"] == "openai", agent
        assert agent["api_base"] == base, agent
        # The vendor's own key env and default model ride along — the three
        # fields the editor used to leave pointing at the previous provider.
        spec = next(p for p in init_fleet.get_providers() if p["key"] == key)
        assert agent["api_key_env"] == spec["api_key_env"], agent
        assert agent["model"] == spec["default_model"], agent
    print("PASS test_build_fleet_config_writes_the_wire_shape")


def test_direct_save_rejects_a_setup_key():
    """/local-api/fleet/config/save is the editor's path — it must not write a
    config that dies at launch, and must say what to write instead."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("SKIP test_direct_save_rejects_a_setup_key (fastapi not installed)")
        return
    import control_server

    # base_url carries a loopback Host so the DNS-rebinding guard lets us in.
    client = TestClient(control_server.create_app(), base_url="http://127.0.0.1")
    bad = {**_BASE, "agents": [{"name": "a1", "provider": "deepseek",
                                "model": "deepseek-v4-pro", "compute": "local"}]}
    res = client.post("/local-api/fleet/config/save", json={"config": bad})
    assert res.status_code == 400, (res.status_code, res.text)
    err = res.json()["error"]
    assert "deepseek" in err and "openai" in err, err
    assert "https://api.deepseek.com/v1" in err, err

    # The shape the editor writes now is accepted (and only that shape) —
    # without this the guard could be "reject everything" and still pass.
    good = {**_BASE, "agents": [{"name": "a1", "provider": "openai",
                                 "model": "deepseek-v4-pro", "compute": "local",
                                 "api_key_env": "DEEPSEEK_API_KEY",
                                 "api_base": _REMAPPED["deepseek"]}]}
    import tempfile, json as _json
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fleet.config.json"
        original = control_server.FLEET_CONFIG_PATH
        control_server.FLEET_CONFIG_PATH = path
        init_fleet.FLEET_CONFIG_PATH = path
        try:
            res = client.post("/local-api/fleet/config/save", json={"config": good})
            assert res.status_code == 200, (res.status_code, res.text)
            written = _json.loads(path.read_text())
            assert written["agents"][0]["api_base"] == _REMAPPED["deepseek"], written
        finally:
            control_server.FLEET_CONFIG_PATH = original
            init_fleet.FLEET_CONFIG_PATH = original
    print("PASS test_direct_save_rejects_a_setup_key")


if __name__ == "__main__":
    test_resolve_wire_provider()
    test_catalog_exposes_the_mapping()
    test_every_wire_provider_is_launchable()
    test_build_fleet_config_writes_the_wire_shape()
    test_direct_save_rejects_a_setup_key()
    print("ALL PASS")
