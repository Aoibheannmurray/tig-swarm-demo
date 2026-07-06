"""Tests for distributed (nonce-sharded) C3 benchmarking.

Runs standalone (`python3 test_c3_sharding.py` from the scripts dir) — no
network / no C3. Covers the pure logic added in c3_compute.py:

  * `_plan_shards` bin-packs all tracks' nonces into shards of at most
    `shard_size`, packing across track boundaries (e.g. 17 @ 8 ->
    [9[0:8]],[9[8:16]],[9[16:17]]; {9:10,16:8} @ 8 packs 9's tail with 16 into
    one shard), ignores the `seed` key, and collapses to a single shard when
    sharding is disabled.
  * `_merge_combined` reassembles per-shard `combined.json` fragments into one
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


def test_plan_shards_remainder():
    shards = c3_compute._plan_shards({"9": 17}, 8)
    assert _slices(shards, "9") == [(0, 8), (8, 8), (16, 1)], shards
    assert len(shards) == 3
    print("PASS test_plan_shards_remainder")


def test_plan_shards_exact_multiple():
    shards = c3_compute._plan_shards({"16": 16}, 8)
    assert _slices(shards, "16") == [(0, 8), (8, 8)], shards
    assert _shard_totals(shards) == [8, 8]
    print("PASS test_plan_shards_exact_multiple")


def test_plan_shards_packs_across_tracks():
    # {9:10, 16:8} @ 8 -> [9[0:8]], [9[8:10]+16[0:6]], [16[6:8]] : 3 shards,
    # not the 2+1=3 you'd get one-per-track... but the MIDDLE shard mixes tracks.
    shards = c3_compute._plan_shards({"9": 10, "16": 8, "seed": 12345}, 8)
    assert _slices(shards, "9") == [(0, 8), (8, 2)]
    assert _slices(shards, "16") == [(0, 6), (6, 2)]
    assert all(s["track_key"] != "seed" for shard in shards for s in shard)
    # ceil(18 / 8) = 3 shards, each <= 8 nonces, and one shard carries 2 tracks.
    assert len(shards) == 3
    assert _shard_totals(shards) == [8, 8, 2]
    assert any(len({s["track_key"] for s in shard}) > 1 for shard in shards)
    print("PASS test_plan_shards_packs_across_tracks")


def test_plan_shards_disabled_is_single_job():
    for size in (0, -1):
        shards = c3_compute._plan_shards({"9": 17, "16": 5}, size)
        assert len(shards) == 1, (size, shards)  # one job for everything
        assert _slices(shards, "9") == [(0, 17)], (size, shards)
        assert _slices(shards, "16") == [(0, 5)], (size, shards)
    print("PASS test_plan_shards_disabled_is_single_job")


def test_plan_shards_skips_nonpositive_and_nonint():
    shards = c3_compute._plan_shards({"9": 0, "16": -3, "x": "nope", "8": 3}, 8)
    assert _slices(shards, "8") == [(0, 3)]
    assert len(shards) == 1
    print("PASS test_plan_shards_skips_nonpositive_and_nonint")


def test_plan_shards_partitions_without_overlap_or_gap():
    total = 25
    shards = c3_compute._plan_shards({"9": total}, 8)
    covered = []
    for shard in shards:
        for s in shard:
            covered.extend(range(s["start"], s["start"] + s["count"]))
    assert covered == list(range(total)), covered  # contiguous, ordered, no dupes
    assert all(t <= 8 for t in _shard_totals(shards))
    print("PASS test_plan_shards_partitions_without_overlap_or_gap")


# ── _merge_combined + score parity ─────────────────────────────────


def _combined(track_records: dict, challenge="knapsack", fuel=1000):
    """Build a driver-shaped combined.json dict from {track: [nonce_records]}."""
    return {
        "challenge": challenge,
        "fuel_limit": fuel,
        "tracks": {t: {"nonces": recs} for t, recs in track_records.items()},
    }


def _rec(nonce, quality, feasible=True):
    return {"nonce": nonce, "feasible": feasible, "quality": quality}


def test_merge_reassembles_nonces_in_order():
    # A 5-nonce track split into shards of 2,2,1 (as _plan_shards would).
    all_recs = [_rec(i, 1.0 + i) for i in range(5)]
    shard_results = [
        _combined({"9": all_recs[0:2]}),
        _combined({"9": all_recs[2:4]}),
        _combined({"9": all_recs[4:5]}),
    ]
    merged = c3_compute._merge_combined(shard_results, "knapsack", 1000)
    got = merged["tracks"]["9"]["nonces"]
    assert [r["nonce"] for r in got] == [0, 1, 2, 3, 4], got
    assert got == all_recs
    print("PASS test_merge_reassembles_nonces_in_order")


def test_merge_multiple_tracks_and_skips_missing():
    shard_results = [
        _combined({"9": [_rec(0, 1.0)]}),
        None,  # a failed shard contributes nothing (orchestrator fails, but merge is defensive)
        _combined({"16": [_rec(0, 2.0)]}),
    ]
    merged = c3_compute._merge_combined(shard_results, "knapsack", 1000)
    assert len(merged["tracks"]["9"]["nonces"]) == 1
    assert len(merged["tracks"]["16"]["nonces"]) == 1
    print("PASS test_merge_multiple_tracks_and_skips_missing")


def test_merge_propagates_track_error():
    shard_results = [_combined({"9": [_rec(0, 1.0)]})]
    shard_results[0]["tracks"]["9"]["error"] = "boom"
    merged = c3_compute._merge_combined(shard_results, "knapsack", 1000)
    assert merged["tracks"]["9"].get("error") == "boom"
    print("PASS test_merge_propagates_track_error")


def test_sharded_score_matches_unsharded():
    cfg = {"challenge": "knapsack"}
    # Reference: one unsharded combined with 17 nonces across two tracks.
    t9 = [_rec(i, 1.0 + 0.1 * i, feasible=(i % 4 != 0)) for i in range(17)]
    t16 = [_rec(i, 2.0 - 0.05 * i) for i in range(8)]
    unsharded = _combined({"9": t9, "16": t16})
    ref = benchmark._tig_adapter(unsharded, cfg)

    # Same nonces, but bin-packed exactly how _plan_shards(size=8) lays them out
    # — including shards that mix both tracks — then each shard's combined.json
    # is assembled from its slices.
    recs_by_track = {"9": t9, "16": t16}
    shards = c3_compute._plan_shards({"9": len(t9), "16": len(t16)}, 8)
    assert any(len({s["track_key"] for s in shard}) > 1 for shard in shards), \
        "expected at least one shard mixing tracks"
    shard_results = []
    for shard in shards:
        track_records = {}
        for s in shard:
            window = recs_by_track[s["track_key"]][s["start"]: s["start"] + s["count"]]
            track_records[s["track_key"]] = window
        shard_results.append(_combined(track_records))
    merged = c3_compute._merge_combined(shard_results, "knapsack", unsharded["fuel_limit"])
    got = benchmark._tig_adapter(merged, cfg)

    assert got["score"] == ref["score"], (got["score"], ref["score"])
    assert got["instances_solved"] == ref["instances_solved"] == 25
    assert got["instances_feasible"] == ref["instances_feasible"]
    assert got["track_scores"] == ref["track_scores"]
    print("PASS test_sharded_score_matches_unsharded")


def _main():
    test_plan_shards_remainder()
    test_plan_shards_exact_multiple()
    test_plan_shards_packs_across_tracks()
    test_plan_shards_disabled_is_single_job()
    test_plan_shards_skips_nonpositive_and_nonint()
    test_plan_shards_partitions_without_overlap_or_gap()
    test_merge_reassembles_nonces_in_order()
    test_merge_multiple_tracks_and_skips_missing()
    test_merge_propagates_track_error()
    test_sharded_score_matches_unsharded()
    print("\nAll C3 sharding tests passed.")


if __name__ == "__main__":
    _main()
