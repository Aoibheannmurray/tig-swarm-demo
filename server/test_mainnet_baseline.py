"""The mainnet baseline: the TIG mainnet algorithm's score on THIS swarm's
instances, so the dashboard can show members the bar they're clearing.

The load-bearing constraint is that measuring it must never slow `setup.py
create` down. So seeding records only WHICH algorithm applies (one INSERT), and
the score arrives afterwards — from the host on demand, or free, when an agent
benchmarks the mainnet seed unchanged and the server recognises the code.

Standalone: `python server/test_mainnet_baseline.py`.
"""

import asyncio
import json
import os
import sys
import tempfile

CHALLENGE = "knapsack"
TS = "2026-07-27T00:00:00Z"

_CODE = "use tig_challenges::knapsack::*;\nfn solve_challenge(){ /* mainnet */ }\n"
_INFO = {
    "algo_name": "topalgo",
    "adoption": 37 * 10**16,
    "code_files": {"mod.rs": _CODE},
    "kernel_code": None,
}


def _fresh():
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    for mod in ("db", "server", "mainnet_seed", "models"):
        sys.modules.pop(mod, None)
    import db
    import server
    import mainnet_seed
    mainnet_seed.fetch_top_reshaped = lambda ch: (
        (dict(_INFO), "") if ch == CHALLENGE
        else (None, "no compiled mainnet algorithm found"))
    return db, server


async def _key(db):
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT value FROM config WHERE key = 'admin_key'")).fetchone()
    return row["value"]


async def _seed(db, server, challenge=CHALLENGE):
    from models import AdminSeedFromMainnet
    key = await _key(db)
    return await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=challenge, target="both"))


async def test_seeding_records_the_algorithm_without_measuring_it():
    """Setup must not wait on a benchmark: the row exists, names the
    algorithm, and is explicitly 'pending' rather than pretending to a score."""
    db, server = _fresh()
    await db.init_db()
    res = await _seed(db, server)
    assert res["results"][0]["actions"]["baseline"] == "inserted", res
    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
    assert row["algo_name"] == "topalgo", row
    assert row["status"] == "pending", row
    assert row["score"] is None, row
    assert row["adoption_pct"] == 37.0, row
    assert row["code_fingerprint"] == db.code_fingerprint(_CODE)
    print("PASS test_seeding_records_the_algorithm_without_measuring_it")


async def test_a_challenge_with_no_mainnet_algorithm_says_so():
    """Better than a permanent 'pending' the dashboard can never resolve."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server, challenge="vehicle_routing")
    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, "vehicle_routing")
    assert row["status"] == "unavailable", row
    print("PASS test_a_challenge_with_no_mainnet_algorithm_says_so")


async def test_an_agent_running_it_unchanged_measures_it_for_free():
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import IterationCreate

    async with db.connect() as conn:
        # An agent publishes a run of the mainnet code, byte-identical.
        req = IterationCreate(
            agent_id="a1", title="t", description="d", strategy_tag="mainnet",
            algorithm_code=_CODE, score=1234.0, feasible=True, challenge=CHALLENGE)
        captured = await server._maybe_capture_mainnet_baseline(
            conn, CHALLENGE, req, timestamp=TS)
        assert captured is True
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
    assert row["status"] == "ready" and row["score"] == 1234.0, row
    assert row["measured_by"] == "agent:a1", row
    print("PASS test_an_agent_running_it_unchanged_measures_it_for_free")


async def test_only_the_unmodified_algorithm_counts():
    """A mutated descendant scores differently by design — adopting its score
    as 'mainnet' would put a fictional bar on the dashboard."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import IterationCreate

    async with db.connect() as conn:
        for label, kwargs in (
            ("mutated code", {"algorithm_code": _CODE + "// tweak\n", "feasible": True}),
            ("infeasible run", {"algorithm_code": _CODE, "feasible": False}),
        ):
            req = IterationCreate(
                agent_id="a1", title="t", description="d", strategy_tag="x",
                score=999.0, challenge=CHALLENGE, **kwargs)
            got = await server._maybe_capture_mainnet_baseline(
                conn, CHALLENGE, req, timestamp=TS)
            assert got is False, f"{label} must not set the baseline"
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
    assert row["status"] == "pending" and row["score"] is None, row
    print("PASS test_only_the_unmodified_algorithm_counts")


