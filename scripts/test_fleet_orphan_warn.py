"""Tests for the fleet-startup orphaned-C3-job warning
(run_fleet._warn_orphaned_c3_jobs).

Leftover `tig-*` jobs from a previous fleet keep holding chips against the
plan's concurrency cap, so a fresh fleet can't claim its full slot budget.
The launcher WARNS about them (never cancels — the user owns that call) and
stays silent when there's nothing to report or when C3 can't be reached.

Self-running: `python scripts/test_fleet_orphan_warn.py`.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_fleet as rf  # noqa: E402


class _Proc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _run(squeue_json, *, have_cli=True, raises=None):
    """Invoke the warner with a fake `c3 squeue --json` and capture log lines."""
    logs: list[str] = []
    saved = (rf.shutil.which, rf.subprocess.run)
    rf.shutil.which = lambda _c: ("c3" if have_cli else None)

    def fake_run(cmd, **kw):
        if raises is not None:
            raise raises
        return _Proc(squeue_json)

    rf.subprocess.run = fake_run
    try:
        rf._warn_orphaned_c3_jobs("c3_key_x", log=logs.append)
    finally:
        rf.shutil.which, rf.subprocess.run = saved
    return "\n".join(logs)


def test_warns_only_about_active_tig_jobs():
    payload = """
    {"jobs": [
      {"id": "job_a", "name": "tig-knapsack-aaa", "status": "RUNNING"},
      {"id": "job_b", "name": "tig-knapsack-bbb", "status": "PENDING"},
      {"id": "job_c", "name": "tig-knapsack-ccc", "status": "COMPLETED"},
      {"id": "job_d", "name": "someone-elses-job", "status": "RUNNING"}
    ]}"""
    out = _run(payload)
    assert "2 C3 job(s) from a previous run" in out, out   # a + b only
    assert "job_a" in out and "job_b" in out, out
    assert "job_c" not in out, out                          # terminal — skipped
    assert "job_d" not in out, out                          # not ours — skipped
    assert "c3 cancel job_a job_b" in out, out              # exact reclaim line
    print("PASS test_warns_only_about_active_tig_jobs")


def test_silent_when_nothing_stale():
    out = _run('{"jobs": [{"id": "j", "name": "tig-x-1", "status": "COMPLETED"}]}')
    assert out == "", repr(out)
    print("PASS test_silent_when_nothing_stale")


def test_silent_without_cli_or_on_error():
    assert _run("", have_cli=False) == ""
    assert _run("", raises=OSError("boom")) == ""
    assert _run("", raises=subprocess.TimeoutExpired("c3", 30)) == ""
    assert _run("not json at all") == ""
    assert _run("") == ""                                   # empty stdout
    print("PASS test_silent_without_cli_or_on_error")


if __name__ == "__main__":
    test_warns_only_about_active_tig_jobs()
    test_silent_when_nothing_stale()
    test_silent_without_cli_or_on_error()
    print("ALL PASS")
