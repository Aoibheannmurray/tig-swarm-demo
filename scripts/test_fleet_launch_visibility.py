"""Tests for making a stalled fleet launch diagnosable.

Contributors reported the launch stopping dead at "[fleet] preparing to launch
— resolving keys and swarm state…" with nothing after it. That one line
covered two operations that can block indefinitely — an interactive key prompt
and `setup.py sync` — and everything either of them printed went to stdout,
which the web companion doesn't show and a redirected launch buffers. So the
last thing anyone saw named neither the step nor a way out.

Self-running: `python scripts/test_fleet_launch_visibility.py`.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_fleet  # noqa: E402
import secrets_local  # noqa: E402

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


class _FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def _isolated_root():
    """Point run_fleet at a temp ROOT — _ensure_root_swarm_cache DELETES a
    cache whose server_url doesn't match, and must never touch the real one."""
    return Path(tempfile.mkdtemp())


def test_sync_progress_reaches_the_ui():
    lines: list[str] = []
    tmp = _isolated_root()
    orig_root, orig_run = run_fleet.ROOT, run_fleet.subprocess.run
    try:
        run_fleet.ROOT = tmp

        def fake_run(*a, **k):
            # Sync "succeeds" and writes the cache the caller checks for.
            (tmp / ".swarm-cache.json").write_text("{}")
            return _FakeProc(0)

        run_fleet.subprocess.run = fake_run
        run_fleet._ensure_root_swarm_cache("https://swarm.example", log=lines.append)
    finally:
        run_fleet.ROOT, run_fleet.subprocess.run = orig_root, orig_run
    check(any("syncing swarm state" in ln for ln in lines),
          "the sync step announces itself through the fleet log")
    check(any("https://swarm.example" in ln for ln in lines),
          "and names the server it is waiting on")


def test_sync_cannot_hang_forever():
    """A wedged server used to block the launch indefinitely with its output
    captured — no timeout, nothing on screen."""
    tmp = _isolated_root()
    orig_root, orig_run = run_fleet.ROOT, run_fleet.subprocess.run
    seen_timeout = []
    try:
        run_fleet.ROOT = tmp

        def fake_run(*a, **k):
            seen_timeout.append(k.get("timeout"))
            raise subprocess.TimeoutExpired(cmd="setup.py sync", timeout=k.get("timeout"))

        run_fleet.subprocess.run = fake_run
        try:
            run_fleet._ensure_root_swarm_cache("https://swarm.example", log=lambda m: None)
            msg = ""
        except SystemExit as exc:
            msg = str(exc)
    finally:
        run_fleet.ROOT, run_fleet.subprocess.run = orig_root, orig_run
    check(seen_timeout and seen_timeout[0] == run_fleet._SYNC_TIMEOUT_SECS,
          "the sync subprocess is given a hard timeout")
    check("https://swarm.example" in msg, "the timeout message names the server")
    check("fleet.config.json" in msg and "python setup.py sync" in msg,
          "and gives the user two things to try themselves")


def test_missing_key_is_announced_before_it_blocks():
    """The blocking input() lives in secrets_local and prints to stdout only.
    Whatever happens next, the fleet log must first say a key is missing."""
    orig_resolve, orig_prompt = secrets_local.resolve, secrets_local.prompt_and_store
    orig_isatty = sys.stdin.isatty
    agent = {"name": "sunny-otter", "provider": "anthropic"}
    try:
        secrets_local.resolve = lambda *a, **k: ""
        secrets_local.prompt_and_store = lambda *a, **k: None

        # Interactive: it really is about to wait on a human, so say so.
        lines: list[str] = []
        sys.stdin.isatty = lambda: True
        try:
            run_fleet._resolve_api_key(agent, log=lines.append)
        except SystemExit:
            pass
        blob = "\n".join(lines)
        check("ANTHROPIC_API_KEY" in blob and "sunny-otter" in blob,
              "names the missing variable and the agent it belongs to")
        check("waiting for you to paste" in blob,
              "warns that the launch is about to block on input")

        # Non-interactive (web companion, nohup): no prompt can happen, so
        # promising one would be a lie — the hard error follows instead.
        lines.clear()
        sys.stdin.isatty = lambda: False
        try:
            run_fleet._resolve_api_key(agent, log=lines.append)
        except SystemExit:
            pass
        blob = "\n".join(lines)
        check("ANTHROPIC_API_KEY" in blob, "still reports the missing variable")
        check("waiting for you to paste" not in blob,
              "but promises no prompt when none is possible")
    finally:
        secrets_local.resolve, secrets_local.prompt_and_store = orig_resolve, orig_prompt
        sys.stdin.isatty = orig_isatty


def test_no_key_error_is_actionable():
    orig_resolve, orig_prompt = secrets_local.resolve, secrets_local.prompt_and_store
    try:
        secrets_local.resolve = lambda *a, **k: ""
        secrets_local.prompt_and_store = lambda *a, **k: None
        try:
            run_fleet._resolve_api_key({"name": "a", "provider": "anthropic"},
                                       log=lambda m: None)
            msg = ""
        except SystemExit as exc:
            msg = str(exc)
    finally:
        secrets_local.resolve, secrets_local.prompt_and_store = orig_resolve, orig_prompt
    check("export ANTHROPIC_API_KEY" in msg, "the exit names the export to run")
    check("run.py --ui" in msg, "and the UI route for people not in a terminal")


def test_fleet_log_flushes():
    """Piped/redirected launches block-buffer stdout; without an explicit
    flush the whole bootstrap is invisible until the buffer fills."""
    import inspect
    src = inspect.getsource(run_fleet.cmd_run)
    check("flush=True" in src, "fleet log lines are flushed as they are written")


if __name__ == "__main__":
    print("sync visibility")
    test_sync_progress_reaches_the_ui()
    test_sync_cannot_hang_forever()
    print("key resolution")
    test_missing_key_is_announced_before_it_blocks()
    test_no_key_error_is_actionable()
    print("output plumbing")
    test_fleet_log_flushes()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("all fleet launch-visibility checks passed")
