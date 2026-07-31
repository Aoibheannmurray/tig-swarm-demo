"""Tests for the contributor config API (hosted-console fleet-config storage
plan): GET/PUT /api/contributor/config, GET /api/contributor/agents,
GET /api/contributor/agent_defaults.

The PUT validator is the security boundary that keeps raw secrets out of
`contributor_configs` — most of this file attacks it.

Runs standalone (`python test_contributor_config.py` from the server dir) and
is also pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.
"""

import asyncio
import os
import sys
import tempfile

USERNAME = "alice"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _setup():
    db, server = _fresh_modules()
    await db.init_db()
    return db, server


def _agents(*names, **extra):
    return {"agents": [dict(name=n, provider="anthropic", **extra) for n in names]}


async def _expect_422(server, config):
    from fastapi import HTTPException
    from models import ContributorConfigPut
    try:
        await server.put_contributor_config(
            ContributorConfigPut(config=config), contributor_username=USERNAME,
        )
    except HTTPException as e:
        assert e.status_code == 422, e
        return str(e.detail)
    raise AssertionError(f"config was accepted but must be rejected: {config}")


async def test_round_trip_and_partial_updates():
    from fastapi import HTTPException
    from models import ContributorConfigPut
    _, server = await _setup()

    # 404 before first save.
    try:
        await server.get_contributor_config(contributor_username=USERNAME)
    except HTTPException as e:
        assert e.status_code == 404, e
    else:
        raise AssertionError("must 404 before first save")

    cfg = {
        "agents": [
            {"name": "my-claude", "provider": "anthropic",
             "model": "claude-opus-4-7", "api_key_env": "ANTHROPIC_API_KEY",
             "compute": "c3", "role": "explorer"},
        ],
        "hpo_search_budget": 13,
    }
    saved = await server.put_contributor_config(
        ContributorConfigPut(config=cfg), contributor_username=USERNAME,
    )
    assert saved["saved"] is True

    got = await server.get_contributor_config(contributor_username=USERNAME)
    assert got["config"] == cfg, got
    assert got["tacit"] == ""

    # Tacit-only PUT keeps the config; config-only PUT keeps the tacit.
    await server.put_contributor_config(
        ContributorConfigPut(tacit="- try simulated annealing"),
        contributor_username=USERNAME,
    )
    got = await server.get_contributor_config(contributor_username=USERNAME)
    assert got["config"] == cfg and got["tacit"] == "- try simulated annealing", got

    cfg2 = _agents("solo-agent")
    await server.put_contributor_config(
        ContributorConfigPut(config=cfg2), contributor_username=USERNAME,
    )
    got = await server.get_contributor_config(contributor_username=USERNAME)
    assert got["config"] == cfg2 and got["tacit"] == "- try simulated annealing", got

    # Empty body is a 422.
    try:
        await server.put_contributor_config(
            ContributorConfigPut(), contributor_username=USERNAME,
        )
    except HTTPException as e:
        assert e.status_code == 422, e
    else:
        raise AssertionError("empty PUT must 422")
    print("PASS test_round_trip_and_partial_updates")


async def test_validator_rejects_secrets_and_junk():
    _, server = await _setup()

    # Raw C3 key on an agent.
    detail = await _expect_422(server, {
        "agents": [{"name": "a1", "c3_api_key": "c3_live_abc123"}],
    })
    assert "c3_api_key" in detail, detail

    # A pasted raw key where the env-var NAME belongs.
    detail = await _expect_422(server, {
        "agents": [{"name": "a1", "api_key_env": "sk-ant-abc123"}],
    })
    assert "api_key_env" in detail, detail

    # Credentials at the top level.
    detail = await _expect_422(server, {
        "agents": [{"name": "a1"}], "swarm_password": "hunter2",
    })
    assert "swarm_password" in detail, detail

    # Unknown per-agent key.
    await _expect_422(server, {"agents": [{"name": "a1", "favourite_color": "red"}]})

    # Bad / duplicate / missing names (worktree-dir safety).
    await _expect_422(server, {"agents": [{"name": "../escape"}]})
    await _expect_422(server, {"agents": [{"name": "a b c"}]})
    await _expect_422(server, {"agents": [{"name": "a1"}, {"name": "a1"}]})
    await _expect_422(server, {"agents": [{"provider": "anthropic"}]})

    # Structure smuggling and size caps.
    await _expect_422(server, {"agents": [{"name": "a1", "model": {"deep": "object"}}]})
    await _expect_422(server, {"agents": [{"name": "a1", "model": "x" * 300}]})
    await _expect_422(server, {"agents": []})
    await _expect_422(server, _agents(*[f"agent-{i}" for i in range(33)]))
    print("PASS test_validator_rejects_secrets_and_junk")


async def test_agents_endpoint_is_scoped_to_caller():
    from models import RegisterRequest
    db, server = await _setup()
    await server.register_agent(
        RegisterRequest(agent_name="alices-agent"), contributor_username="alice")
    await server.register_agent(
        RegisterRequest(agent_name="bobs-agent"), contributor_username="bob")

    mine = await server.contributor_agents(contributor_username="alice")
    names = [a["name"] for a in mine["agents"]]
    assert names == ["alices-agent"], names
    assert all(a["active"] for a in mine["agents"]), mine
    print("PASS test_agents_endpoint_is_scoped_to_caller")


async def test_agent_defaults_follow_tier():
    _, server = await _setup()
    frontier = await server.contributor_agent_defaults(
        provider="anthropic", model="claude-opus-4-7",
        contributor_username=USERNAME,
    )
    assert frontier["tier"] == "frontier" and frontier["role"] == "explorer", frontier
    assert frontier["detailed_prompts"] is False, frontier

    standard = await server.contributor_agent_defaults(
        provider="openai", model="gpt-4o-mini", contributor_username=USERNAME,
    )
    assert standard["tier"] == "standard" and standard["role"] == "exploiter", standard
    assert standard["detailed_prompts"] is True, standard
    print("PASS test_agent_defaults_follow_tier")


async def test_providers_catalog_served():
    _, server = await _setup()
    got = await server.list_providers()
    keys = {p["key"] for p in got["providers"]}
    assert {"anthropic", "openai", "openrouter"} <= keys, keys
    print("PASS test_providers_catalog_served")


async def _main():
    await test_round_trip_and_partial_updates()
    await test_validator_rejects_secrets_and_junk()
    await test_agents_endpoint_is_scoped_to_caller()
    await test_agent_defaults_follow_tier()
    await test_providers_catalog_served()
    print("ALL PASS")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_main())
