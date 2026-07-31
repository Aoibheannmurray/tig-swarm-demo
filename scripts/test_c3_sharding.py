"""Tests for distributed (nonce-sharded) C3 benchmarking.

Runs standalone (`python3 test_c3_sharding.py` from the scripts dir) — no
network / no C3. Covers the pure logic in c3_compute.py:

  * `_plan_shards` splits all tracks' nonces into exactly
    `min(num_machines, total)` **balanced** shards (sizes differ by <=1 nonce),
    packing across track boundaries (e.g. {9:10,16:8} over 3 machines packs 9's
    tail with 16 into one shard), ignores the `seed` key, and collapses to a
    single shard when num_machines <= 1.
  * `_merge_shard_benchmarks` reassembles per-shard benchmark.json dicts into one
    combined dict, preserving nonce order/count and propagating errors — and the
    merged dict scores identically through `benchmark._tig_adapter` to the
    equivalent unsharded run (the whole point: sharding changes speed, not score).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark
import c3_compute


# ── _plan_shards ───────────────────────────────────────────────────


def _slices(shards, track):
    """All (start, count) slices for `track`, in shard order."""
    return [
        (s["start"], s["count"])
        for shard in shards for s in shard if s["track_key"] == track
    ]


def _shard_totals(shards):
    return [sum(s["count"] for s in shard) for shard in shards]


def _assert_balanced(shards):
    """No two shards differ by more than one nonce."""
    totals = _shard_totals(shards)
    assert max(totals) - min(totals) <= 1, totals


def test_balanced_sizes_examples():
    # The exact examples from the design discussion.
    assert c3_compute._balanced_sizes(22, 3) == [8, 7, 7]
    assert c3_compute._balanced_sizes(22, 4) == [6, 6, 5, 5]
    assert c3_compute._balanced_sizes(2, 3) == [1, 1]          # fewer nonces than machines
    assert c3_compute._balanced_sizes(16, 4) == [4, 4, 4, 4]   # exact multiple
    assert c3_compute._balanced_sizes(17, 1) == [17]           # single machine
    print("PASS test_balanced_sizes_examples")


def test_plan_shards_balanced_remainder():
    # 22 nonces over 3 machines -> 8, 7, 7.
    shards = c3_compute._plan_shards({"9": 22}, 3)
    assert _slices(shards, "9") == [(0, 8), (8, 7), (15, 7)], shards
    assert _shard_totals(shards) == [8, 7, 7]
    assert len(shards) == 3
    _assert_balanced(shards)
    print("PASS test_plan_shards_balanced_remainder")


def test_plan_shards_balanced_four_machines():
    # 22 nonces over 4 machines -> 6, 6, 5, 5.
    shards = c3_compute._plan_shards({"9": 22}, 4)
    assert _slices(shards, "9") == [(0, 6), (6, 6), (12, 5), (17, 5)], shards
    assert _shard_totals(shards) == [6, 6, 5, 5]
    assert len(shards) == 4
    _assert_balanced(shards)
    print("PASS test_plan_shards_balanced_four_machines")


def test_plan_shards_exact_multiple():
    shards = c3_compute._plan_shards({"16": 16}, 4)
    assert _slices(shards, "16") == [(0, 4), (4, 4), (8, 4), (12, 4)], shards
    assert _shard_totals(shards) == [4, 4, 4, 4]
    print("PASS test_plan_shards_exact_multiple")


def test_plan_shards_fewer_nonces_than_machines():
    # Can't split a nonce: min(machines, total) shards of one nonce each.
    shards = c3_compute._plan_shards({"9": 2}, 3)
    assert _slices(shards, "9") == [(0, 1), (1, 1)], shards
    assert len(shards) == 2
    print("PASS test_plan_shards_fewer_nonces_than_machines")


def test_plan_shards_packs_across_tracks():
    # {9:10, 16:8} over 3 machines: 18 nonces -> [6, 6, 6].
    #   [9[0:6]], [9[6:10] + 16[0:2]], [16[2:8]]  — middle shard mixes tracks.
    shards = c3_compute._plan_shards({"9": 10, "16": 8, "seed": 12345}, 3)
    assert _slices(shards, "9") == [(0, 6), (6, 4)]
    assert _slices(shards, "16") == [(0, 2), (2, 6)]
    assert all(s["track_key"] != "seed" for shard in shards for s in shard)
    assert len(shards) == 3
    assert _shard_totals(shards) == [6, 6, 6]
    assert any(len({s["track_key"] for s in shard}) > 1 for shard in shards)
    print("PASS test_plan_shards_packs_across_tracks")


def test_plan_shards_single_machine_is_single_job():
    for machines in (1, 0, -1):  # <=1 clamps to a single job for everything
        shards = c3_compute._plan_shards({"9": 17, "16": 5}, machines)
        assert len(shards) == 1, (machines, shards)
        assert _slices(shards, "9") == [(0, 17)], (machines, shards)
        assert _slices(shards, "16") == [(0, 5)], (machines, shards)
    print("PASS test_plan_shards_single_machine_is_single_job")


def test_plan_shards_skips_nonpositive_and_nonint():
    shards = c3_compute._plan_shards({"9": 0, "16": -3, "x": "nope", "8": 3}, 1)
    assert _slices(shards, "8") == [(0, 3)]
    assert len(shards) == 1
    print("PASS test_plan_shards_skips_nonpositive_and_nonint")


def test_plan_shards_partitions_without_overlap_or_gap():
    total = 25
    shards = c3_compute._plan_shards({"9": total}, 4)
    covered = []
    for shard in shards:
        for s in shard:
            covered.extend(range(s["start"], s["start"] + s["count"]))
    assert covered == list(range(total)), covered  # contiguous, ordered, no dupes
    assert len(shards) == 4
    _assert_balanced(shards)
    print("PASS test_plan_shards_partitions_without_overlap_or_gap")


def test_plan_shards_empty():
    assert c3_compute._plan_shards({}, 3) == []
    assert c3_compute._plan_shards({"seed": 1}, 3) == []
    print("PASS test_plan_shards_empty")


# ── _merge_shard_benchmarks + score parity ────────────────────────


def _ir(track, i, score, feasible=True, error=None):
    rec = {"instance": f"{track}/{i}", "track": track,
           "feasible": feasible, "score": score if feasible else None}
    if error:
        rec["error"] = error
    return rec


def _bench(instance_results, viz=None, errors=None, challenge="knapsack"):
    """Build a benchmark.py-shaped output dict for one shard."""
    agg = benchmark.aggregate(instance_results)
    return {
        "challenge": challenge,
        **agg,
        "errors": errors or None,
        "instance_results": instance_results,
        "viz_data": viz,
    }


def test_merge_concats_instances_and_unions_viz_and_errors():
    merged = c3_compute._merge_shard_benchmarks(
        [
            _bench([_ir("9", 0, 1.0)], viz={"9/0": {"x": 1}},
                   errors=["9/0: hiccup"]),
            _bench([_ir("16", 0, 2.0)], viz={"16/0": {"x": 2}}),
        ],
        "knapsack",
    )
    assert [r["instance"] for r in merged["instance_results"]] == ["9/0", "16/0"]
    assert merged["viz_data"] == {"9/0": {"x": 1}, "16/0": {"x": 2}}
    assert merged["errors"] == ["9/0: hiccup"]
    assert merged["instances_solved"] == 2
    # challenge_metrics is an opaque full-run roll-up shards don't carry.
    assert "challenge_metrics" not in merged
    print("PASS test_merge_concats_instances_and_unions_viz_and_errors")


def test_sharded_score_matches_unsharded():
    # Reference: one unsharded run with 17+8 instances across two tracks,
    # including infeasible instances (they pin to the infeasible floor).
    t9 = [_ir("9", i, 1_000.0 + 10 * i, feasible=(i % 4 != 0)) for i in range(17)]
    t16 = [_ir("16", i, 2_000.0 - 5 * i) for i in range(8)]
    ref = benchmark.aggregate(t9 + t16)

    # Same instances, balanced over 4 machines exactly how _plan_shards lays
    # them out — including a shard that mixes both tracks — then each shard
    # produces its own benchmark.json and the orchestrator merges them.
    recs_by_track = {"9": t9, "16": t16}
    shards = c3_compute._plan_shards({"9": len(t9), "16": len(t16)}, 4)
    assert any(len({s["track_key"] for s in shard}) > 1 for shard in shards), \
        "expected at least one shard mixing tracks"
    shard_benches = []
    for shard in shards:
        window = []
        for s in shard:
            window.extend(recs_by_track[s["track_key"]][s["start"]: s["start"] + s["count"]])
        shard_benches.append(_bench(window))
    merged = c3_compute._merge_shard_benchmarks(shard_benches, "knapsack")

    assert merged["score"] == ref["score"], (merged["score"], ref["score"])
    assert merged["instances_solved"] == ref["instances_solved"] == 25
    assert merged["instances_feasible"] == ref["instances_feasible"]
    assert merged["track_scores"] == ref["track_scores"]
    print("PASS test_sharded_score_matches_unsharded")


def test_runners_export_track_starts():
    import tempfile
    from pathlib import Path as _P
    for writer, image in (
        (c3_compute._write_c3_project, "rust:1-bookworm"),
        (c3_compute._write_warm_c3_project, "docker.io/ns/tig-swarm-warm-cpu:latest"),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            stage = _P(tmp)
            script = writer(
                stage,
                {"challenge": "knapsack", "c3_hardware": "auto",
                 "tracks": {"9": 4, "seed": "test"},
                 "track_starts": {"9": 6}},
                "https://example.invalid", "00:10:00", image,
            )
            runner = (stage / script).read_text()
            assert 'export TIG_TRACK_STARTS=' in runner, writer.__name__
            assert '{\\"9\\": 6}' in runner or '9\\": 6' in runner, runner
    # And absent track_starts => no export at all.
    with tempfile.TemporaryDirectory() as tmp:
        stage = _P(tmp)
        script = c3_compute._write_c3_project(
            stage, {"challenge": "knapsack", "c3_hardware": "auto"},
            "https://example.invalid", "00:10:00", "rust:1-bookworm",
        )
        assert "TIG_TRACK_STARTS" not in (stage / script).read_text()
    print("PASS test_runners_export_track_starts")


# ── shard-count policy: a slot must repay its provisioning ──────────

_CPU4 = {"timeout": 30, "c3_hardware": "cpu-e2-4vcpu-16gb"}


def _shards(total, cap=3, **over):
    return c3_compute._worthwhile_shards(total, {**_CPU4, **over}, cap)


def test_small_benchmark_runs_as_one_job():
    # The regression this policy exists for: 6 instances x 30s is ~60s of
    # solving on a 4-worker box. Three shards meant three provisions and three
    # builds to save half a minute — and starved both fleet-mates of a slot.
    assert _shards(6) == 1
    assert _shards(6, c3_warm_images=False) == 1


def test_large_benchmark_still_uses_the_whole_cap():
    assert _shards(200, timeout=60) == 3
    assert _shards(200, timeout=60, c3_warm_images=False) == 3
    assert _shards(2000, cap=10, timeout=60) == 10


def test_shards_scale_between_the_extremes():
    # 40 instances / 4 workers = 10 waves x 30s = 300s of solving. A second
    # warm box (60s) repays itself (saves 150s); a third (saves 50s) does not.
    assert _shards(40) == 2


def test_warm_images_shard_sooner_than_full_source():
    warm = _shards(40)
    cold = _shards(40, c3_warm_images=False)
    assert warm > cold, (warm, cold)


def test_never_more_shards_than_waves_or_instances():
    # 2 instances can't fill 3 boxes however cheap provisioning gets.
    assert _shards(2, c3_shard_fixed_secs=0.001) <= 2
    # 8 instances on 4 workers is 2 waves: a 3rd shard would idle cores.
    assert _shards(8, timeout=100000, c3_shard_fixed_secs=1) == 2


def test_gpu_still_shards_hard():
    # One solver per GPU job, so sharding IS the parallelism — the policy must
    # not de-shard GPU work the way it does a 4-worker CPU box.
    gpu = {"timeout": 60, "is_gpu": True, "c3_hardware": "l40"}
    assert c3_compute._worthwhile_shards(20, gpu, 3) == 3


def test_falls_back_to_the_cap_without_cost_inputs():
    # No per-instance timeout -> nothing to reason about; keep prior behaviour.
    assert c3_compute._worthwhile_shards(200, {"c3_hardware": "cpu-e2-4vcpu-16gb"}, 3) == 3
    # Fixed cost explicitly zeroed -> sharding is free, use the cap.
    assert _shards(200, c3_shard_fixed_secs=0) == 3


def test_explicit_bench_workers_and_hardware_are_honoured():
    assert c3_compute._hardware_vcpus("cpu-e2-48vcpu-192gb") == 48
    assert c3_compute._hardware_vcpus("l40") == 0
    assert c3_compute._estimate_shard_workers(
        {"c3_hardware": "cpu-e2-48vcpu-192gb"}) == 24
    assert c3_compute._estimate_shard_workers(
        {"c3_hardware": "cpu-e2-48vcpu-192gb", "bench_workers": 8}) == 8
    # A 48-vCPU box chews through the same work in far fewer waves, so it
    # needs fewer shards than a 4-vCPU one (shown against a cap high enough
    # that neither simply saturates it).
    big = _shards(200, cap=10, timeout=60, c3_hardware="cpu-e2-48vcpu-192gb")
    small = _shards(200, cap=10, timeout=60)
    assert big < small, (big, small)


def _main():
    test_small_benchmark_runs_as_one_job()
    test_large_benchmark_still_uses_the_whole_cap()
    test_shards_scale_between_the_extremes()
    test_warm_images_shard_sooner_than_full_source()
    test_never_more_shards_than_waves_or_instances()
    test_gpu_still_shards_hard()
    test_falls_back_to_the_cap_without_cost_inputs()
    test_explicit_bench_workers_and_hardware_are_honoured()
    print("PASS shard-count policy")
    test_balanced_sizes_examples()
    test_plan_shards_balanced_remainder()
    test_plan_shards_balanced_four_machines()
    test_plan_shards_exact_multiple()
    test_plan_shards_fewer_nonces_than_machines()
    test_plan_shards_packs_across_tracks()
    test_plan_shards_single_machine_is_single_job()
    test_plan_shards_skips_nonpositive_and_nonint()
    test_plan_shards_partitions_without_overlap_or_gap()
    test_plan_shards_empty()
    test_merge_concats_instances_and_unions_viz_and_errors()
    test_sharded_score_matches_unsharded()
    test_runners_export_track_starts()
    print("\nAll C3 sharding tests passed.")


if __name__ == "__main__":
    _main()
