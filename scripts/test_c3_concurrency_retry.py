"""Tests for the C3 concurrent-chip-limit retry (c3_compute._run_one_c3_job_inner).

The fleet slot pool (c3_pool.py) keeps THIS fleet's live C3 jobs under the plan
cap, but a `c3 deploy` can still be rejected with

    API error 429 (CONCURRENCY_LIMIT): Concurrency limit reached: you have 3
    concurrent chips (queued or running). ...

whenever C3's real-time chip count momentarily exceeds the cap — teardown lag
on a just-released sibling chip, a manual `c3` job outside the pool, or a pool
sized above the account's true tier. That 429 says "No new job was queued", so
nothing is orphaned and a chip WILL free: the deploy must WAIT and retry, not
fail the benchmark. These tests pin that behaviour.

Self-running: `python scripts/test_c3_concurrency_retry.py`.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_compute as cc  # noqa: E402

CONCURRENCY_429 = (
    "Error: submit job: API error 429 (CONCURRENCY_LIMIT): Concurrency limit "
    "reached: you have 3 concurrent chips (queued or running). You are at the "
    "maximum concurrent chip count for your current Free subscription tier (3). "
    "No new job was queued. Wait for a job to finish or cancel one with "
    "'c3 cancel <job_id>'."
)
DEPLOY_OK = 'Deployed job "tig-knapsack-abc123"\n{"id": "job_1700000000000_ab12"}'


class _FakeProc:
    def __init__(self, output: str, returncode: int):
        self.stdout = iter(output.splitlines())
        self.returncode = returncode

    def wait(self):
        return self.returncode


def _args():
    return argparse.Namespace(
        c3_provider=None, c3_time="00:10:00",
        c3_poll_timeout=None, c3_cancel_on_timeout=False,
    )


def _install_stubs(monkey, deploy_outputs):
    """Patch out everything downstream of a successful deploy so the test
    exercises only the deploy retry loop. `deploy_outputs` is a list of
    (output, returncode) the fake `c3 deploy` returns in order. Returns a dict
    recording the calls and sleeps."""
    rec = {"popens": 0, "sleeps": []}

    def fake_popen(cmd, **kw):
        i = min(rec["popens"], len(deploy_outputs) - 1)
        out, rc = deploy_outputs[i]
        rec["popens"] += 1
        return _FakeProc(out, rc)

    monkey.append((cc.subprocess, "Popen", cc.subprocess.Popen))
    monkey.append((cc.time, "sleep", cc.time.sleep))
    cc.subprocess.Popen = fake_popen
    cc.time.sleep = lambda s: rec["sleeps"].append(s)

    # Short-circuit the post-deploy path to an immediate clean completion.
    for name, fn in (
        ("_poll_c3_job", lambda *a, **k: "completed"),
        ("_pull_artifacts", lambda *a, **k: None),
        ("_load_benchmark_json", lambda *a, **k: ({"tracks": {}}, "")),
        ("_read_benchmark_stderr", lambda *a, **k: ""),
    ):
        monkey.append((cc, name, getattr(cc, name)))
        setattr(cc, name, fn)
    return rec


def _restore(monkey):
    for obj, name, orig in reversed(monkey):
        setattr(obj, name, orig)


def test_concurrency_429_waits_then_succeeds():
    monkey = []
    # Two concurrency rejections, then the deploy lands.
    rec = _install_stubs(monkey, [
        (CONCURRENCY_429, 1), (CONCURRENCY_429, 1), (DEPLOY_OK, 0),
    ])
    try:
        bench, err = cc._run_one_c3_job_inner(
            _args(), env={}, stage=Path("."), label="0",
        )
    finally:
        _restore(monkey)
    assert err == "", err
    assert bench == {"tracks": {}}, bench
    assert rec["popens"] == 3, rec["popens"]          # retried, did not fail
    assert len(rec["sleeps"]) == 2, rec["sleeps"]     # one wait per rejection
    # Waits are the patient chip-free interval, NOT the sub-20s upload backoff.
    assert all(cc._CONCURRENCY_RETRY_MIN_SECS <= s <= cc._CONCURRENCY_RETRY_MAX_SECS
               for s in rec["sleeps"]), rec["sleeps"]
    print("PASS test_concurrency_429_waits_then_succeeds")


def test_concurrency_429_gives_up_after_budget():
    """A chip that never frees must surface a clear, actionable error once the
    wait budget is spent — not park forever and not fail instantly."""
    monkey = []
    # Call for the side effect (installing the stubs); the recorder is unused
    # here — this test asserts on the surfaced error, not on the calls made.
    _install_stubs(monkey, [(CONCURRENCY_429, 1)])  # always rejected
    # Drive monotonic time forward so the deadline is crossed on the 2nd check.
    ticks = iter([0.0, 0.0, cc._CONCURRENCY_MAX_WAIT_SECS + 1.0])
    monkey.append((cc.time, "monotonic", cc.time.monotonic))
    cc.time.monotonic = lambda: next(ticks, cc._CONCURRENCY_MAX_WAIT_SECS + 1.0)
    try:
        bench, err = cc._run_one_c3_job_inner(
            _args(), env={}, stage=Path("."), label="0",
        )
    finally:
        _restore(monkey)
    assert bench is None, bench
    assert "concurrent-chip limit still reached" in err, err
    assert "c3 squeue" in err and "c3 cancel" in err, err  # tells the user what to do
    print("PASS test_concurrency_429_gives_up_after_budget")


def test_object_store_throttle_still_short_backoff():
    """The OTHER 429 (same-object upload throttle) keeps its fast sub-20s
    backoff and its own attempt cap — the concurrency path must not swallow it."""
    monkey = []
    throttle = "c3 deploy: status 429: Reduce your concurrent request rate for the same object"
    rec = _install_stubs(monkey, [(throttle, 1), (DEPLOY_OK, 0)])
    try:
        bench, err = cc._run_one_c3_job_inner(
            _args(), env={}, stage=Path("."), label="0",
        )
    finally:
        _restore(monkey)
    assert err == "" and bench == {"tracks": {}}, (err, bench)
    assert rec["popens"] == 2, rec["popens"]
    assert len(rec["sleeps"]) == 1, rec["sleeps"]
    assert rec["sleeps"][0] <= cc._DEPLOY_BACKOFF_CAP_SECS, rec["sleeps"]
    print("PASS test_object_store_throttle_still_short_backoff")


def test_non_retryable_failure_still_fails_fast():
    monkey = []
    rec = _install_stubs(monkey, [("error[E0425]: cannot find value `foo`", 1)])
    try:
        bench, err = cc._run_one_c3_job_inner(
            _args(), env={}, stage=Path("."), label="0",
        )
    finally:
        _restore(monkey)
    assert bench is None and "c3 deploy failed" in err, (bench, err)
    assert rec["popens"] == 1, rec["popens"]          # no retry
    assert rec["sleeps"] == [], rec["sleeps"]
    print("PASS test_non_retryable_failure_still_fails_fast")


if __name__ == "__main__":
    test_concurrency_429_waits_then_succeeds()
    test_concurrency_429_gives_up_after_budget()
    test_object_store_throttle_still_short_backoff()
    test_non_retryable_failure_still_fails_fast()
    print("ALL PASS")
