"""_parse_c3_id must return C3's job ID, never the job NAME.

Deploy output prints `job  tig-<challenge>-<run_id>` above `id  job_<...>`.
On the job_scheduling challenge the name itself contains a `job_…` token, so a
loose first-match pattern returned `job_scheduling-<run_id>` — an ID C3 has
never heard of. Polling then never saw a terminal status, held its fleet pool
slot for the whole timeout, and never pulled the finished job's benchmark.json.

Run directly: python scripts/test_c3_job_id.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_compute
from c3_compute import _parse_c3_id, _poll_c3_job

REAL_ID = "job_1784803063125_x6wm3l"


def _deploy_output(challenge: str) -> str:
    return f"""
    ━━ flight plan
    job          tig-{challenge}-3aae77f7b5
    project      tig-swarm-benchmark
    hardware     cpu-e2-4vcpu-16gb

    ━━ job sheet
    id           {REAL_ID} (PENDING)
    """


def test_job_scheduling_name_does_not_shadow_the_id():
    got = _parse_c3_id(_deploy_output("job_scheduling"))
    assert got == REAL_ID, f"expected {REAL_ID}, got {got}"


def test_other_challenges_still_parse():
    for challenge in (
        "vehicle_routing", "knapsack", "satisfiability", "energy_arbitrage",
        "hypergraph", "neuralnet_optimizer", "vector_search",
    ):
        got = _parse_c3_id(_deploy_output(challenge))
        assert got == REAL_ID, f"{challenge}: expected {REAL_ID}, got {got}"


def test_json_id_still_wins():
    text = f'prelude tig-job_scheduling-abc {{"job_id": "{REAL_ID}"}}'
    assert _parse_c3_id(text) == REAL_ID


def test_no_id_returns_none():
    assert _parse_c3_id("Error: submit job: API error 402") is None


# ── polling: an ID C3 never recognises must fail fast, not wait out the timeout


class _FakeClock:
    """Monotonic time we control, so the grace period is exercised without
    actually sleeping through it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def _poll_with(statuses, wait_secs=9000):
    """Run _poll_c3_job against a scripted status sequence on a fake clock.
    `statuses` is consumed one per poll; it repeats its last value forever."""
    clock = _FakeClock()
    seq = list(statuses)
    calls = {"n": 0}

    def fake_read(job_id, env, cwd):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    orig = (c3_compute.time.monotonic, c3_compute.time.sleep,
            c3_compute._read_job_status)
    c3_compute.time.monotonic = clock.monotonic
    c3_compute.time.sleep = clock.sleep
    c3_compute._read_job_status = fake_read
    try:
        result = _poll_c3_job(REAL_ID, {}, Path("."), 0, wait_secs)
    finally:
        (c3_compute.time.monotonic, c3_compute.time.sleep,
         c3_compute._read_job_status) = orig
    return result, calls["n"], clock.now - 1000.0


def test_never_recognised_gives_up_within_the_grace_period():
    result, _, elapsed = _poll_with([None])
    assert result == "unrecognised", result
    grace = c3_compute._UNRECOGNISED_JOB_GRACE_SECS
    assert elapsed <= grace + c3_compute._POLL_INTERVAL_SECS, elapsed


def test_a_job_that_resolves_once_keeps_the_full_timeout():
    # RUNNING for far longer than the grace period, then COMPLETED: the
    # early-exit must not fire on a genuinely slow job.
    slow = ["RUNNING"] * 200 + ["COMPLETED"]
    result, _, elapsed = _poll_with(slow)
    assert result == "completed", result
    assert elapsed > c3_compute._UNRECOGNISED_JOB_GRACE_SECS, elapsed


def test_transient_unknown_after_a_resolve_does_not_trip_it():
    result, _, _ = _poll_with(["RUNNING", None, None, None, "SUCCEEDED"])
    assert result == "completed", result


def test_terminal_bad_still_fails():
    result, _, _ = _poll_with(["RUNNING", "FAILED"])
    assert result == "failed", result


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("all c3 job-id parsing tests passed")
