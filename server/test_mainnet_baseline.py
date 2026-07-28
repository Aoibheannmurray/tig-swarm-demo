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


async def test_measure_button_provisions_everything_itself():
    """One button, no prerequisites. It used to require "Seed from mainnet"
    into the INACTIVE pool first — two steps with an ordering that, done
    wrong, produced a request nothing could ever fulfil."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminMeasureMainnetBaseline

    # Nothing seeded at all: no baseline row, empty pools.
    key = await _key(db)
    res = await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))
    assert res["queued"] is True, res
    assert res["algorithm"] == "topalgo", res
    assert "recorded topalgo" in res["provisioned"], res
    assert "added it to the inactive pool" in res["provisioned"], res

    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
        # Genuinely performable, not merely marked requested.
        assert await server._mainnet_measurement_claimable(conn, CHALLENGE)
    assert row["status"] == "requested", row
    print("PASS test_measure_button_provisions_everything_itself")


async def test_measure_button_re_deposits_a_consumed_entry():
    """Adoption is consume-once, so a failed or repeated measurement leaves
    the pool empty. Pressing again must refill it rather than queueing against
    nothing — the state the beta swarm got stuck in."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminMeasureMainnetBaseline
    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))

    async with db.connect() as conn:  # an agent adopts it; the row is deleted
        for e in await db.get_inactive_with_deactivations(conn, CHALLENGE):
            await db.remove_inactive(conn, e["id"])
        await conn.commit()
        assert not await server._mainnet_measurement_claimable(conn, CHALLENGE)

    res = await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))
    assert "added it to the inactive pool" in res["provisioned"], res
    async with db.connect() as conn:
        assert await server._mainnet_measurement_claimable(conn, CHALLENGE)
    print("PASS test_measure_button_re_deposits_a_consumed_entry")


async def test_measure_button_does_not_refetch_when_ready():
    """A fetch is two calls to someone else's API. With the algorithm recorded
    and an adoptable copy pooled, there is nothing to provision."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminMeasureMainnetBaseline
    import mainnet_seed
    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))

    calls = []
    orig = mainnet_seed.fetch_top_reshaped

    def _counted(ch):
        calls.append(ch)
        return orig(ch)

    mainnet_seed.fetch_top_reshaped = _counted
    try:
        res = await server.admin_measure_mainnet_baseline(
            AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))
    finally:
        mainnet_seed.fetch_top_reshaped = orig
    assert calls == [], f"should not refetch when ready, got {calls}"
    assert res["provisioned"] == [], res
    assert res["queued"] is True, res
    print("PASS test_measure_button_does_not_refetch_when_ready")


async def test_measure_button_still_refuses_what_it_cannot_do():
    """A challenge with no compatible mainnet algorithm must say so, not
    queue a measurement that can never complete."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminMeasureMainnetBaseline
    from fastapi import HTTPException
    key = await _key(db)
    try:
        await server.admin_measure_mainnet_baseline(
            AdminMeasureMainnetBaseline(admin_key=key, challenge="vehicle_routing"))
        raise AssertionError("expected a refusal")
    except HTTPException as exc:
        assert "No compatible mainnet algorithm" in exc.detail, exc.detail
    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, "vehicle_routing")
    assert row["status"] == "unavailable", row
    print("PASS test_measure_button_still_refuses_what_it_cannot_do")


async def test_a_differently_reshaped_pool_entry_is_not_mistaken_for_this_one():
    """The drift trap. Two reshapes of the same mainnet algorithm — host-side
    challenge_files vs server-side mainnet_seed, kept only in "rough sync" —
    produce different bytes and therefore different hashes. The old guard
    counted rows, so an entry from the other path satisfied it, the deposit
    was skipped, and the fingerprint just recorded described code that existed
    nowhere. Nothing could ever match it and nothing said so."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet

    key = await _key(db)
    async with db.connect() as conn:
        agent_id = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        # What the OTHER reshape left behind: same algorithm, different bytes.
        await db.deposit_inactive(
            conn, agent_id, CHALLENGE, _CODE.replace("/* mainnet */", "/*mainnet*/"),
            None, TS)
        await conn.commit()

    res = await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=CHALLENGE, target="both"))
    assert res["results"][0]["actions"]["inactive"] == "seeded", res

    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
        # The invariant that was broken: the recorded fingerprint must have a
        # matching entry an agent can actually adopt.
        assert await db.has_inactive_with_code(conn, agent_id, CHALLENGE, _CODE)
        assert row["code_fingerprint"] == db.code_fingerprint(_CODE)
        # The other entry is left alone — it may be someone's real seed, and
        # deleting it to tidy our own bookkeeping is a bad trade.
        cur = await conn.execute(
            "SELECT COUNT(*) c FROM inactive_algorithms WHERE challenge = ?",
            (CHALLENGE,))
        assert (await cur.fetchone())["c"] == 2
    print("PASS test_a_differently_reshaped_pool_entry_is_not_mistaken_for_this_one")


async def test_an_unrelated_seed_no_longer_blocks_mainnet():
    """seed_inactive's source_label DEFAULTS to "tig-foundation", so a host
    admin-seeding any algorithm without setting one used to block mainnet
    deposits on that challenge outright."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedInactive, AdminSeedFromMainnet

    key = await _key(db)
    await server.admin_seed_inactive(AdminSeedInactive(
        admin_key=key, challenge=CHALLENGE,
        algorithm_code="// somebody's own algorithm\n"))
    res = await server.admin_seed_from_mainnet(
        AdminSeedFromMainnet(admin_key=key, challenge=CHALLENGE, target="both"))
    assert res["results"][0]["actions"]["inactive"] == "seeded", res
    print("PASS test_an_unrelated_seed_no_longer_blocks_mainnet")


