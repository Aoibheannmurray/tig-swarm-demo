"""Tests for C3 pools that accept a job and never run it (or kill it silently).

Linked behaviours:
  * `_poll_c3_job` gives up on a job stuck in a pre-run state instead of
    waiting out the walltime-derived timeout (2h45m at the default c3_time);
  * `_poll_c3_job` samples the job's logs for C3's internal-error banner
    ("Contact C3 support … Code: C3-JOB-…"), which can appear while squeue
    still reports the job RUNNING — the job is dead but never goes terminal;
  * an auto-selected profile that does either is dropped from later
    auto-picks, so the agent moves to a working pool by itself;
  * a run of C3 failures prints the switch-to-local-compute advice once.

Self-running: `python scripts/test_c3_queue_stall.py`.
"""

import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import c3_compute as cc  # noqa: E402


def _fake_clock():
    """A monotonic() that jumps 60s per call, so a 20-minute grace elapses in
    a handful of polls instead of 20 real minutes."""
    t = [0.0]

    def now():
        t[0] += 60.0
        return t[0]
    return now


def _quiet_logs():
    """Stub _read_logs: the poll loop samples logs for the internal-error
    banner, and these tests must not shell out to a real c3 CLI."""
    orig = cc._read_logs
    cc._read_logs = lambda *a, **k: ""
    return orig


def test_stuck_in_queue_gives_up_early():
    orig_mono, orig_sleep, orig_status = time.monotonic, time.sleep, cc._read_job_status
    orig_logs = _quiet_logs()
    polls = []
    try:
        time.monotonic = _fake_clock()
        time.sleep = lambda s: None
        cc._read_job_status = lambda *a, **k: polls.append(1) or "QUEUED"
        buf = io.StringIO()
        with redirect_stdout(buf):
            # walltime 2h → the old timeout would be 9900s of polling.
            status = cc._poll_c3_job("job_1", {}, ROOT, 7200)
        assert status == "never_started", status
        assert "without starting" in buf.getvalue(), buf.getvalue()
        # Bounded by the grace (~20 min of fake time), not the 2h45m timeout.
        assert len(polls) < 45, len(polls)
    finally:
        time.monotonic, time.sleep, cc._read_job_status = orig_mono, orig_sleep, orig_status
        cc._read_logs = orig_logs
    print("PASS test_stuck_in_queue_gives_up_early")


def test_a_job_that_starts_keeps_the_full_timeout():
    """Reaching RUNNING clears the queue guard — a long solve must not be
    mistaken for a pool that never provisioned."""
    orig_mono, orig_sleep, orig_status = time.monotonic, time.sleep, cc._read_job_status
    orig_logs = _quiet_logs()
    seq = ["QUEUED", "QUEUED", "RUNNING"] + ["RUNNING"] * 60 + ["COMPLETED"]
    try:
        time.monotonic = _fake_clock()
        time.sleep = lambda s: None
        cc._read_job_status = lambda *a, **k: seq.pop(0) if seq else "COMPLETED"
        with redirect_stdout(io.StringIO()):
            status = cc._poll_c3_job("job_2", {}, ROOT, 7200)
        assert status == "completed", status
    finally:
        time.monotonic, time.sleep, cc._read_job_status = orig_mono, orig_sleep, orig_status
        cc._read_logs = orig_logs
    print("PASS test_a_job_that_starts_keeps_the_full_timeout")


