"""Tests for the `custom` provider — a contributor's own OpenAI-compatible LLM.

Covers the whole path a self-hosted endpoint takes: the provider table entry,
the config build (model + api_base + api_key_env written through as provider
`openai`), and the two launch-time key resolutions that must NOT demand a key
from a server that checks none.

Self-running: `python scripts/test_custom_provider.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import init_fleet  # noqa: E402
import run_fleet  # noqa: E402
import run_loop  # noqa: E402
import secrets_local  # noqa: E402
from llm_backends import is_local_api_base  # noqa: E402

_BASE = {"server_url": "https://swarm.example", "username": "u",
         "swarm_password": "p"}


def test_is_local_api_base():
    for url in ("http://127.0.0.1:8000/v1", "http://localhost:11434/v1",
                "https://LOCALHOST:8000", "http://[::1]:8000/v1",
                "http://192.168.1.9:8000", "http://10.0.0.4:1234/v1",
                "http://172.16.5.5:8000", "http://my-box.local:1234/v1",
                "127.0.0.1:8000/v1"):
        assert is_local_api_base(url), url
    for url in ("https://api.openai.com", "https://openrouter.ai/api/v1",
                "https://api.deepseek.com/v1", "http://8.8.8.8:8000/v1",
                "", None):
        assert not is_local_api_base(url), url
    print("PASS test_is_local_api_base")


def test_custom_provider_in_catalog():
    catalog = init_fleet.get_providers()
    custom = next(p for p in catalog if p["key"] == "custom")
    assert custom["needs_api_base"] is True
    # No default model: only the contributor's server knows what it serves.
    assert custom["default_model"] == ""
    assert custom["popular_models"] == []
    # Benchmarking is independent of where the LLM runs — a local model can
    # still use C3 cloud compute.
    assert custom["supports_c3"] is True
    # Every other provider must stay a fixed-endpoint one.
    assert [p["key"] for p in catalog if p["needs_api_base"]] == ["custom"]
    print("PASS test_custom_provider_in_catalog")


def test_build_writes_endpoint_through():
    cfg = init_fleet.build_fleet_config({
        **_BASE,
        "provider": "custom",
        "model": "Qwen3-Coder-Next-Q8_0",
        "api_base": "http://127.0.0.1:8000/v1",
        "api_key_env": "xxxx",
        "compute": "local",
        "role": "explorer",
        "names": ["Omnissiah-1"],
    })
    agent = cfg["agents"][0]
    # Written as an OpenAI-compatible agent — the same remap OpenRouter and
    # DeepSeek get, so run_loop needs no new provider branch.
    assert agent["provider"] == "openai"
    assert agent["model"] == "Qwen3-Coder-Next-Q8_0"
    assert agent["api_base"] == "http://127.0.0.1:8000/v1"
    assert agent["api_key_env"] == "xxxx"
    assert agent["compute"] == "local"
    assert agent["role"] == "explorer"
    print("PASS test_build_writes_endpoint_through")


def test_build_defaults_and_rejections():
    # api_key_env is optional — a name is still needed to look one up under.
    cfg = init_fleet.build_fleet_config({
        **_BASE, "provider": "custom", "model": "m",
        "api_base": "http://127.0.0.1:8000/v1", "names": ["a"],
    })
    assert cfg["agents"][0]["api_key_env"] == init_fleet._CUSTOM_API_KEY_ENV

    def rejects(params, needle):
        try:
            init_fleet.build_fleet_config({**_BASE, **params})
        except ValueError as exc:
            assert needle in str(exc), f"{needle!r} not in {exc}"
            return True
        return False

    # Neither has anything to fall back on, so both must be asked for.
    assert rejects({"provider": "custom", "model": "m"}, "api_base is required")
    assert rejects({"provider": "custom", "api_base": "http://127.0.0.1:8000/v1"},
                   "model is required")
    assert rejects({"provider": "custom", "model": "m", "api_base": "127.0.0.1:8000"},
                   "must be an http")
    # api_base/api_key_env are custom-only: a normal provider ignores them
    # rather than silently pointing Anthropic at someone's laptop.
    cfg = init_fleet.build_fleet_config({
        **_BASE, "provider": "anthropic", "model": "claude-opus-4-8",
        "api_base": "http://127.0.0.1:8000/v1", "api_key_env": "xxxx",
        "names": ["a"],
    })
    assert "api_base" not in cfg["agents"][0]
    assert cfg["agents"][0]["api_key_env"] == "ANTHROPIC_API_KEY"
    print("PASS test_build_defaults_and_rejections")


def test_launch_does_not_demand_a_key_for_a_local_endpoint():
    """Both key resolutions must tolerate a keyless self-hosted server.

    llama.cpp/vLLM/Ollama authenticate nothing by default, so requiring a key
    would block the setup this feature exists for — while a public provider
    must still fail fast and clearly."""
    local = {"name": "a", "provider": "openai", "api_key_env": "xxxx",
             "api_base": "http://127.0.0.1:8000/v1"}

    # run_fleet: no key stored, and it must not prompt — prompt_and_store()
    # blows up here if the local-endpoint branch ever stops short-circuiting.
    orig_resolve, orig_prompt = secrets_local.resolve, secrets_local.prompt_and_store
    secrets_local.resolve = lambda name, *a, **k: ""
    secrets_local.prompt_and_store = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not prompt for a key a local server won't check")
    )
    try:
        assert run_fleet._resolve_api_key(local) == (None, None)
        # A stored key still gets used — some local servers do check one.
        secrets_local.resolve = lambda name, *a, **k: "sk-local" if name == "xxxx" else ""
        assert run_fleet._resolve_api_key(local) == ("OPENAI_API_KEY", "sk-local")
        # A public endpoint keeps the old behavior: prompt, then exit.
        remote = {**local, "api_base": "https://api.deepseek.com/v1"}
        try:
            run_fleet._resolve_api_key(remote)
            raise AssertionError("a public provider must still require a key")
        except AssertionError as exc:
            assert "must not prompt" in str(exc), exc
    finally:
        secrets_local.resolve, secrets_local.prompt_and_store = orig_resolve, orig_prompt

    # run_loop: same rule inside the worktree, where the key is resolved again.
    secrets_local.resolve = lambda name, *a, **k: ""
    try:
        assert run_loop.resolve_api_key("openai", None, "http://127.0.0.1:8000/v1") == ""
        try:
            run_loop.resolve_api_key("openai", None, "https://api.openai.com")
            raise AssertionError("a public provider must still require a key")
        except SystemExit as exc:
            assert "No API key" in str(exc)
    finally:
        secrets_local.resolve = orig_resolve
    print("PASS test_launch_does_not_demand_a_key_for_a_local_endpoint")


if __name__ == "__main__":
    test_is_local_api_base()
    test_custom_provider_in_catalog()
    test_build_writes_endpoint_through()
    test_build_defaults_and_rejections()
    test_launch_does_not_demand_a_key_for_a_local_endpoint()
    print("ALL PASS")
