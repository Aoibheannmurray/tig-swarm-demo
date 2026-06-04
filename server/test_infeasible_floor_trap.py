"""Regression tests for the infeasible-floor trap.

Runs standalone (`python test_infeasible_floor_trap.py` from the server dir) and
is also pytest-compatible. Each test builds an isolated temp DB by pointing
DATA_DIR at a fresh directory *before* importing the server modules.

The bug: on a `max` challenge whose feasible scores run well below zero (the
neuralnet baseline is ~-2.29M), an infeasible run — which scores a fixed floor
(`benchmark.INFEASIBLE_QUALITY`) — was numerically GREATER than a legitimate
feasible score. Because `beats_trajectory_best` / `is_new_best` were pure
numeric comparisons with no feasibility gate, an infeasible edit registered as a
new best, became the trajectory anchor, and every later feasible recovery — now
scoring *below* the infeasible floor — was rejected as "not an improvement". The
trajectory was pinned at the floor forever (observed: 80+ flat edits).

Three fixes, all exercised here:
  1. server.create_iteration gates `beats_trajectory_best` on feasibility.
  2. server.create_iteration gates `is_new_best` on feasibility.
  3. The stagnation deposit into the inactive/adoption pool is feasibility-gated
     so infeasible code can never seed a fresh agent.
A fourth (benchmark.INFEASIBLE_QUALITY = -QUALITY_CLAMP) is covered separately in
the benchmark aggregate smoke test.
"""

import asyncio
import os
import sys
import tempfile

CHALLENGE = "neuralnet_optimizer"  # max-direction, feasible scores run negative
TS = "2026-06-03T00:00:00Z"

# A legitimate feasible score (near the neuralnet baseline) and the OLD infeasible
# floor. The whole bug hinges on FLOOR > FEASIBLE numerically while FLOOR is
# infeasible. We use the old -1M value here precisely because it sits *above* the
# feasible baseline — that is the adversarial case the gate must reject.
FEASIBLE_BASELINE = -2_293_888.0
INFEASIBLE_FLOOR = -1_000_000.0
FEASIBLE_BETTER = -2_200_000.0  # a real improvement over the baseline


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


def _iter(server, agent_id, score, feasible, title):
    from models import IterationCreate
    return IterationCreate(
        agent_id=agent_id, title=title, strategy_tag="other",
        algorithm_code="// code", score=score, feasible=feasible,
        challenge=CHALLENGE,
    )


async def test_infeasible_does_not_beat_feasible_best():
    """Feasible best, then an infeasible run that scores numerically higher must
    NOT register as a new best / trajectory best."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    feas = await server.create_iteration(
        _iter(server, "agentA", FEASIBLE_BASELINE, True, "feasible baseline"))
    assert feas.beats_trajectory_best is True, feas
    assert feas.is_new_best is True, feas

    infeas = await server.create_iteration(
        _iter(server, "agentA", INFEASIBLE_FLOOR, False, "infeasible cheat (higher score)"))
    assert infeas.beats_trajectory_best is False, (
        "infeasible run must not beat a feasible trajectory best", infeas)
    assert infeas.is_new_best is False, (
        "infeasible run must not become the global best", infeas)

    # The trajectory anchor must still be the feasible baseline, not the floor.
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT score, feasible FROM trajectory_bests WHERE agent_id = ?",
            ("agentA",))).fetchone()
    assert row["score"] == FEASIBLE_BASELINE and row["feasible"] == 1, dict(row)
    print("PASS test_infeasible_does_not_beat_feasible_best")


async def test_feasible_recovery_after_infeasible_is_accepted():
    """The trap's tail: after an infeasible run, a genuine feasible improvement
    over the feasible best must still register (it is no longer walled off by an
    infeasible floor sitting above it)."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    await server.create_iteration(
        _iter(server, "agentA", FEASIBLE_BASELINE, True, "feasible baseline"))
    await server.create_iteration(
        _iter(server, "agentA", INFEASIBLE_FLOOR, False, "infeasible"))
    recover = await server.create_iteration(
        _iter(server, "agentA", FEASIBLE_BETTER, True, "feasible improvement"))
    assert recover.beats_trajectory_best is True, (
        "a real feasible improvement must beat the (feasible) best", recover)
    assert recover.is_new_best is True, recover
    print("PASS test_feasible_recovery_after_infeasible_is_accepted")


async def test_first_infeasible_run_sets_no_anchor():
    """A fresh trajectory whose very first run is infeasible must not anchor on
    the floor (prev_best is None must not let infeasible through)."""
    db, server = _fresh_modules()
    await db.init_db()
    await _register_agent(db)

    first = await server.create_iteration(
        _iter(server, "agentA", INFEASIBLE_FLOOR, False, "infeasible first"))
    assert first.beats_trajectory_best is False, first
    assert first.is_new_best is False, first

    async with db.connect() as conn:
        tb = await (await conn.execute(
            "SELECT COUNT(*) c FROM trajectory_bests WHERE agent_id = ?",
            ("agentA",))).fetchone()
        bh = await (await conn.execute(
            "SELECT COUNT(*) c FROM best_history WHERE challenge = ?",
            (CHALLENGE,))).fetchone()
    assert tb["c"] == 0, "no trajectory best should exist after an infeasible-only run"
    assert bh["c"] == 0, "no global best_history row should exist for an infeasible run"
    print("PASS test_first_infeasible_run_sets_no_anchor")


async def test_aggregate_infeasible_floor_below_feasible():
    """benchmark.aggregate: an all-infeasible run must score strictly below any
    realistic feasible run, and the geomean must stay defined."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bm", os.path.join(os.path.dirname(__file__), "..", "scripts", "benchmark.py"))
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)

    assert bm.INFEASIBLE_QUALITY == -bm.QUALITY_CLAMP, bm.INFEASIBLE_QUALITY
    infeasible = bm.aggregate([{"track": "t", "feasible": False}])["score"]
    feasible = bm.aggregate([{"track": "t", "feasible": True, "score": FEASIBLE_BASELINE}])["score"]
    assert feasible > infeasible, (feasible, infeasible)
    # Two-track run with one fully-infeasible track stays finite (geomean shift
    # keeps the infeasible floor at exactly +1 after shifting → log defined).
    mixed = bm.aggregate([
        {"track": "a", "feasible": True, "score": 5_000_000.0},
        {"track": "b", "feasible": False},
    ])["score"]
    assert mixed == mixed, "geomean must be finite (not NaN)"
    print("PASS test_aggregate_infeasible_floor_below_feasible")


async def _main():
    await test_infeasible_does_not_beat_feasible_best()
    await test_feasible_recovery_after_infeasible_is_accepted()
    await test_first_infeasible_run_sets_no_anchor()
    await test_aggregate_infeasible_floor_below_feasible()
    print("\nAll infeasible-floor-trap tests passed.")


if __name__ == "__main__":
    asyncio.run(_main())
