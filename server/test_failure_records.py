"""Failed-attempts archive: storage toggle, hint gating, retention, isolation.

Standalone (`python server/test_failure_records.py`). Fresh temp DB per test.

The archive stores LLM-authored failure artifacts (retrospectives + tacit
lessons) in `failure_records`, gated by `config.failed_attempts_archive`
(default 0 = off). When on, "failed_attempts" joins the stagnation-hint
rotation — but only for agents that have material, and each agent is only
ever served its OWN records.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "knapsack"  # max-direction, positive feasible scores
TS = "2026-07-28T00:00:00Z"


def _fresh_modules():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    import db
    import server
    return db, server


async def _register(db, agent_id="agentA", name="Agent A"):
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, tier) "
            "VALUES (?, ?, ?, ?, 'frontier')",
            (agent_id, name, TS, TS),
        )
        await conn.commit()


async def _set_config(db, server, **kv):
    async with db.connect() as conn:
        for key, value in kv.items():
            await conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        await conn.commit()
    server._invalidate_caches()


def _iter(agent_id, score, title="t"):
    from models import IterationCreate
    return IterationCreate(
        agent_id=agent_id, title=title, strategy_tag="greedy",
        algorithm_code="// code\n", score=score, feasible=True,
        challenge=CHALLENGE, role="explorer",
    )


def _record(agent_id, kind="retrospective", **fields):
    from models import FailureRecordCreate
    defaults = dict(
        approach_summary="greedy density ordering",
        what_was_tried="sorted by value/weight, then 2-opt swaps",
        observed_outcome="score plateaued below the trajectory best",
        possible_reasons="local optimum; no diversification step",
    )
    defaults.update(fields)
    return FailureRecordCreate(
        agent_id=agent_id, challenge=CHALLENGE, kind=kind, **defaults,
    )


async def _publish(server, req):
    return await server.create_iteration(req, token_agent_id=req.agent_id)


async def _post_record(server, req):
    return await server.create_failure_record(req, token_agent_id=req.agent_id)


class _ChoiceRecorder:
    """Patch for server.random.choice: records every offered sequence and
    prefers `want` when present (falls back to the first element)."""

    def __init__(self, want=None):
        self.want = want
        self.offered = []

    def __call__(self, seq):
        seq = list(seq)
        self.offered.append(seq)
        if self.want is not None and self.want in seq:
            return self.want
        return seq[0]


async def _stagnate(server, agent_id):
    """One improving publish then two rejected ones: runs_since=2 (default
    stagnation_threshold) with beats_trajectory_best=0 material on file."""
    await _publish(server, _iter(agent_id, 100.0, "baseline"))
    await _publish(server, _iter(agent_id, 50.0, "worse-1"))
    await _publish(server, _iter(agent_id, 50.0, "worse-2"))


async def test_toggle_off_is_noop():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)
    await _set_config(db, server, stagnation_limit=50)  # archive stays default 0

    await _stagnate(server, "agentA")
    resp = await _post_record(server, _record("agentA"))
    assert resp["stored"] is False and resp["reason"] == "archive_disabled", resp
    async with db.connect() as conn:
        count = await (await conn.execute(
            "SELECT COUNT(*) AS c FROM failure_records")).fetchone()
    assert count["c"] == 0, "toggle off must not store records"

    recorder = _ChoiceRecorder()
    server.random.choice = recorder
    state = await server._agent_state("agentA", CHALLENGE, None)
    assert state["stagnation_hint"] is not None, "agent should be stagnated"
    assert all("failed_attempts" not in seq for seq in recorder.offered), (
        f"failed_attempts must never be offered with the archive off: {recorder.offered}"
    )
    assert state["failed_attempts"] is None
    print("PASS test_toggle_off_is_noop")


async def test_toggle_on_serves_hint_and_attributes_consumption():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)
    await _set_config(db, server, failed_attempts_archive=1, stagnation_limit=50)

    await _stagnate(server, "agentA")
    resp = await _post_record(server, _record("agentA"))
    assert resp["stored"] is True, resp
    lesson = await _post_record(
        server, _record("agentA", kind="lesson",
                        lesson="- LLM: pure greedy plateaus without diversification"))
    assert lesson["stored"] is True, lesson

    recorder = _ChoiceRecorder(want="failed_attempts")
    server.random.choice = recorder
    state = await server._agent_state("agentA", CHALLENGE, None)
    assert state["stagnation_hint"] == "failed_attempts", state["stagnation_hint"]
    assert any("failed_attempts" in seq for seq in recorder.offered), recorder.offered

    payload = state["failed_attempts"]
    assert payload is not None
    kinds = {r["kind"] for r in payload["retrospectives"]}
    assert kinds == {"retrospective", "lesson"}, kinds
    retro = next(r for r in payload["retrospectives"] if r["kind"] == "retrospective")
    assert retro["approach_summary"] == "greedy density ordering", retro
    # The two non-improving publishes are the derived lightweight records.
    rejected_titles = {r["title"] for r in payload["recent_rejected"]}
    assert rejected_titles == {"worse-1", "worse-2"}, rejected_titles

    async with db.connect() as conn:
        acs = await db.get_agent_challenge_state(conn, "agentA", CHALLENGE)
    assert acs["failed_attempts_count"] == 1, acs["failed_attempts_count"]
    assert acs["pending_hint"] == "failed_attempts", acs["pending_hint"]

    # Consumption: the next publish absorbs pending_hint into
    # experiments.received_hint and the leaderboard counts CONSUMED.
    await _publish(server, _iter("agentA", 40.0, "post-hint"))
    async with db.connect() as conn:
        acs = await db.get_agent_challenge_state(conn, "agentA", CHALLENGE)
        stamped = await (await conn.execute(
            "SELECT COUNT(*) AS c FROM experiments "
            "WHERE agent_id = 'agentA' AND received_hint = 'failed_attempts'"
        )).fetchone()
        lb = await db.compute_leaderboard(conn, CHALLENGE, None, direction="max")
    assert acs["pending_hint"] is None, "pending_hint must clear on publish"
    assert stamped["c"] == 1, stamped["c"]
    row = next(r for r in lb if r["agent_id"] == "agentA")
    assert row["failed_attempts_count"] == 1, (
        "leaderboard must report the CONSUMED failed_attempts count"
    )
    print("PASS test_toggle_on_serves_hint_and_attributes_consumption")


async def test_no_material_no_hint():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)
    await _set_config(db, server, failed_attempts_archive=1, stagnation_limit=50)

    # Stagnated by counter only — no rejected experiments, no records.
    async with db.connect() as conn:
        await db.ensure_agent_challenge_state(conn, "agentA", CHALLENGE, TS)
        await db.increment_agent_challenge_counters(
            conn, "agentA", CHALLENGE, runs_since_improvement_inc=2,
        )
        await conn.commit()

    recorder = _ChoiceRecorder(want="failed_attempts")
    server.random.choice = recorder
    state = await server._agent_state("agentA", CHALLENGE, None)
    assert state["stagnation_hint"] in ("tacit_knowledge", "inspiration")
    assert all("failed_attempts" not in seq for seq in recorder.offered), (
        f"no material -> hint must not be offered: {recorder.offered}"
    )
    print("PASS test_no_material_no_hint")


async def test_retention_prunes_to_cap():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)
    await _set_config(db, server, failed_attempts_archive=1,
                      failure_records_max_per_agent=5)

    for i in range(8):
        await _post_record(server, _record("agentA", approach_summary=f"attempt {i}"))
    async with db.connect() as conn:
        count = await (await conn.execute(
            "SELECT COUNT(*) AS c FROM failure_records "
            "WHERE agent_id = 'agentA' AND challenge = ?", (CHALLENGE,)
        )).fetchone()
    assert count["c"] == 5, f"retention must keep newest 5, got {count['c']}"
    print("PASS test_retention_prunes_to_cap")


async def test_per_agent_isolation():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db, "agentA", "Agent A")
    await _register(db, "agentB", "Agent B")
    await _set_config(db, server, failed_attempts_archive=1, stagnation_limit=50)

    await _stagnate(server, "agentA")
    await _stagnate(server, "agentB")
    await _post_record(server, _record("agentA", approach_summary="A's approach"))
    await _post_record(server, _record("agentB", approach_summary="B's approach"))

    recorder = _ChoiceRecorder(want="failed_attempts")
    server.random.choice = recorder
    state = await server._agent_state("agentA", CHALLENGE, None)
    payload = state["failed_attempts"]
    assert state["stagnation_hint"] == "failed_attempts"
    summaries = {r["approach_summary"] for r in payload["retrospectives"]}
    assert summaries == {"A's approach"}, (
        f"agent A must only see its own records, got {summaries}"
    )
    async with db.connect() as conn:
        a_exp_ids = {r["id"] for r in await db.list_rejected_experiments(
            conn, "agentA", CHALLENGE, 10)}
        b_exp_ids = {r["id"] for r in await db.list_rejected_experiments(
            conn, "agentB", CHALLENGE, 10)}
    served_ids = {r["id"] for r in payload["recent_rejected"]}
    assert served_ids <= a_exp_ids and not (served_ids & b_exp_ids), (
        "recent_rejected must be agent A's own experiments only"
    )
    print("PASS test_per_agent_isolation")


async def test_clamping_and_bad_kind():
    db, server = _fresh_modules()
    await db.init_db()
    await _register(db)
    await _set_config(db, server, failed_attempts_archive=1)
    from models import MAX_FAILURE_FIELD_LEN, MAX_LESSON_LEN

    await _post_record(server, _record(
        "agentA",
        approach_summary="x" * 50_000,
        lesson="y" * 50_000,
    ))
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT approach_summary, lesson FROM failure_records "
            "WHERE agent_id = 'agentA'")).fetchone()
    assert len(row["approach_summary"]) == MAX_FAILURE_FIELD_LEN
    assert len(row["lesson"]) == MAX_LESSON_LEN

    try:
        _record("agentA", kind="ban_list")
        raise AssertionError("kind outside the Literal must be rejected")
    except ValueError:
        pass
    print("PASS test_clamping_and_bad_kind")


async def _main():
    await test_toggle_off_is_noop()
    await test_toggle_on_serves_hint_and_attributes_consumption()
    await test_no_material_no_hint()
    await test_retention_prunes_to_cap()
    await test_per_agent_isolation()
    await test_clamping_and_bad_kind()
    print("\nAll failure-records tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
