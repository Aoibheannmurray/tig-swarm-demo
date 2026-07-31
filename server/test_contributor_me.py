"""Tests for GET /api/contributor/me (the invite-validation endpoint).

The endpoint validates a contributor credential pair (the same
X-Username / X-Swarm-Password headers that gate agent registration) and
returns the swarm summary the hosted /join page renders. Auth flows through
verify_swarm_password, so revocation and bad-password behavior must match
the register path exactly.

Runs standalone (`python test_contributor_me.py` from the server dir) and is
also pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.
"""

import asyncio
import hashlib
import os
import sys
import tempfile

BASE = "base-secret"
USERNAME = "alice"


def _derived(username: str) -> str:
    return hashlib.sha256(f"{username}:{BASE}".encode()).hexdigest()


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _setup(revoked: list[str] | None = None):
    """Fresh modules with swarm_password / swarm_name / active challenge (and
    optionally revoked contributors — stored as a JSON array, matching
    server._revoked_usernames) written to config before the server's config
    cache is primed."""
    import json
    db, server = _fresh_modules()
    await db.init_db()
    async with db.connect() as conn:
        for key, value in (
            ("swarm_password", BASE),
            ("swarm_name", "test-swarm"),
            ("active_challenge", "satisfiability"),
            ("revoked_contributors", json.dumps(revoked or [])),
        ):
            await conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()
    return db, server


async def test_valid_invite():
    _, server = await _setup()
    username = await server.verify_swarm_password(
        x_username=USERNAME, x_swarm_password=_derived(USERNAME),
    )
    me = await server.contributor_me(contributor_username=username)
    assert me["username"] == USERNAME, me
    assert me["swarm_name"] == "test-swarm", me
    assert me["active_challenge"] == "satisfiability", me
    assert me["swarm_type"] in ("cpu", "gpu"), me
    # runner_url is present (empty when no hosted runner is configured), so the
    # join page can decide whether to offer the cloud tier.
    assert me["runner_url"] == "", me
    print("PASS test_valid_invite")


async def test_wrong_password_is_403():
    from fastapi import HTTPException
    _, server = await _setup()
    try:
        await server.verify_swarm_password(
            x_username=USERNAME, x_swarm_password="not-the-derived-hash",
        )
    except HTTPException as e:
        assert e.status_code == 403, e
    else:
        raise AssertionError("wrong password must 403")
    print("PASS test_wrong_password_is_403")


async def test_revoked_contributor_is_403():
    from fastapi import HTTPException
    _, server = await _setup(revoked=[USERNAME])
    try:
        await server.verify_swarm_password(
            x_username=USERNAME, x_swarm_password=_derived(USERNAME),
        )
    except HTTPException as e:
        assert e.status_code == 403, e
    else:
        raise AssertionError("revoked contributor must 403")
    print("PASS test_revoked_contributor_is_403")


def test_join_link_round_trip():
    """The CLI's join link must carry exactly the values /join needs: the
    fragment params parse back to the same username + derived password."""
    import urllib.parse
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hostadmin.contributors import build_join_link

    derived = _derived("weird name+/?")
    link = build_join_link("https://swarm.example.app/", "weird name+/?", derived)
    assert link.startswith("https://swarm.example.app/join#"), link
    frag = urllib.parse.parse_qs(link.split("#", 1)[1])
    assert frag["u"] == ["weird name+/?"], frag
    assert frag["p"] == [derived], frag

    # No usable server URL → no link (placeholder/template values included).
    assert build_join_link(None, "a", "b") is None
    assert build_join_link("<paste server URL>", "a", "b") is None
    assert build_join_link("$SERVER_URL", "a", "b") is None
    print("PASS test_join_link_round_trip")


async def _main():
    await test_valid_invite()
    await test_wrong_password_is_403()
    await test_revoked_contributor_is_403()
    test_join_link_round_trip()
    print("ALL PASS")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(_main())