async def test_a_real_measurement_is_not_overwritten():
    """Later re-runs of the same code are noisier samples, not corrections."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import IterationCreate

    async with db.connect() as conn:
        req = IterationCreate(
            agent_id="a1", title="t", description="d", strategy_tag="mainnet",
            algorithm_code=_CODE, score=1234.0, feasible=True, challenge=CHALLENGE)
        await server._maybe_capture_mainnet_baseline(conn, CHALLENGE, req, timestamp=TS)
        req.score = 1111.0
        req.agent_id = "a2"
        again = await server._maybe_capture_mainnet_baseline(
            conn, CHALLENGE, req, timestamp=TS)
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
    assert again is False, "a second run must not overwrite the measurement"
    assert row["score"] == 1234.0 and row["measured_by"] == "agent:a1", row
    print("PASS test_a_real_measurement_is_not_overwritten")


async def test_editing_the_instance_set_marks_the_score_stale():
    """A score means nothing against a different instance set — say so rather
    than compare a number against a problem it never solved."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import IterationCreate

    async with db.connect() as conn:
        await db.upsert_challenge_config(
            conn, CHALLENGE, tracks=json.dumps({"easy": 10}), timeout=30)
        req = IterationCreate(
            agent_id="a1", title="t", description="d", strategy_tag="mainnet",
            algorithm_code=_CODE, score=1234.0, feasible=True, challenge=CHALLENGE)
        await server._maybe_capture_mainnet_baseline(conn, CHALLENGE, req, timestamp=TS)
        view = await server._mainnet_baseline_view(conn, CHALLENGE, "max")
        assert view["stale"] is False, view
        assert view["score"] == 1234.0 and view["algorithm"] == "topalgo", view

        # Host doubles the instance count — the old number no longer applies.
        await db.upsert_challenge_config(
            conn, CHALLENGE, tracks=json.dumps({"easy": 20}), timeout=30)
        view = await server._mainnet_baseline_view(conn, CHALLENGE, "max")
    assert view["stale"] is True, view
    print("PASS test_editing_the_instance_set_marks_the_score_stale")


async def test_view_is_absent_until_an_algorithm_is_known():
    """The panels render nothing rather than an empty frame."""
    db, server = _fresh()
    await db.init_db()
    async with db.connect() as conn:
        assert await server._mainnet_baseline_view(conn, CHALLENGE, "max") is None
        await _seed(db, server)
        view = await server._mainnet_baseline_view(conn, CHALLENGE, "max")
    assert view["status"] == "pending" and view["score"] is None, view
    assert view["algorithm"] == "topalgo", view
    print("PASS test_view_is_absent_until_an_algorithm_is_known")


