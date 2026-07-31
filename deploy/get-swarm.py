#!/usr/bin/env python3
"""One-command, no-clone contributor bootstrap.

Run straight from the web -- no manual git clone, no editing files:

  macOS / Linux:
    curl -fsSL https://raw.githubusercontent.com/tig-foundation/prometheus-swarm/main/deploy/get-swarm.py \
      | python3 - join "<your-join-link>" --ui

  Windows (PowerShell or cmd; try `py` if `python` isn't recognized):
    curl.exe -fsSL <same-url> | python - join "<your-join-link>" --ui

It maintains a managed checkout of the repo under a per-user data dir and
delegates to that checkout's run.py:

  join "<link>"          -> run.py --join "<link>"  (save credentials, launch)
  join "<link>" --ui     -> ...then open the local setup app in your browser,
                            where you pick models, paste API keys, and launch
  anything else          -> passed through to run.py unchanged

Flags handled here (stripped before delegating):
  --branch <ref>   clone/track a specific branch (env TIG_SWARM_BRANCH works
                   too; default: the repo's default branch)

Re-running updates the checkout. Stdlib only (it runs before anything is
installed) and ASCII only (Windows PowerShell re-encodes piped text).

(The repo can't ship as a pip/uvx package: its load-bearing root setup.py is
a host CLI, not setuptools, so standard packaging can't build it. This
bootstrap is the packaging-free equivalent.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = os.environ.get(
    "TIG_SWARM_REPO", "https://github.com/tig-foundation/prometheus-swarm.git"
)


def _data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "tig-swarm"


def _require_git() -> None:
    if shutil.which("git"):
        return
    if sys.platform == "darwin":
        hint = "  macOS: run `xcode-select --install` (or just run `git` once\n" \
               "  and accept the popup), then re-run this command."
    elif os.name == "nt":
        hint = "  Windows: install from https://git-scm.com/download/win\n" \
               "  then re-open your terminal and re-run this command."
    else:
        hint = "  Linux: `sudo apt install git` (or your distro's equivalent),\n" \
               "  then re-run this command."
    sys.exit("git is required (the swarm runs each agent in a git worktree).\n" + hint)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def ensure_checkout(branch: str = "") -> Path:
    """Clone the repo (or update the managed checkout) and return its path.
    `branch` pins a specific branch; best-effort on updates so an offline
    contributor can still relaunch."""
    _require_git()
    repo = _data_dir() / "repo"
    if (repo / "run.py").exists():
        if branch:
            _git(repo, "fetch", "origin", branch)
            _git(repo, "checkout", branch)
        _git(repo, "pull", "--ff-only")
        return repo
    repo.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [REPO_URL, str(repo)]
    print("Fetching the swarm code into %s ..." % repo)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(
            "Could not fetch the swarm code:\n"
            "  %s\n\n"
            "Check your network, or clone manually:\n"
            "    git clone %s && cd prometheus-swarm && python3 run.py --ui"
            % (result.stderr.strip(), REPO_URL)
        )
    return repo


def main(argv: list[str]) -> int:
    args = list(argv)

    # --branch <ref> is a bootstrap concern; strip it before delegating.
    branch = os.environ.get("TIG_SWARM_BRANCH", "")
    if "--branch" in args:
        i = args.index("--branch")
        if i + 1 >= len(args):
            print("--branch needs a value", file=sys.stderr)
            return 2
        branch = args[i + 1]
        del args[i:i + 2]

    if not args:
        print('Usage: ... | python3 - join "<join-link>" --ui', file=sys.stderr)
        print("       (or any run.py args, e.g. --ui alone)", file=sys.stderr)
        return 2

    checkout = ensure_checkout(branch)
    if args[0] == "join":
        args = ["--join", *args[1:]]  # map the friendly verb to run.py's flag
    return subprocess.call([sys.executable, "run.py", *args], cwd=str(checkout))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