async def test_seeding_the_same_algorithm_twice_stays_idempotent():
    """The looser guard must not turn repeat runs into duplicate pool rows."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedFromMainnet
    key = await _key(db)
    for expected in ("seeded", "already_seeded", "already_seeded"):
        res = await server.admin_seed_from_mainnet(
            AdminSeedFromMainnet(admin_key=key, challenge=CHALLENGE, target="both"))
        assert res["results"][0]["actions"]["inactive"] == expected, res
    async with db.connect() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) c FROM inactive_algorithms WHERE challenge = ?",
            (CHALLENGE,))
        assert (await cur.fetchone())["c"] == 1
    print("PASS test_seeding_the_same_algorithm_twice_stays_idempotent")


async def test_the_host_cli_path_registers_the_baseline_too():
    """`setup.py create` deposits mainnet via seed_inactive, which never
    touched mainnet_baselines — so every CLI-seeded swarm had the algorithm
    in its pool and no bar on its dashboard."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedInactive

    key = await _key(db)
    res = await server.admin_seed_inactive(AdminSeedInactive(
        admin_key=key, challenge=CHALLENGE, algorithm_code=_CODE,
        mainnet_algo_name="hgs_advance", mainnet_adoption_pct=41.2))
    assert res["seeded"] is True, res
    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
    assert row["algo_name"] == "hgs_advance", row
    assert row["adoption_pct"] == 41.2, row
    assert row["code_fingerprint"] == db.code_fingerprint(_CODE), row
    assert row["status"] == "pending", row

    # An ordinary seed carries no mainnet fields and must register nothing.
    db, server = _fresh()
    await db.init_db()
    key = await _key(db)
    await server.admin_seed_inactive(AdminSeedInactive(
        admin_key=key, challenge=CHALLENGE, algorithm_code="// plain\n"))
    async with db.connect() as conn:
        assert await db.get_mainnet_baseline(conn, CHALLENGE) is None
    print("PASS test_the_host_cli_path_registers_the_baseline_too")


async def test_baseline_registers_even_when_the_deposit_is_skipped():
    """Registration is about WHICH algorithm the challenge is measured
    against; a duplicate deposit being skipped is no reason to leave the
    dashboard with no bar."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminSeedInactive

    key = await _key(db)
    seed = AdminSeedInactive(
        admin_key=key, challenge=CHALLENGE, algorithm_code=_CODE,
        mainnet_algo_name="hgs_advance")
    await server.admin_seed_inactive(seed)
    async with db.connect() as conn:
        await conn.execute("DELETE FROM mainnet_baselines")
        await conn.commit()
    res = await server.admin_seed_inactive(seed)  # now hits already_seeded
    assert res["seeded"] is False and res["reason"] == "already_seeded", res
    async with db.connect() as conn:
        row = await db.get_mainnet_baseline(conn, CHALLENGE)
    assert row is not None and row["algo_name"] == "hgs_advance", row
    print("PASS test_baseline_registers_even_when_the_deposit_is_skipped")


async def _agent_with_a_trajectory(db, server, name="a1", score=100.0):
    """An agent mid-flight and improving — i.e. one that will never stagnate,
    which is exactly the case the old design could not serve."""
    from models import IterationCreate
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?)", (name, name, TS, TS))
        await conn.commit()
    req = IterationCreate(
        agent_id=name, title="t", description="d", strategy_tag="x",
        algorithm_code="// mine\n", score=score, feasible=True,
        challenge=CHALLENGE)
    await server.create_iteration(req, token_agent_id=name)
    return name


async def test_measurement_happens_without_waiting_for_stagnation():
    """The whole point of the one-off job. A healthy swarm never stagnates, so
    a measurement queued behind a stagnation reset never ran — the button
    looked broken because, in practice, it was."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import AdminMeasureMainnetBaseline

    agent = await _agent_with_a_trajectory(db, server)
    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))

    # The agent is NOT stagnating; it just published an improvement.
    state = await server.get_state(agent_id=agent, challenge=CHALLENGE,
                                   token_agent_id=agent)
    reset = state.get("trajectory_reset") or {}
    assert reset.get("type") == "adopted_inactive", reset
    assert reset.get("reason") == "mainnet_baseline", reset
    # needs_benchmark is what drives the agent's existing [SEED-BENCH] path,
    # which benchmarks the adopted code unchanged — no agent-side change.
    assert reset.get("needs_benchmark") is True, reset
    assert state["best_algorithm_code"] == _CODE, "must hand over the mainnet code"

    # The agent's own work was banked on the way past, not discarded.
    async with db.connect() as conn:
        pool = await db.get_inactive_with_deactivations(conn, CHALLENGE)
    assert any("// mine" in (e["algorithm_code"] or "") for e in pool), pool
    print("PASS test_measurement_happens_without_waiting_for_stagnation")


