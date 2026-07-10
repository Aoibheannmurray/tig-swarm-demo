"""Tests for hostadmin.swarm.verify_seed_pool — the create-time check that
authored seeds actually landed in the server's seed pool.

Why it exists: seed_pool_from_authored is best-effort per seed, and a POST
during the deploy's health-rollout window can land on a doomed container and
vanish. Three swarms in a row shipped with silently empty pools; create must
now read the pool back (POST /api/admin/seeds) and retry until verified.

Self-running: `python scripts/test_seed_verify.py`.
"""

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hostadmin import swarm as S  # noqa: E402

SEEDS = [
    {"challenge": "knapsack", "strategy_tag": "greedy",
     "algorithm_code": "// g\n", "kernel_code": None},
    {"challenge": "satisfiability", "strategy_tag": "local_search",
     "algorithm_code": "// ls\n", "kernel_code": None},
]


class _FakeTime:
    """Deterministic clock: sleep() advances it, no real waiting."""
    def __init__(self):
        self.now = 0.0
    def time(self):
        return self.now
    def sleep(self, s):
        self.now += s


def _http_error(code: int) -> urllib.error.HTTPError:
    import io
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b""))


def _run(post_impl, deadline_s=90):
    orig_post, orig_time = S.post_json, S.time
    S.post_json, S.time = post_impl, _FakeTime()
    try:
        return S.verify_seed_pool("http://s", "key", SEEDS, deadline_s=deadline_s)
    finally:
        S.post_json, S.time = orig_post, orig_time


def test_verified_when_all_present():
    def post(url, payload, timeout=10):
        assert url.endswith("/api/admin/seeds"), url
        tag = {"knapsack": "greedy", "satisfiability": "local_search"}[payload["challenge"]]
        return {"seeds": [{"strategy_tag": tag, "source": "authored"}]}
    assert _run(post) == []
    print("PASS test_verified_when_all_present")


def test_redeposits_until_present():
    """First read finds knapsack missing; the retry deposit 'lands' and the
    second read verifies. Harvested rows with the same tag must not count."""
    state = {"knapsack_present": False}
    calls = {"deposits": 0}
    def post(url, payload, timeout=10):
        if url.endswith("/api/admin/seed_pool"):
            calls["deposits"] += 1
            state["knapsack_present"] = True
            return {"seeded": True}
        if payload["challenge"] == "satisfiability":
            return {"seeds": [{"strategy_tag": "local_search", "source": "authored"}]}
        rows = [{"strategy_tag": "greedy", "source": "harvested"}]  # decoy
        if state["knapsack_present"]:
            rows.append({"strategy_tag": "greedy", "source": "authored"})
        return {"seeds": rows}
    assert _run(post) == []
    assert calls["deposits"] == 1, calls
    print("PASS test_redeposits_until_present")


def test_reports_missing_at_deadline():
    def post(url, payload, timeout=10):
        if url.endswith("/api/admin/seed_pool"):
            return {"seeded": True}  # deposit "succeeds" but never shows up
        return {"seeds": []}
    missing = _run(post, deadline_s=5)
    assert missing == ["knapsack/greedy", "satisfiability/local_search"], missing
    print("PASS test_reports_missing_at_deadline")


def test_404_means_unverifiable_not_fatal():
    """A server predating /api/admin/seeds can't be verified — that must not
    fail create (resumed create against an old image)."""
    def post(url, payload, timeout=10):
        raise _http_error(404)
    assert _run(post) == []
    print("PASS test_404_means_unverifiable_not_fatal")


def test_unreachable_reports_all_missing():
    def post(url, payload, timeout=10):
        raise urllib.error.URLError("down")
    missing = _run(post, deadline_s=5)
    assert len(missing) == 2, missing
    print("PASS test_unreachable_reports_all_missing")


if __name__ == "__main__":
    test_verified_when_all_present()
    test_redeposits_until_present()
    test_reports_missing_at_deadline()
    test_404_means_unverifiable_not_fatal()
    test_unreachable_reports_all_missing()
    print("ALL PASS")
