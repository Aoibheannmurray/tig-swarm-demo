"""benchmark._bounded_viz_data caps viz_data so it can't 422 the publish.

The server rejects a publish whose solution_data exceeds MAX_CODE_LEN (2MB).
Big challenges (VRP: up to 100 instances of route geometry) overflow that, so
viz_data is bounded to a byte budget. Runs standalone.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark as B


def test_large_challenge_is_capped_under_budget():
    # 100 VRP-ish instances, each ~1000 route points -> multiple MB total.
    results = [
        {"instance": f"n_nodes=600/{i}",
         "route_data": {"routes": [{"vehicle_id": 0, "path": [
             {"x": j, "y": j, "customer_id": j} for j in range(1000)]}]}}
        for i in range(100)
    ]
    viz = B._bounded_viz_data(results, "route_data")
    assert viz is not None
    assert len(json.dumps(viz)) <= B.VIZ_DATA_BUDGET
    assert 0 < len(viz) < 100  # some kept, some dropped
    # Kept instances are whole + unaltered (viz overlays them as-is).
    first = "n_nodes=600/0"
    assert first in viz and viz[first] == results[0]["route_data"]
    print("PASS test_large_challenge_is_capped_under_budget")


def test_small_challenge_keeps_every_instance():
    results = [{"instance": f"k/{i}", "knapsack_data": {"n": i}} for i in range(8)]
    viz = B._bounded_viz_data(results, "knapsack_data")
    assert viz is not None and len(viz) == 8
    print("PASS test_small_challenge_keeps_every_instance")


def test_no_extras_yields_none():
    results = [{"instance": "x/0", "score": 1.0}]  # no per_field present
    assert B._bounded_viz_data(results, "route_data") is None
    print("PASS test_no_extras_yields_none")


def test_single_huge_instance_still_kept():
    # An oversized FIRST instance is still included (the server backstop drops
    # it if truly unstorable) — the budget only gates the SECOND+ instance.
    results = [{"instance": "big/0",
                "route_data": {"path": [{"x": j} for j in range(200_000)]}}]
    viz = B._bounded_viz_data(results, "route_data")
    assert viz is not None and "big/0" in viz
    print("PASS test_single_huge_instance_still_kept")


def _main():
    test_large_challenge_is_capped_under_budget()
    test_small_challenge_keeps_every_instance()
    test_no_extras_yields_none()
    test_single_huge_instance_still_kept()
    print("\nAll viz-budget tests passed.")


if __name__ == "__main__":
    _main()
