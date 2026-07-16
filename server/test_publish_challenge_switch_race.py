"""Regression tests for the publish challenge-switch race.

Runs standalone (`python test_publish_challenge_switch_race.py` from the server
dir). Each test builds an isolated temp DB by pointing DATA_DIR at a fresh
directory *before* importing the server modules.

The bug: `create_iteration` resolved a publish's challenge via
`resolve_challenge`, which falls back to the swarm's *current* `active_challenge`
when the request omits the field. If the host switched the active challenge
between an agent benchmarking its algorithm and publishing the result, the
result was recorded under the NEW challenge — tagging the old challenge's code
onto the new challenge's trajectory (observed: a hypergraph algorithm appearing
as a neuralnet_optimizer trajectory best, which then failed every neuralnet
validation and stalled the agent).

The fix: a publish MUST state the challenge it was benchmarked on. The server
refuses a challenge-less publish instead of inferring it from `active_challenge`,
so a result is always attributed to the challenge it actually ran on. The client
always sends it (benchmark.py stamps the benchmarked challenge; publish falls
back to the synced config challenge).

Exercised here:
  1. A publish with no challenge is rejected (400) rather than silently
     attributed to active_challenge.
  2. A publish naming an explicit challenge is recorded under THAT challenge,
     even when it differs from active_challenge — i.e. the switch can't
     misattribute an explicitly-tagged result.
  3. The normal happy path (publish for the active challenge) still records.
"""

import asyncio
import os
import sys
import tempfile

from fastapi import HTTPException

TS = "2026-07-15T00:00:00Z"


def _fresh_modules():
    """Re-import db + server against a brand-new temp DB. Returns (db, server)."""
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _register_agent(db, agent_id="agentA", name="Agent A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, name, TS, TS),
        )
        await conn.commit()


async def _set_active(db, server, challenge):
    """Flip the swarm's active challenge and drop the server's config cache so
    the next read sees it (mirrors the admin config-update path)."""
    async with db.connect() as conn:
        await db.set_active_challenge(conn, challenge)
        await conn.commit()
    server._config_cache = None
    server._challenge_config_cache = None


def _iter(agent_id, score, title, challenge):
    """Build an IterationCreate. `challenge=None` models a client that omitted
    the field (the racy pre-fix case)."""
    from models import IterationCreate
    kwargs = dict(
        agent_id=agent_id, title=title, strategy_tag="other",
        algorithm_code="// code", score=score,
    )
    if challenge is not None:
        kwargs["challenge"] = challenge
    return IterationCreate(**kwargs)


async def _publish(server, req):
    return await server.create_iteration(req, token_agent_id=req.agent_id)


async def test_challengeless_publish_is_rejected():
    """A publish that omits `challenge` must be refused, not inferred from the
    active challenge (the misattribution vector)."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _set_active(db, server, "neuralnet_optimizer")

    raised = False
    try:
        await _publish(server, _iter("agentA", 1.0, "no challenge", challenge=None))
    except HTTPException as e:
        raised = True
        assert e.status_code == 400, e.status_code
        assert "challenge" in e.detail.lower(), e.detail
    assert raised, "a challenge-less publish must raise HTTPException(400)"

    # Nothing was written under the active challenge.
    async with db.connect() as conn:
        n = await (await conn.execute(
            "SELECT COUNT(*) c FROM hypotheses WHERE challenge = ?",
            ("neuralnet_optimizer",))).fetchone()
    assert n["c"] == 0, "rejected publish must not create rows"
    print("PASS test_challengeless_publish_is_rejected")


async def test_explicit_challenge_is_not_misattributed():
    """A result explicitly tagged with the challenge it ran on is recorded under
    THAT challenge, even after the active challenge has moved on — the switch
    can't drag it onto the new challenge's trajectory."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    # Swarm has already switched to neuralnet_optimizer, but this in-flight
    # result was benchmarked on hypergraph.
    await _set_active(db, server, "neuralnet_optimizer")

    resp = await _publish(server, _iter("agentA", 123.0, "late hypergraph result",
                                        challenge="hypergraph"))
    assert resp.experiment_id, resp

    async with db.connect() as conn:
        hg = await (await conn.execute(
            "SELECT COUNT(*) c FROM hypotheses WHERE challenge = ?",
            ("hypergraph",))).fetchone()
        nn = await (await conn.execute(
            "SELECT COUNT(*) c FROM hypotheses WHERE challenge = ?",
            ("neuralnet_optimizer",))).fetchone()
    assert hg["c"] == 1, "result must record under the challenge it named"
    assert nn["c"] == 0, "result must NOT leak onto the active challenge"
    print("PASS test_explicit_challenge_is_not_misattributed")


async def test_active_challenge_happy_path_records():
    """The normal case — publishing for the active challenge — still records."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)
    await _set_active(db, server, "knapsack")

    resp = await _publish(server, _iter("agentA", 999.0, "normal", challenge="knapsack"))
    assert resp.experiment_id and resp.is_new_best, resp
    async with db.connect() as conn:
        n = await (await conn.execute(
            "SELECT COUNT(*) c FROM hypotheses WHERE challenge = ?",
            ("knapsack",))).fetchone()
    assert n["c"] == 1, resp
    print("PASS test_active_challenge_happy_path_records")


async def _main():
    await test_challengeless_publish_is_rejected()
    await test_explicit_challenge_is_not_misattributed()
    await test_active_challenge_happy_path_records()
    print("\nAll publish challenge-switch-race tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
