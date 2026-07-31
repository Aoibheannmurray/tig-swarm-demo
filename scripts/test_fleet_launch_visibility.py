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


def test_git_calls_cannot_hang_the_launch():
    """A git that blocks — a stale index.lock, a credential prompt — used to
    stop the launch forever with its output captured and nothing on screen."""
    import subprocess as _sp
    orig_run = run_fleet.subprocess.run
    seen = {}

    def fake_run(argv, *a, **k):
        seen["timeout"] = k.get("timeout")
        seen["env"] = k.get("env") or {}
        raise _sp.TimeoutExpired(cmd=argv, timeout=k.get("timeout"))

    try:
        run_fleet.subprocess.run = fake_run
        try:
            run_fleet._git(["worktree", "add", "x"])
            msg = ""
        except RuntimeError as exc:
            msg = str(exc)
    finally:
        run_fleet.subprocess.run = orig_run
    check(seen.get("timeout") == run_fleet._GIT_TIMEOUT_SECS,
          "git calls in the launch path are given a hard timeout")
    check("index.lock" in msg and "credential" in msg,
          "the timeout names the two usual causes")
    check("worktree add" in msg, "and which git command was stuck")
    # A prompt nobody can answer is the failure mode under the web companion,
    # where there is no terminal at all.
    check(seen.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0",
          "git is told never to prompt for credentials")


def test_worktree_creation_announces_itself():
    """The slowest step of a first launch. Silence here is indistinguishable
    from a hang unless it says what it is doing."""
    lines: list[str] = []
    tmp = _isolated_root()
    orig_root, orig_git, orig_exists, orig_refresh = (
        run_fleet.ROOT, run_fleet._git, run_fleet._existing_worktree_paths,
        run_fleet._refresh_worktree,
    )
    try:
        run_fleet.ROOT = tmp
        run_fleet.WORKTREES_DIR = tmp / "worktrees"
        run_fleet._existing_worktree_paths = lambda: set()
        run_fleet._refresh_worktree = lambda *a, **k: None
        run_fleet._git = lambda args, **k: (
            (tmp / "worktrees" / "vader").mkdir(parents=True, exist_ok=True)
            if args[:2] == ["worktree", "add"] else "") or ""
        run_fleet._ensure_worktree("vader", log=lines.append)
    finally:
        (run_fleet.ROOT, run_fleet._git, run_fleet._existing_worktree_paths,
         run_fleet._refresh_worktree) = (
            orig_root, orig_git, orig_exists, orig_refresh)
        run_fleet.WORKTREES_DIR = orig_root / "worktrees"
    blob = "\n".join(lines)
    check("creating worktree" in blob, "worktree creation is announced")
    check("vader" in blob, "and says which agent it is for")


def test_compile_warning_sits_where_the_wait_is():
    """The 'first run compiles' warning belonged on the spawn line, not on
    'preparing' — compiling happens in the agent process, after the launcher
    is done, so anyone stuck in worktree prep was told to wait for something
    that hadn't started."""
    import inspect
    src = inspect.getsource(run_fleet.cmd_run)
    prep = src[src.index("preparing {name}"):src.index("preparing {name}") + 300]
    check("compiles" not in prep, "the preparing line no longer promises a compile")
    spawn = src[src.index("spawned {name}"):src.index("spawned {name}") + 300]
    check("compiles" in spawn, "the spawn line does, where the wait actually is")


def test_agent_names_git_rejects_still_launch():
    """Reported: an agent called "Darth Vader" stopped the launch dead.

    Git refnames cannot contain spaces, so `git worktree add -b "fleet/Darth
    Vader"` fails outright. Names are the contributor's to choose, so the
    branch and directory get a slug instead of the name getting rejected."""
    cases = {
        "Darth Vader": "Darth-Vader",
        "opus-007": "opus-007",          # already safe — unchanged
        "  spaced  name  ": "spaced-name",
        "a/b:c?d*e": "a-b-c-d-e",
        "...": "agent",                   # refs can't be a bare dot sequence
        "my.lock": "my",                  # nor end in .lock
        "": "agent",
    }
    for name, want in cases.items():
        got = run_fleet.slug_for_git(name)
        check(got == want, f"slug({name!r}) == {want!r} (got {got!r})")

    # The real contract: git itself must accept every slug as a branch name.
    for name in list(cases) + ["fleet@{1}", "~^:?*[]", "trailing-dot."]:
        ref = f"fleet/{run_fleet.slug_for_git(name)}"
        ok = subprocess.run(["git", "check-ref-format", "--branch", ref],
                            capture_output=True).returncode == 0
        check(ok, f"git accepts the branch name derived from {name!r}")


def test_slug_is_used_everywhere_a_name_becomes_a_path():
    """A slug used for the branch but not the worktree dir (or vice versa)
    would create the worktree and then fail to find it."""
    import inspect
    src = inspect.getsource(run_fleet)
    # No raw `name` left in a path join or a ref string.
    check("WORKTREES_DIR / name" not in src.replace("`str(WORKTREES_DIR / name)`", ""),
          "no worktree path is built from the raw agent name")
    check('f"fleet/{name}"' not in src,
          "no branch name is built from the raw agent name")


def test_names_that_collide_once_slugged_are_rejected():
    """Slugging maps many names onto one path, so the duplicate-name check is
    no longer enough: two agents that differ only by a space would quietly
    share a worktree, an agent.config.json and a branch."""
    import inspect
    src = inspect.getsource(run_fleet._load_fleet)
    check("slug_for_git(name)" in src and "collide on disk" in src,
          "_load_fleet rejects names that collide after slugging")

    # The guard has to fire on names that are individually legal and distinct.
    collide = run_fleet.slug_for_git("Darth Vader") == run_fleet.slug_for_git("Darth-Vader")
    check(collide, "the collision this guards against is real")
    check(run_fleet.slug_for_git("opus 1") == run_fleet.slug_for_git("opus/1"),
          "punctuation collides the same way")

    # ...and must NOT fire on names that merely slug to themselves.
    distinct = {run_fleet.slug_for_git(n) for n in ("opus-1", "opus-2", "fable")}
    check(len(distinct) == 3, "ordinary distinct names do not collide")


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
    print("worktree preparation")
    test_git_calls_cannot_hang_the_launch()
    test_worktree_creation_announces_itself()
    test_compile_warning_sits_where_the_wait_is()
    test_agent_names_git_rejects_still_launch()
    test_slug_is_used_everywhere_a_name_becomes_a_path()
    test_names_that_collide_once_slugged_are_rejected()
    print("output plumbing")
    test_fleet_log_flushes()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("all fleet launch-visibility checks passed")
