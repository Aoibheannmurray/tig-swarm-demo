"""Process-group-aware subprocess helpers (teardown that kills grandchildren).

The fleet and the agentic backends spawn children (run_loop.py, the claude/
codex CLIs) that themselves spawn docker/cargo/nvcc grandchildren. A plain
`proc.terminate()` (or subprocess.run's timeout kill) signals only the direct
child, orphaning the grandchildren mid-build.

On POSIX we put each child in its own session (process group) via
`start_new_session=True` and signal the whole group: SIGTERM first, then
SIGKILL after a grace period. On Windows (os.name == "nt") there is no
killpg — we keep the previous terminate()/kill() behavior on the direct
child.

Stdlib only; flat module imported by bare name like the rest of scripts/.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

_IS_WINDOWS = os.name == "nt"

# Grace period between SIGTERM and SIGKILL when tearing a tree down.
DEFAULT_TERM_GRACE_S = 10.0


def group_kwargs() -> dict:
    """Extra Popen kwargs that give the child its own process group (POSIX).

    Pass as `**group_kwargs()` at every spawn site whose teardown goes
    through `term_tree`/`kill_tree`. Empty on Windows (current behavior
    kept there)."""
    if _IS_WINDOWS:
        return {}
    return {"start_new_session": True}


def _signal_tree(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group; fall back to the direct child.

    The fallback covers a child that was spawned without `group_kwargs()`
    (or whose group is already gone) — behavior then matches the old
    single-process terminate/kill."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def term_tree(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process tree (POSIX); `terminate()` on Windows."""
    if _IS_WINDOWS:
        proc.terminate()
        return
    _signal_tree(proc, signal.SIGTERM)


def kill_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process tree (POSIX); `kill()` on Windows."""
    if _IS_WINDOWS:
        proc.kill()
        return
    _signal_tree(proc, signal.SIGKILL)


def terminate_tree(
    proc: subprocess.Popen, *, grace_s: float = DEFAULT_TERM_GRACE_S,
) -> None:
    """Graceful tree teardown: SIGTERM, wait up to `grace_s`, then SIGKILL.

    Returns once the direct child has been reaped (grandchildren, being in
    the same killed group, die with it on POSIX)."""
    term_tree(proc)
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    kill_tree(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_tree(
    cmd: list[str],
    *,
    input_text: str | None = None,
    cwd=None,
    env: dict | None = None,
    timeout_s: float | None = None,
    term_grace_s: float = DEFAULT_TERM_GRACE_S,
) -> tuple[str, str, int | None, bool]:
    """subprocess.run-alike whose timeout kills the whole process tree.

    `subprocess.run(timeout=...)` can't cleanly killpg after TimeoutExpired
    (it has already kill()ed the direct child and is blocked reaping it while
    grandchildren keep the pipes open), so this uses Popen + communicate and
    tears the group down itself on expiry.

    Captures text output (UTF-8, errors replaced). Returns
    `(stdout, stderr, returncode, timed_out)`; on timeout `returncode` is
    whatever the reaped child reported (negative signal number)."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        **group_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout_s)
        return stdout or "", stderr or "", proc.returncode, False
    except subprocess.TimeoutExpired:
        terminate_tree(proc, grace_s=term_grace_s)
        # The tree is dead (or Windows child kill()ed); drain what the child
        # managed to write before the deadline. Bounded wait in case an
        # escaped process still holds the pipe.
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            stdout, stderr = "", ""
        return stdout or "", stderr or "", proc.returncode, True


def _selftest() -> int:
    """Spawn a child that spawns a sleeping grandchild; verify the grandchild
    dies when the tree is torn down. Run: python3 scripts/proc_utils.py"""
    import sys

    child_src = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print(g.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src],
        stdout=subprocess.PIPE, text=True, **group_kwargs(),
    )
    assert proc.stdout is not None
    grandchild_pid = int(proc.stdout.readline())
    print(f"child={proc.pid} grandchild={grandchild_pid}")
    terminate_tree(proc, grace_s=5)
    time.sleep(0.5)
    try:
        os.kill(grandchild_pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    print(f"grandchild alive after teardown: {alive}")
    return 1 if alive else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
