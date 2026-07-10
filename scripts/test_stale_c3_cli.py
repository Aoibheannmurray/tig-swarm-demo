"""Tests for stale-c3-CLI detection (c3_compute._stale_cli_error).

An outdated c3 binary rejects the project's .c3 fields with Go-struct YAML
unmarshal errors ("field hardware not found in type data.C3Config"). That must
be translated into an actionable environment-error message whose prefix
matches run_loop's infra_markers, so the loop doesn't burn LLM calls "fixing"
Rust that isn't broken.

Self-running: `python scripts/test_stale_c3_cli.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from c3_compute import _stale_cli_error  # noqa: E402

# Verbatim shape from a real occurrence (macOS, stale CLI).
STALE_OUTPUT = """\
Error: read .c3 config: parse /private/var/folders/xx/T/tig-c3-tig-abc/shard-0/.c3: invalid YAML: yaml: unmarshal errors:
  line 3: field hardware not found in type data.C3Config
  line 9: field requires_accelerator not found in type data.DockerConfig"""


def test_detects_stale_cli_and_is_actionable():
    msg = _stale_cli_error(STALE_OUTPUT)
    assert msg is not None
    assert msg.startswith("c3 CLI is out of date"), msg
    assert "install.sh" in msg and "which -a c3" in msg, msg
    # Carries the CLI's own words so nothing is hidden.
    assert "data.C3Config" in msg, msg
    print("PASS test_detects_stale_cli_and_is_actionable")


def test_matches_run_loop_infra_markers():
    """The message prefix must be in run_loop's infra_markers, or the retry
    loop still treats it as a code failure. Read the list from the source so
    the two can't drift apart silently."""
    src = (Path(__file__).resolve().parent / "run_loop.py").read_text()
    assert '"c3 CLI is out of date"' in src, (
        "run_loop.infra_markers lost the stale-CLI marker"
    )
    print("PASS test_matches_run_loop_infra_markers")


def test_real_build_errors_pass_through():
    for output in (
        "error[E0425]: cannot find value `foo` in this scope",
        "c3 deploy failed (1):\nnetwork unreachable",
        "",
    ):
        assert _stale_cli_error(output) is None, output
    print("PASS test_real_build_errors_pass_through")


def test_self_heal_runs_installer_exactly_once():
    """_update_c3_cli_once must run the installer at most once per process and
    share the outcome (success AND failure) with every later caller — parallel
    shard threads must not race N installer runs."""
    import c3_compute as cc

    calls = []
    orig = cc._run_c3_installer
    try:
        # Success is cached.
        cc._c3_update_result = None
        cc._run_c3_installer = lambda: calls.append(1) or True
        assert cc._update_c3_cli_once() is True
        assert cc._update_c3_cli_once() is True
        assert len(calls) == 1, f"installer ran {len(calls)} times"

        # Failure is cached too — no retry storm on a broken installer.
        calls.clear()
        cc._c3_update_result = None
        cc._run_c3_installer = lambda: calls.append(1) or False
        assert cc._update_c3_cli_once() is False
        assert cc._update_c3_cli_once() is False
        assert len(calls) == 1, f"installer ran {len(calls)} times"
    finally:
        cc._run_c3_installer = orig
        cc._c3_update_result = None
    print("PASS test_self_heal_runs_installer_exactly_once")


def test_shard_retry_guard_prevents_loops():
    """After one update-and-retry, a still-stale CLI must fall through to the
    actionable error, not recurse — verified via the _cli_updated parameter.
    The guard lives on _run_one_c3_shard_inner: _run_one_c3_shard is the
    job_slots semaphore wrapper, and the self-heal retry must re-enter _inner
    directly so the already-held slot isn't re-acquired."""
    import inspect
    import c3_compute as cc
    sig = inspect.signature(cc._run_one_c3_shard_inner)
    assert "_cli_updated" in sig.parameters, sig
    assert sig.parameters["_cli_updated"].default is False
    print("PASS test_shard_retry_guard_prevents_loops")


if __name__ == "__main__":
    test_detects_stale_cli_and_is_actionable()
    test_matches_run_loop_infra_markers()
    test_real_build_errors_pass_through()
    test_self_heal_runs_installer_exactly_once()
    test_shard_retry_guard_prevents_loops()
    print("ALL PASS")
