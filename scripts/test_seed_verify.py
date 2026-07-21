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


def _http_error(code: int, body: bytes = b'{"detail":"Not Found"}') -> urllib.error.HTTPError:
    import io
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


# What Railway's edge returns while a service has no routable deployment.
EDGE_404 = (b'{"status":"error","code":404,"message":"Application not found",'
            b'"request_id":"ksOUYlgiSNinoBljLPU1MQ"}')


def _run(post_impl, deadline_s=90):
    orig_post, orig_time = S.post_json, S.time
    S.post_json, S.time = post_impl, _FakeTime()
    try:
        return S.verify_seed_pool("http://s", "key", SEEDS, deadline_s=deadline_s)
    finally:
        S.post_json, S.time = orig_post, orig_time


def test_platform_vs_app_error_classification():
    """The crux: a 404 from Railway's edge (transient, retry) must not read as
    a 404 from our own app (endpoint genuinely absent, give up)."""
    from hostadmin.http import classify_http_error, looks_like_platform_error

    assert looks_like_platform_error(EDGE_404.decode()) is True
    assert looks_like_platform_error('{"detail":"Not Found"}') is False
    assert looks_like_platform_error("") is True          # never our app
    assert looks_like_platform_error("<html>502</html>") is True

    def retryable(code, body):
        return classify_http_error(_http_error(code, body))[0]

    assert retryable(404, EDGE_404) is True               # edge mid-rollout
    assert retryable(404, b'{"detail":"Not Found"}') is False   # old image
    assert retryable(502, b"bad gateway") is True
    assert retryable(503, b"") is True
    assert retryable(401, b'{"detail":"bad admin key"}') is False
    assert retryable(400, b'{"detail":"algorithm_code is empty"}') is False
    print("PASS test_platform_vs_app_error_classification")


def test_verified_when_all_present():
    def post(url, payload, timeout=10):
        assert url.endswith("/api/admin/seeds"), url
        tag = {"knapsack": "greedy", "satisfiability": "local_search"}[payload["challenge"]]
        return {"seeds": [{"strategy_tag": tag, "source": "authored"}]}
    assert _run(post) == ([], True)
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
    assert _run(post) == ([], True)
    assert calls["deposits"] == 1, calls
    print("PASS test_redeposits_until_present")


def test_reports_missing_at_deadline():
    def post(url, payload, timeout=10):
        if url.endswith("/api/admin/seed_pool"):
            return {"seeded": True}  # deposit "succeeds" but never shows up
        return {"seeds": []}
    missing, verified = _run(post, deadline_s=5)
    assert missing == ["knapsack/greedy", "satisfiability/local_search"], missing
    assert verified is True, "the pool WAS readable — that's a real miss"
    print("PASS test_reports_missing_at_deadline")


def test_app_404_means_unverifiable_not_fatal():
    """A server predating /api/admin/seeds can't be verified — that must not
    fail create (resumed create against an old image), but it must report
    verified=False so no caller prints a success line."""
    def post(url, payload, timeout=10):
        raise _http_error(404)  # FastAPI's {"detail": ...} shape
    assert _run(post) == ([], False)
    print("PASS test_app_404_means_unverifiable_not_fatal")


def test_edge_404_is_retried_not_surrendered_to():
    """THE REGRESSION THIS FILE EXISTS FOR: Railway's edge 404s during a
    rollout. Read as 'server predates the endpoint', it returned a clean
    result over an untouched pool. It must instead be retried — and once the
    container is back, verify normally."""
    calls = {"n": 0}
    def post(url, payload, timeout=10):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise _http_error(404, EDGE_404)
        tag = {"knapsack": "greedy", "satisfiability": "local_search"}[payload["challenge"]]
        return {"seeds": [{"strategy_tag": tag, "source": "authored"}]}
    assert _run(post) == ([], True)
    assert calls["n"] > 3, "edge 404 must have been retried, not accepted"
    print("PASS test_edge_404_is_retried_not_surrendered_to")


def test_edge_404_throughout_is_missing_not_verified():
    """If the edge never comes back, the seeds are reported MISSING — never as
    a verified pool."""
    def post(url, payload, timeout=10):
        raise _http_error(404, EDGE_404)
    missing, verified = _run(post, deadline_s=5)
    assert len(missing) == 2, missing
    assert verified is False, "a read we never got back cannot verify anything"
    print("PASS test_edge_404_throughout_is_missing_not_verified")


def test_unreachable_reports_all_missing():
    def post(url, payload, timeout=10):
        raise urllib.error.URLError("down")
    missing, _ = _run(post, deadline_s=5)
    assert len(missing) == 2, missing
    print("PASS test_unreachable_reports_all_missing")


def test_hard_rejection_is_not_retried():
    """A bad admin key (401) must fail fast — retrying just repeats it."""
    calls = {"n": 0}
    def post(url, payload, timeout=10):
        calls["n"] += 1
        raise _http_error(401, b'{"detail":"bad admin key"}')
    missing, verified = _run(post, deadline_s=5)
    assert missing, missing
    assert verified is False, missing
    assert calls["n"] <= 2, f"401 retried {calls['n']}x — should fail fast"
    print("PASS test_hard_rejection_is_not_retried")


if __name__ == "__main__":
    test_platform_vs_app_error_classification()
    test_verified_when_all_present()
    test_redeposits_until_present()
    test_reports_missing_at_deadline()
    test_app_404_means_unverifiable_not_fatal()
    test_edge_404_is_retried_not_surrendered_to()
    test_edge_404_throughout_is_missing_not_verified()
    test_unreachable_reports_all_missing()
    test_hard_rejection_is_not_retried()
    print("ALL PASS")