def test_queue_grace_is_configurable_and_disablable():
    assert cc._queue_grace_secs(None) == cc._QUEUE_GRACE_SECS
    assert cc._queue_grace_secs({"c3_queue_grace_secs": 300}) == 300
    assert cc._queue_grace_secs({"c3_queue_grace_secs": 0}) == 0
    assert cc._queue_grace_secs({"c3_queue_grace_secs": "nonsense"}) == cc._QUEUE_GRACE_SECS
    import os
    os.environ["TIG_C3_QUEUE_GRACE"] = "60"
    try:
        assert cc._queue_grace_secs({"c3_queue_grace_secs": 300}) == 60, "env wins"
    finally:
        del os.environ["TIG_C3_QUEUE_GRACE"]

    # 0 disables: the job stays queued forever and we wait, as before.
    orig_mono, orig_sleep, orig_status = time.monotonic, time.sleep, cc._read_job_status
    orig_logs = _quiet_logs()
    try:
        time.monotonic = _fake_clock()
        time.sleep = lambda s: None
        cc._read_job_status = lambda *a, **k: "QUEUED"
        with redirect_stdout(io.StringIO()):
            status = cc._poll_c3_job("job_3", {}, ROOT, 60,
                                     cfg={"c3_queue_grace_secs": 0})
        assert status == "timeout", status
    finally:
        time.monotonic, time.sleep, cc._read_job_status = orig_mono, orig_sleep, orig_status
        cc._read_logs = orig_logs
    print("PASS test_queue_grace_is_configurable_and_disablable")


def test_support_error_code_parsing():
    banner = ("The job could not be completed. Contact C3 support with the "
              "error code below. Code: C3-JOB-531022R88XJW.")
    assert cc._support_error_code(banner) == "C3-JOB-531022R88XJW"
    # Banner without a parseable code still counts as an infra error.
    assert cc._support_error_code(
        "The job could not be completed. Contact C3 support.") == "unknown"
    # Benign logs — including our own runner/benchmark noise — never match.
    for benign in ("", "Compiling tig-swarm v0.1.0", "error[E0599]: no method",
                   "solved 5/5 instances"):
        assert cc._support_error_code(benign) is None, benign
    print("PASS test_support_error_code_parsing")


def test_internal_error_banner_fails_fast_despite_running_status():
    """The reported failure mode: the job errored on C3 (support-code banner
    in its logs) but squeue kept saying RUNNING, so the swarm sat on it as if
    it were still working. The log sampler must catch it within a few polls."""
    orig_mono, orig_sleep = time.monotonic, time.sleep
    orig_status, orig_logs = cc._read_job_status, cc._read_logs
    log_reads = []
    try:
        time.monotonic = _fake_clock()
        time.sleep = lambda s: None
        cc._read_job_status = lambda *a, **k: "RUNNING"
        cc._read_logs = lambda *a, **k: log_reads.append(1) or (
            "The job could not be completed. Contact C3 support with the "
            "error code below. Code: C3-JOB-531022R88XJW.")
        buf = io.StringIO()
        with redirect_stdout(buf):
            status = cc._poll_c3_job("job_4", {}, ROOT, 7200)
        assert status == "infra_error:C3-JOB-531022R88XJW", status
        assert len(log_reads) == 1, log_reads  # first sample already caught it
        assert "C3 internal error" in buf.getvalue(), buf.getvalue()
    finally:
        time.monotonic, time.sleep = orig_mono, orig_sleep
        cc._read_job_status, cc._read_logs = orig_status, orig_logs
    print("PASS test_internal_error_banner_fails_fast_despite_running_status")


def test_dead_pool_is_dropped_from_auto_picks():
    """The reported failure mode: auto picked the 48-vCPU box, the job hung,
    and the contributor had to switch hardware by hand. The session blocklist
    makes the next benchmark pick something else on its own."""
    orig = set(cc._session_hw_blocklist)
    orig_cache = cc._hw_cache
    try:
        cc._session_hw_blocklist.clear()
        cc._hw_cache = (time.monotonic(), [
            {"name": "cpu-e2-48vcpu-192gb", "tier": 2, "vcpu": 48, "rate": 1.15},
        ])
        with redirect_stdout(io.StringIO()):
            cc._blocklist_profile_for_session("cpu-e2-48vcpu-192gb", "test")
        assert "cpu-e2-48vcpu-192gb" in cc._hw_blocklist(None)
        # The cached listing is dropped too — a freshly-blocked pool should
        # trigger a re-query, not ride out the 10-minute TTL.
        assert cc._hw_cache is None
        # Still merged with a per-fleet standing blocklist.
        merged = cc._hw_blocklist({"c3_hardware_blocklist": "cpu-e2-4vcpu-16gb"})
        assert {"cpu-e2-48vcpu-192gb", "cpu-e2-4vcpu-16gb"} <= merged, merged
    finally:
        cc._session_hw_blocklist.clear()
        cc._session_hw_blocklist.update(orig)
        cc._hw_cache = orig_cache
    print("PASS test_dead_pool_is_dropped_from_auto_picks")


