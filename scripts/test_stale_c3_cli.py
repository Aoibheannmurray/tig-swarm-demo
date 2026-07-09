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


if __name__ == "__main__":
    test_detects_stale_cli_and_is_actionable()
    test_matches_run_loop_infra_markers()
    test_real_build_errors_pass_through()
    print("ALL PASS")
