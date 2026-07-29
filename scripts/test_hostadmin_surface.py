#!/usr/bin/env python3
"""Self-running tests for the hostadmin public surface.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_hostadmin_surface.py

`setup.py` used to be both the CLI and a back-compat import surface, via a
types.ModuleType subclass whose __getattr__ searched every hostadmin submodule
and whose __setattr__ forwarded writes into them. Embedders now import
`hostadmin` directly, which re-exports explicitly.

Covers:
  - every name in __all__ actually resolves
  - the challenge registries stay LAZY: importing hostadmin must not pull in
    server/challenges.py, because `setup.py sync` runs inside worktrees and in
    trimmed clones that have no server/ directory
  - a trimmed clone (hostadmin/ + setup.py, no server/) can still import and
    run sync, and a command that genuinely needs challenge metadata fails with
    a clean message rather than an ImportError
  - setup.py carries no module-class magic, and still dispatches its commands
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hostadmin

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def test_reexports_resolve() -> None:
    print("re-exports")
    missing = [n for n in hostadmin.__all__ if not hasattr(hostadmin, n)]
    check(not missing, f"every __all__ name resolves (missing: {missing})")
    check(len(hostadmin.__all__) == len(set(hostadmin.__all__)),
          "no duplicates in __all__")
    # A few spot checks across submodules, so a re-export that silently stops
    # being exported is caught by name rather than only in aggregate.
    for name in ("create_swarm", "read_swarm_admin", "run_tacit",
                 "build_join_link", "post_json", "RailwayError"):
        check(hasattr(hostadmin, name), f"{name} is exported")


def test_challenges_stay_lazy() -> None:
    """Importing hostadmin must not load server/challenges.py. `setup.py sync`
    runs inside worktrees, and trimmed clones have no server/."""
    print("lazy challenge registries")

    probe = (
        "import sys; sys.path.insert(0, %r);"
        "import hostadmin;"
        "from hostadmin import challenges_bridge as cb;"
        "print('LOADED' if cb._challenge_registry_cache is not None else 'LAZY');"
        "hostadmin.CHALLENGES;"
        "print('LOADED' if cb._challenge_registry_cache is not None else 'LAZY')"
    ) % str(ROOT)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=ROOT).stdout.split()
    check(out[:1] == ["LAZY"], "import hostadmin does not load the registry")
    check(out[1:2] == ["LOADED"], "touching CHALLENGES loads it")


def test_trimmed_clone() -> None:
    """hostadmin/ + setup.py with no server/ — the shape a trimmed clone and a
    swarm worktree can present."""
    print("trimmed clone (no server/)")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        shutil.copy(ROOT / "setup.py", tmp / "setup.py")
        shutil.copytree(ROOT / "hostadmin", tmp / "hostadmin",
                        ignore=shutil.ignore_patterns("__pycache__"))
        check(not (tmp / "server").exists(), "fixture really has no server/")

        r = subprocess.run([sys.executable, "-c", "import hostadmin"],
                           capture_output=True, text=True, cwd=tmp)
        check(r.returncode == 0, f"import hostadmin succeeds ({r.stderr.strip()[:80]})")

        # sync should reach its own error, not blow up importing.
        r = subprocess.run([sys.executable, "setup.py", "sync"],
                           capture_output=True, text=True, cwd=tmp)
        combined = r.stdout + r.stderr
        check("Traceback" not in combined,
              "setup.py sync does not traceback without server/")
        check("server_url" in combined,
              "setup.py sync reaches its own missing-config message")

        # ...and a command that needs challenge metadata says so clearly.
        r = subprocess.run(
            [sys.executable, "-c",
             "import hostadmin\ntry:\n hostadmin.CHALLENGES\nexcept RuntimeError as e:\n print('CLEAN', e)"],
            capture_output=True, text=True, cwd=tmp)
        check("CLEAN" in r.stdout and "server/challenges.py" in r.stdout,
              "CHALLENGES raises a clean RuntimeError naming the missing file")


def test_setup_is_only_a_cli() -> None:
    print("setup.py is a CLI, not an import surface")

    # Code only: the comments deliberately describe the machinery that was
    # removed, and that history is worth keeping. Matching it would fail on
    # the explanation rather than on a regression.
    src = "\n".join(
        line for line in (ROOT / "setup.py").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for banned, why in (
        ("types.ModuleType", "module-class magic"),
        ("sys.modules[__name__].__class__", "module-class reassignment"),
        ("_COMPAT_MODULES", "the compat delegation tuple"),
        ("def __setattr__", "setattr forwarding"),
    ):
        check(banned not in src, f"no {why} in setup.py")

    r = subprocess.run([sys.executable, "setup.py", "--help"],
                       capture_output=True, text=True, cwd=ROOT)
    check(r.returncode == 0, "setup.py --help exits 0")
    for cmd in ("create", "switch", "sync", "tacit", "invite", "revoke", "list"):
        check(cmd in r.stdout, f"setup.py still dispatches {cmd}")


def main() -> int:
    test_reexports_resolve()
    test_challenges_stay_lazy()
    test_trimmed_clone()
    test_setup_is_only_a_cli()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all hostadmin surface checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