async def test_only_one_agent_is_pulled_off_its_trajectory():
    """Without a claim, every agent polling for state would be handed the same
    forced reset and the whole swarm would drop what it was doing."""
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import AdminMeasureMainnetBaseline

    agents = [await _agent_with_a_trajectory(db, server, f"a{i}", 100.0 + i)
              for i in range(4)]
    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))

    forced = []
    for a in agents:
        st = await server.get_state(agent_id=a, challenge=CHALLENGE, token_agent_id=a)
        if (st.get("trajectory_reset") or {}).get("reason") == "mainnet_baseline":
            forced.append(a)
    assert len(forced) == 1, f"exactly one agent should be claimed, got {forced}"
    print("PASS test_only_one_agent_is_pulled_off_its_trajectory")


async def test_a_dead_claimant_does_not_park_the_measurement():
    db, server = _fresh()
    await db.init_db()
    await _seed(db, server)
    from models import AdminMeasureMainnetBaseline

    a1 = await _agent_with_a_trajectory(db, server, "a1")
    a2 = await _agent_with_a_trajectory(db, server, "a2", 101.0)
    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))
    await server.get_state(agent_id=a1, challenge=CHALLENGE, token_agent_id=a1)

    # a1 claimed it and then died. Age the claim past its TTL.
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE mainnet_baselines SET benchmarked_at = '2000-01-01T00:00:00+00:00'")
        # Put a fresh mainnet entry back — a1 consumed the first on adoption.
        agent_id = await db.ensure_synthetic_agent(conn, "tig-foundation", TS)
        await db.deposit_inactive(conn, agent_id, CHALLENGE, _CODE, None, TS)
        await conn.commit()

    st = await server.get_state(agent_id=a2, challenge=CHALLENGE, token_agent_id=a2)
    assert (st.get("trajectory_reset") or {}).get("reason") == "mainnet_baseline", st
    print("PASS test_a_dead_claimant_does_not_park_the_measurement")


async def test_no_agent_is_disturbed_when_nothing_is_adoptable():
    """A request with nothing to adopt must not reset agents, over and over.

    The button now provisions the pool entry itself, so this state no longer
    arises from pressing it — it arises AFTER an adoption, because the pool is
    consume-once: the measuring agent takes the entry, and if its benchmark
    fails the request is still outstanding with nothing left to adopt. That is
    exactly what happened on the beta swarm when the mainnet algorithm would
    not compile."""
    db, server = _fresh()
    await db.init_db()
    from models import AdminMeasureMainnetBaseline
    key = await _key(db)
    await server.admin_measure_mainnet_baseline(
        AdminMeasureMainnetBaseline(admin_key=key, challenge=CHALLENGE))

    async with db.connect() as conn:  # the entry is adopted and consumed
        for e in await db.get_inactive_with_deactivations(conn, CHALLENGE):
            await db.remove_inactive(conn, e["id"])
        await conn.commit()

    a1 = await _agent_with_a_trajectory(db, server, "a1")
    st = await server.get_state(agent_id=a1, challenge=CHALLENGE, token_agent_id=a1)
    assert (st.get("trajectory_reset") or {}).get("reason") != "mainnet_baseline", st
    print("PASS test_no_agent_is_disturbed_when_nothing_is_adoptable")


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
    await test_measure_button_provisions_everything_itself()
    await test_measure_button_re_deposits_a_consumed_entry()
    await test_measure_button_does_not_refetch_when_ready()
    await test_measure_button_still_refuses_what_it_cannot_do()
    await test_a_differently_reshaped_pool_entry_is_not_mistaken_for_this_one()
    await test_an_unrelated_seed_no_longer_blocks_mainnet()
    await test_seeding_the_same_algorithm_twice_stays_idempotent()
    await test_the_host_cli_path_registers_the_baseline_too()
    await test_baseline_registers_even_when_the_deposit_is_skipped()
    await test_measurement_happens_without_waiting_for_stagnation()
    await test_only_one_agent_is_pulled_off_its_trajectory()
    await test_a_dead_claimant_does_not_park_the_measurement()
    await test_no_agent_is_disturbed_when_nothing_is_adoptable()
    print("\nAll mainnet-baseline tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
