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


def test_c3_infra_marker_skips_the_fix():
    """The live misroute: a pool killed the job before benchmark.py wrote a
    byte, the profile was correctly blocklisted — and the LLM was then asked
    to 'fix' an algorithm that was never the problem. c3_compute now stamps
    C3_INFRA_MARKER on every error it KNOWS is pool trouble."""
    import c3_compute

    # The two modules route on the same literal — pin them together.
    src = open(run_loop.__file__, encoding="utf-8").read()
    assert f'"{c3_compute.C3_INFRA_MARKER}"' in src, (
        "run_loop's infra_markers no longer contain c3_compute.C3_INFRA_MARKER")

    # Job died before the benchmark ran (empty bench stderr), single job…
    assert not _route(
        "job job_1784290854753_abc failed — [C3 infra] died before the "
        "benchmark produced any output (a C3/pool problem, not the "
        "algorithm)\nsome runner log noise")
    # …and the same head line after run_benchmark_c3's shard grouping.
    assert not _route(
        "[C3] 1/1 shard job(s) failed:\n"
        "  shard(s) 1: job job_1 failed — [C3 infra] died before the "
        "benchmark produced any output (a C3/pool problem, not the "
        "algorithm)\n  runner log noise")
    # Queue-stall and internal-error shapes carry the same marker.
    assert not _route(
        "[C3 infra] job job_2 never left the C3 queue on cpu-d3-4vcpu-16gb — "
        "the pool accepted the job but never gave it a machine.")
    assert not _route(
        "[C3 infra] job job_3 died with a C3 internal error on "
        "cpu-d3-4vcpu-16gb (support code C3-JOB-531022R88XJW, for C3 "
        "support) while C3 still reported it running.")
    # But a job that failed WITH cargo output is still a code problem.
    assert _route(
        "job job_4 failed\n   Compiling tig-challenges v0.1.0 (/app)\n"
        "error[E0425]: cannot find type `Value` in this scope")
    print("PASS C3 infra-marked errors skip the LLM fix")


def test_bare_numbers_inside_words_are_not_infra():
    # No rustc markers, but "50000"/"14013" must not read as HTTP 500/401.
    err = "job completed but benchmark.json was not found\nn_h_edges=50000 seed=14013"
    assert _route(err), "a number containing 500/401 misread as an HTTP code"
    print("PASS embedded numbers are not HTTP status codes")


def test_compile_fix_reinserts_imports_the_model_drops():
    """The fix loop used to make things WORSE.

    Every other codegen path runs ensure_common_imports; the compile-fix path
    did not. So a model asked to repair a borrow error returned a whole file
    without `use serde_json::{Map, Value}` and the next build failed with
    E0412 on the hyperparameters signature — a brand new error, inside the
    code we were repairing. Both retries then chased the damage instead of the
    original fault. Observed on knapsack: E0382 -> E0412 -> E0412 -> freeze.

    Asserted at the source, because the repair has to happen between
    parse_response and write — a fix applied anywhere later would already have
    been validated and written."""
    import inspect
    import run_loop
    src = inspect.getsource(run_loop._try_compile_fix)
    assert "ensure_common_imports" in src, (
        "the compile-fix path must re-insert dropped imports")
    # Before validation: an import-less file can otherwise fail validation and
    # abort the retry for a reason the model never had a chance to fix.
    assert src.index("ensure_common_imports") < src.index("_validate_entry"), (
        "imports must be repaired before the fix is validated")
    assert src.index("ensure_common_imports") < src.index("files.write"), (
        "imports must be repaired before the fix is written")
    print("PASS test_compile_fix_reinserts_imports_the_model_drops")


def test_the_exact_failure_from_the_log_is_repaired():
    from challenge_files import ensure_common_imports, ensure_challenge_import
    dropped = (
        "pub fn solve_challenge(\n"
        "    challenge: &Challenge,\n"
        "    _hyperparameters: &Option<Map<String, Value>>,\n"
        ") -> anyhow::Result<()> { Ok(()) }\n"
    )
    fixed = ensure_common_imports(ensure_challenge_import(dropped, "knapsack"))
    assert "serde_json::Map" in fixed and "serde_json::Value" in fixed, fixed
    assert "tig_challenges::knapsack" in fixed, fixed
    print("PASS test_the_exact_failure_from_the_log_is_repaired")


if __name__ == "__main__":
    test_compile_error_with_numeric_noise_gets_fixed()
    test_infra_errors_skip_the_fix()
    test_c3_infra_marker_skips_the_fix()
    test_bare_numbers_inside_words_are_not_infra()
    test_compile_fix_reinserts_imports_the_model_drops()
    test_the_exact_failure_from_the_log_is_repaired()
    print("\nAll compile-fix routing tests passed.")