async def test_measure_button_queues_onto_the_next_reset():
    """The Admin Console button. The server has no Docker or C3, so it can
    only steer a reset the swarm was going to do anyway: the next adoption
    picks the mainnet entry instead of a random one."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)  # target="both" → it's in the inactive pool
    from models import AdminMeasureMainnetBaseline
    import trajectory_reset

    key = await _key(db)
    res = await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))
    assert res["status"] == "requested" and res["queued"] is True, res
    assert res["algorithm"] == "topalgo", res

    async with db.connect() as conn:
        # A pool holding the mainnet entry plus decoys: the request must win.
        pool = [
            {"id": 1, "algorithm_code": "other one\n", "score": 5.0},
            {"id": 2, "algorithm_code": _CODE, "score": None},
            {"id": 3, "algorithm_code": "another\n", "score": 7.0},
        ]
        for _ in range(12):  # random.choice would escape a one-off fluke
            picked = await trajectory_reset._pick_inactive(conn, CHALLENGE, pool)
            assert picked["id"] == 2, picked
        # Once measured, selection goes back to being random.
        await db.set_mainnet_baseline_score(
            conn, CHALLENGE, 42.0, feasible=True, benchmarked_at=TS,
            measured_by="agent:a1")
        ids = {(await trajectory_reset._pick_inactive(conn, CHALLENGE, pool))["id"]
               for _ in range(40)}
    assert len(ids) > 1, "a fulfilled request must not pin adoption forever"
    print("PASS test_measure_button_queues_onto_the_next_reset")


async def test_the_whole_measure_chain_end_to_end():
    """Request → the reset adopts the mainnet entry → the agent benchmarks it
    unchanged → the score becomes the baseline.

    This seam shipped broken: the button sets status='requested' while the
    capture accepted only 'pending', so pressing "Measure" silently disabled
    the very capture it was asking for. Both halves had tests; the join
    between them did not — observed live as a swarm that benchmarked the
    mainnet algorithm and still showed no bar."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import AdminMeasureMainnetBaseline, IterationCreate
    import trajectory_reset

    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))

    async with db.connect() as conn:
        # The reset steers at the mainnet entry...
        pool = [{"id": 1, "algorithm_code": "other\n", "score": 5.0},
                {"id": 2, "algorithm_code": _CODE, "score": None}]
        picked = await trajectory_reset._pick_inactive(conn, CHALLENGE, pool)
        assert picked["id"] == 2, picked
        # ...the agent benchmarks it unchanged and publishes.
        req = IterationCreate(
            agent_id="darth-vader", title="t", description="d",
            strategy_tag="mainnet", algorithm_code=picked["algorithm_code"],
            score=177591.0, feasible=True, challenge=CHALLENGE)
        captured = await server._maybe_capture_mainnet_baseline(
            conn, CHALLENGE, req, timestamp=TS)
        assert captured is True, "a requested measurement must be captured"
        view = await server._mainnet_baseline_view(conn, CHALLENGE, "max")
    assert view["status"] == "ready", view
    assert view["score"] == 177591.0, view
    assert view["measured_by"] == "agent:darth-vader", view
    print("PASS test_the_whole_measure_chain_end_to_end")


async def test_measure_button_reports_an_unfulfillable_request():
    """Seeding into the seed pool only leaves nothing for a reset to adopt, so
    the request would sit unfulfilled forever. Say that, don't just say OK."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet, AdminMeasureMainnetBaseline
    key = await _key(db)
    await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=CHALLENGE, target="seed_pool"))
    res = await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))
    assert res["queued"] is False, res
    assert "inactive" in res["detail"], res
    print("PASS test_measure_button_reports_an_unfulfillable_request")


async def test_measure_button_refuses_when_nothing_is_known():
    db, server = _fresh()
    await db.init_db()
    from models import AdminMeasureMainnetBaseline
    from fastapi import HTTPException
    key = await _key(db)
    for setup, needle in (
        (None, "Seed from mainnet"),
        ("vehicle_routing", "No compatible mainnet algorithm"),
    ):
        if setup:
            await _seed(db, server, challenge=setup)  # marks it unavailable
        try:
            await server.admin_measure_mainnet_baseline(
                AdminMeasureMainnetBaseline(
                    admin_key=key, challenge=setup or CHALLENGE))
            raise AssertionError(f"expected a refusal for {setup or CHALLENGE}")
        except HTTPException as exc:
            assert needle in exc.detail, exc.detail
    print("PASS test_measure_button_refuses_when_nothing_is_known")


async def main():
    await test_seeding_records_the_algorithm_without_measuring_it()
    await test_a_challenge_with_no_mainnet_algorithm_says_so()
    await test_an_agent_running_it_unchanged_measures_it_for_free()
    await test_only_the_unmodified_algorithm_counts()
    await test_a_real_measurement_is_not_overwritten()
    await test_editing_the_instance_set_marks_the_score_stale()
    await test_view_is_absent_until_an_algorithm_is_known()
    await test_measure_button_queues_onto_the_next_reset()
    await test_the_whole_measure_chain_end_to_end()
    await test_measure_button_reports_an_unfulfillable_request()
    await test_measure_button_refuses_when_nothing_is_known()
    print("\nAll mainnet-baseline tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
