"""Tests for dynamic C3 CPU hardware selection (c3_compute._best_cpu_hardware).

Runs standalone (`python test_c3_hardware.py` from the scripts dir) and is
pytest-compatible. The c3 CLI is stubbed via c3_compute._run_c3.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3_compute


def _profile(name, vcpu, tier, available, rate):
    return {
        "hardware_profile": name, "hardware_kind": "cpu", "vcpu": vcpu,
        "availability_tier": tier, "available": available,
        "rate_per_hour_gbp": rate,
    }


def _listing(*profiles):
    payload = json.dumps({"hardware": [{"profiles": list(profiles)}]})
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")


def _with_listing(result):
    """Point _run_c3 at a canned listing and clear the cache."""
    c3_compute._hw_cache = None
    c3_compute._run_c3 = lambda *a, **k: result


def test_picks_largest_available_without_workload():
    # With nothing to cost against (no instance count / timeout), the old
    # ranking applies: the largest trustworthy box.
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(
            _profile("cpu-d3-4vcpu-16gb", 4, "medium", True, 0.11),
            _profile("cpu-e2-48vcpu-192gb", 48, "medium", True, 1.15),
        ))
        assert c3_compute._best_cpu_hardware({}) == "cpu-e2-48vcpu-192gb"
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_picks_largest_available_without_workload")


_REAL_POOLS = (
    _profile("cpu-d3-4vcpu-16gb", 4, "medium", True, 0.11),
    _profile("cpu-e2-48vcpu-192gb", 48, "medium", True, 1.15),
    _profile("cpu-d3-96vcpu-384gb", 96, "high", True, 2.28),
)


def test_small_benchmark_gets_a_small_box():
    # THE core fix: 5 instances have no business on a 96-vCPU box — workers
    # beyond the instance count idle cores at the big box's rate. The high
    # availability tier of the big box doesn't outrank cost (tier is a
    # reliability gate for LOW pools, not a ranking).
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(*_REAL_POOLS))
        got = c3_compute._best_cpu_hardware({}, {"timeout": 30}, 5)
        assert got == "cpu-d3-4vcpu-16gb", got
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_small_benchmark_gets_a_small_box")


def test_large_benchmark_still_gets_the_big_box():
    # 1000 instances x 60s: the 96-vCPU box's wall-clock savings outweigh its
    # rate, so scale-up still happens when the workload earns it.
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(*_REAL_POOLS))
        got = c3_compute._best_cpu_hardware({}, {"timeout": 60}, 1000)
        assert got == "cpu-d3-96vcpu-384gb", got
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_large_benchmark_still_gets_the_big_box")


def test_explicit_bench_workers_routes_to_cheapest_rate():
    # A pinned bench_workers caps every profile's parallelism the same way,
    # so the big boxes lose their only advantage and the cheapest rate wins
    # even on a large benchmark.
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(*_REAL_POOLS))
        got = c3_compute._best_cpu_hardware(
            {}, {"timeout": 60, "bench_workers": 4}, 1000)
        assert got == "cpu-d3-4vcpu-16gb", got
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_explicit_bench_workers_routes_to_cheapest_rate")


def test_low_tier_is_last_resort_even_when_cheapest():
    # A LOW pool may simply never provision — a cheap box that never arrives
    # costs more than any rate. But when low-tier is ALL that's listed, use it.
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(
            _profile("cpu-d3-4vcpu-16gb", 4, "low", True, 0.11),
            _profile("cpu-e2-48vcpu-192gb", 48, "high", True, 1.15),
        ))
        got = c3_compute._best_cpu_hardware({}, {"timeout": 30}, 5)
        assert got == "cpu-e2-48vcpu-192gb", got
        c3_compute._hw_cache = None
        _with_listing(_listing(
            _profile("cpu-d3-4vcpu-16gb", 4, "low", True, 0.11),
        ))
        got = c3_compute._best_cpu_hardware({}, {"timeout": 30}, 5)
        assert got == "cpu-d3-4vcpu-16gb", got
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_low_tier_is_last_resort_even_when_cheapest")


def test_session_blocklist_skips_a_pool_that_misbehaved():
    # Nothing is blocklisted out of the box — the largest box wins. Once a
    # profile has proven undeliverable in THIS session, auto moves off it
    # without the contributor changing any config.
    orig = c3_compute._run_c3
    orig_blocked = set(c3_compute._session_hw_blocklist)
    try:
        c3_compute._session_hw_blocklist.clear()
        _with_listing(_listing(
            _profile("cpu-d3-96vcpu-384gb", 96, "high", True, 2.28),
            _profile("cpu-e2-48vcpu-192gb", 48, "medium", True, 1.15),
        ))
        assert c3_compute._best_cpu_hardware({}) == "cpu-d3-96vcpu-384gb"
        # ...it never gives a queued job a machine, so drop it and re-pick.
        c3_compute._blocklist_profile_for_session("cpu-d3-96vcpu-384gb", "test")
        assert c3_compute._best_cpu_hardware({}) == "cpu-e2-48vcpu-192gb"
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
        c3_compute._session_hw_blocklist.clear()
        c3_compute._session_hw_blocklist.update(orig_blocked)
    print("PASS test_session_blocklist_skips_a_pool_that_misbehaved")


def test_config_and_env_extend_blocklist():
    import os
    orig = c3_compute._run_c3
    try:
        listing = _listing(
            _profile("cpu-e2-48vcpu-192gb", 48, "medium", True, 1.15),
            _profile("cpu-e2-4vcpu-16gb", 4, "medium", True, 0.11),
        )
        _with_listing(listing)
        cfg = {"c3_hardware_blocklist": "cpu-e2-48vcpu-192gb"}
        assert c3_compute._best_cpu_hardware({}, cfg) == "cpu-e2-4vcpu-16gb"
        c3_compute._hw_cache = None
        os.environ["TIG_C3_HW_BLOCKLIST"] = "cpu-e2-48vcpu-192gb"
        try:
            assert c3_compute._best_cpu_hardware({}) == "cpu-e2-4vcpu-16gb"
        finally:
            os.environ.pop("TIG_C3_HW_BLOCKLIST")
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_config_and_env_extend_blocklist")


def test_prefers_higher_availability_tier_over_size():
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(
            _profile("cpu-x9-64vcpu-256gb", 64, "low", True, 2.28),
            _profile("cpu-e2-48vcpu-192gb", 48, "high", True, 1.15),
        ))
        assert c3_compute._best_cpu_hardware({}) == "cpu-e2-48vcpu-192gb"
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_prefers_higher_availability_tier_over_size")


def test_skips_unavailable_and_gpu_profiles():
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(
            _profile("cpu-x9-64vcpu-256gb", 64, "medium", False, 2.28),
            {"hardware_profile": "h100-80gb", "hardware_kind": "gpu",
             "vcpu": None, "availability_tier": "high", "available": True,
             "rate_per_hour_gbp": 2.37},
            _profile("cpu-e2-4vcpu-16gb", 4, "medium", True, 0.11),
        ))
        assert c3_compute._best_cpu_hardware({}) == "cpu-e2-4vcpu-16gb"
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_skips_unavailable_and_gpu_profiles")


def test_ties_break_on_cheaper_rate():
    orig = c3_compute._run_c3
    try:
        _with_listing(_listing(
            _profile("cpu-pricey-4vcpu", 4, "medium", True, 0.22),
            _profile("cpu-cheap-4vcpu", 4, "medium", True, 0.11),
        ))
        assert c3_compute._best_cpu_hardware({}) == "cpu-cheap-4vcpu"
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_ties_break_on_cheaper_rate")


def test_falls_back_to_default_on_query_failure():
    orig = c3_compute._run_c3
    try:
        for bad in (
            None,  # CLI timed out
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
            _listing(),  # empty listing
        ):
            _with_listing(bad)
            assert c3_compute._best_cpu_hardware({}) == c3_compute._DEFAULT_CPU_HARDWARE, bad
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_falls_back_to_default_on_query_failure")


def test_result_is_cached():
    orig = c3_compute._run_c3
    calls = []
    try:
        c3_compute._hw_cache = None

        def counting(*a, **k):
            calls.append(1)
            return _listing(_profile("cpu-e2-48vcpu-192gb", 48, "medium", True, 1.15))

        c3_compute._run_c3 = counting
        assert c3_compute._best_cpu_hardware({}) == "cpu-e2-48vcpu-192gb"
        assert c3_compute._best_cpu_hardware({}) == "cpu-e2-48vcpu-192gb"
        assert len(calls) == 1, calls  # second call served from cache
    finally:
        c3_compute._run_c3 = orig
        c3_compute._hw_cache = None
    print("PASS test_result_is_cached")


def _main():
    test_picks_largest_available_without_workload()
    test_small_benchmark_gets_a_small_box()
    test_large_benchmark_still_gets_the_big_box()
    test_explicit_bench_workers_routes_to_cheapest_rate()
    test_low_tier_is_last_resort_even_when_cheapest()
    test_session_blocklist_skips_a_pool_that_misbehaved()
    test_config_and_env_extend_blocklist()
    test_prefers_higher_availability_tier_over_size()
    test_skips_unavailable_and_gpu_profiles()
    test_ties_break_on_cheaper_rate()
    test_falls_back_to_default_on_query_failure()
    test_result_is_cached()
    print("\nAll C3 hardware-selection tests passed.")


if __name__ == "__main__":
    _main()
