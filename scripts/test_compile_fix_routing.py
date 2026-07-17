"""Self-running tests for compile-fix vs infrastructure-error routing in
_benchmark_with_compile_fix.

The infra markers used to be bare substrings ("500", "401", "timeout"), so a
compile error whose log mentioned a track name like `n_h_edges=50000` was
misrouted to the infrastructure branch and the LLM compile fix silently never
ran (seen live: opus-008's E0425 import errors were never retried). Rustc
output must win the classification.

Run directly: `python scripts/test_compile_fix_routing.py`
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_loop  # noqa: E402


def _route(build_err: str) -> bool:
    """Return True if _benchmark_with_compile_fix attempted an LLM fix."""
    fix_called = []
    real_bench = run_loop.run_benchmark
    real_fix = run_loop._try_compile_fix
    try:
        run_loop.run_benchmark = lambda *a, **k: (None, build_err)
        run_loop._try_compile_fix = (
            lambda *a, **k: (fix_called.append(True) and False, 0, 0))
        bench, err, _changed, _i, _o = run_loop._benchmark_with_compile_fix(
            SimpleNamespace(), "model", "key", {}, "http://srv", None)
        assert bench is None and err == build_err
    finally:
        run_loop.run_benchmark = real_bench
        run_loop._try_compile_fix = real_fix
    return bool(fix_called)


def test_compile_error_with_numeric_noise_gets_fixed():
    # The live failure shape: rustc errors + a track name containing "500".
    err = (
        "[C3] 3/3 shard job(s) failed, 1 distinct error(s):\n"
        "  shard(s) 0,1,2: job job_1784290854753_uqjaki failed\n"
        "  [BENCH] shard window starts: {'n_h_edges=50000': 0}\n"
        "   Compiling tig-challenges v0.1.0 (/app)\n"
        "error[E0425]: cannot find type `Value` in this scope\n"
    )
    assert _route(err), "compile error was misrouted to the infra branch"
    print("PASS compile error with numeric noise reaches the LLM fix")


def test_infra_errors_skip_the_fix():
    assert not _route("[C3] c3 CLI not found. Install from https://…")
    assert not _route("job job_12345 timeout\nno artifacts")
    assert not _route("c3 deploy failed: HTTP 401 unauthorized")
    print("PASS infrastructure errors skip the LLM fix")


def test_bare_numbers_inside_words_are_not_infra():
    # No rustc markers, but "50000"/"14013" must not read as HTTP 500/401.
    err = "job completed but benchmark.json was not found\nn_h_edges=50000 seed=14013"
    assert _route(err), "a number containing 500/401 misread as an HTTP code"
    print("PASS embedded numbers are not HTTP status codes")


if __name__ == "__main__":
    test_compile_error_with_numeric_noise_gets_fixed()
    test_infra_errors_skip_the_fix()
    test_bare_numbers_inside_words_are_not_infra()
    print("\nAll compile-fix routing tests passed.")