def test_nothing_is_blocklisted_out_of_the_box():
    """No hardcoded bad pools: a shipped list goes stale in both directions —
    it keeps avoiding profiles C3 has fixed and can't know about the one that
    breaks tomorrow. Everything is learned per session instead."""
    assert cc._DEFAULT_HW_BLOCKLIST == frozenset(), cc._DEFAULT_HW_BLOCKLIST
    orig = set(cc._session_hw_blocklist)
    try:
        cc._session_hw_blocklist.clear()
        assert cc._hw_blocklist(None) == frozenset()
        assert cc._hw_blocklist({}) == frozenset()
    finally:
        cc._session_hw_blocklist.clear()
        cc._session_hw_blocklist.update(orig)
    print("PASS test_nothing_is_blocklisted_out_of_the_box")


def test_only_the_pool_s_own_failures_are_learned_from():
    """The discriminator: did our benchmark produce any output?

    A cargo error from LLM-authored code writes to benchmark.stderr and must
    never cost a profile its place — otherwise a few bad edits would blocklist
    every box on the account. A job that dies with nothing written never ran
    the code at all, which is the pool's fault."""
    import inspect
    src = inspect.getsource(cc._run_one_c3_job_inner)
    # The blocklist call for a hard failure is guarded on BOTH an empty
    # benchmark.stderr and an auto-selected profile.
    guard = 'status == "failed" and not bench_stderr'
    assert guard in src, "hard-failure blocklisting must require empty stderr"
    idx = src.index(guard)
    assert 'c3_hardware_auto' in src[idx:idx + 200], (
        "a pinned profile is the contributor's choice — never auto-dropped"
    )
    print("PASS test_only_the_pool_s_own_failures_are_learned_from")


def test_failure_streak_suggests_local_compute():
    orig_n, orig_shown = cc._consecutive_c3_failures, cc._c3_advice_shown
    try:
        cc._consecutive_c3_failures, cc._c3_advice_shown = 0, False

        def note(ok):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cc._note_c3_outcome(ok, "vehicle_routing")
            return buf.getvalue()

        # Quiet while it still looks like a blip.
        for _ in range(cc._C3_TROUBLE_STREAK - 1):
            assert note(False) == ""
        advice = note(False)
        assert "compute" in advice and "local" in advice, advice
        assert "fleet.config.json" in advice, advice
        assert "vehicle_routing" in advice, advice
        # Once per streak, not once per failure.
        assert note(False) == ""
        # A success clears it, and a later streak advises again.
        assert note(True) == ""
        for _ in range(cc._C3_TROUBLE_STREAK - 1):
            assert note(False) == ""
        assert "local" in note(False)
    finally:
        cc._consecutive_c3_failures, cc._c3_advice_shown = orig_n, orig_shown
    print("PASS test_failure_streak_suggests_local_compute")


if __name__ == "__main__":
    test_stuck_in_queue_gives_up_early()
    test_a_job_that_starts_keeps_the_full_timeout()
    test_queue_grace_is_configurable_and_disablable()
    test_support_error_code_parsing()
    test_internal_error_banner_fails_fast_despite_running_status()
    test_dead_pool_is_dropped_from_auto_picks()
    test_nothing_is_blocklisted_out_of_the_box()
    test_only_the_pool_s_own_failures_are_learned_from()
    test_failure_streak_suggests_local_compute()
    print("ALL PASS")
