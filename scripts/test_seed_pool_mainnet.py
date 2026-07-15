"""seed_pool_from_mainnet deposits each challenge's top mainnet algorithm into
the SEED pool via /api/admin/seed_pool, multi-file aware. The mainnet fetch and
the HTTP POST are stubbed so the test is offline + deterministic.

Runs standalone (`python scripts/test_seed_pool_mainnet.py`).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hostadmin.swarm as sw  # noqa: E402


def _run_with_stubs(reshaped, capture):
    orig_reshape = sw._reshaped_mainnet_algo
    orig_post = sw.post_json
    sw._reshaped_mainnet_algo = lambda ch: reshaped.get(
        ch, (None, "no compiled mainnet algorithm found"))
    sw.post_json = lambda url, payload, timeout=10: (
        capture.append((url, payload)) or {"action": "inserted", "seeded": True})
    try:
        return sw.seed_pool_from_mainnet("https://s.invalid", "KEY",
                                         set(reshaped.keys()) | {"job_scheduling"})
    finally:
        sw._reshaped_mainnet_algo = orig_reshape
        sw.post_json = orig_post


def test_deposits_multifile_mainnet_to_seed_pool():
    files = {"mod.rs": "// entry", "helpers.rs": "// h", "k.cu": "// kern"}
    reshaped = {
        "knapsack": ({"algo_name": "topalgo", "adoption": 42 * 10**16,
                      "code_files": files, "kernel_code": None}, ""),
    }
    capture: list = []
    failed = _run_with_stubs(reshaped, capture)

    assert failed == [], failed  # job_scheduling had no mainnet -> skipped, not failed
    assert len(capture) == 1, capture
    url, payload = capture[0]
    assert url.endswith("/api/admin/seed_pool"), url
    assert payload["challenge"] == "knapsack"
    assert payload["strategy_tag"] == "mainnet"
    assert payload["algorithm_files"] == files       # full map deposited
    assert payload["algorithm_code"] == files["mod.rs"]
    assert payload["admin_key"] == "KEY"
    print("PASS test_deposits_multifile_mainnet_to_seed_pool")


def test_skips_challenge_with_no_mainnet_algo():
    capture: list = []
    failed = _run_with_stubs({}, capture)  # nothing reshapes
    assert capture == []       # nothing deposited
    assert failed == []        # a skip is not a failure
    print("PASS test_skips_challenge_with_no_mainnet_algo")


def test_http_failure_is_reported_not_raised():
    import urllib.error
    files = {"mod.rs": "// entry"}
    reshaped = {"knapsack": ({"algo_name": "a", "adoption": 1,
                              "code_files": files, "kernel_code": None}, "")}
    orig_reshape = sw._reshaped_mainnet_algo
    orig_post = sw.post_json

    def boom(url, payload, timeout=10):
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    sw._reshaped_mainnet_algo = lambda ch: reshaped.get(ch, (None, "none"))
    sw.post_json = boom
    try:
        failed = sw.seed_pool_from_mainnet("https://s.invalid", "KEY", {"knapsack"})
    finally:
        sw._reshaped_mainnet_algo = orig_reshape
        sw.post_json = orig_post
    assert failed == ["knapsack/mainnet"], failed  # reported, not raised
    print("PASS test_http_failure_is_reported_not_raised")


def _main():
    test_deposits_multifile_mainnet_to_seed_pool()
    test_skips_challenge_with_no_mainnet_algo()
    test_http_failure_is_reported_not_raised()
    print("\nAll seed-pool-mainnet tests passed.")


if __name__ == "__main__":
    _main()
