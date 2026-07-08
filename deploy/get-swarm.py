#!/usr/bin/env python3
"""One-command, no-clone contributor bootstrap (onboarding P4).

Run straight from the web — no `git clone`, no editing files:

    curl -fsSL https://raw.githubusercontent.com/Aoibheannmurray/tig-swarm-demo/main/deploy/get-swarm.py \
      | python3 - join "<your-join-link>"

It maintains a managed checkout of the repo under a per-user data dir and
delegates to that checkout's `run.py`. `join <link>` maps to `run.py --join`;
any other args (e.g. `--ui`) pass straight through. Re-running updates the
checkout. Stdlib only, since it runs before anything is installed.

(The repo can't ship as a pip/uvx package: its load-bearing root `setup.py`
is a host CLI, not setuptools, so standard packaging can't build it. This
bootstrap is the packaging-free equivalent.)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_URL = os.environ.get(
    "TIG_SWARM_REPO", "https://github.com/Aoibheannmurray/tig-swarm-demo.git"
)
REPO_BRANCH = os.environ.get("TIG_SWARM_BRANCH", "")  # empty = default branch


def _data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "tig-swarm"


def ensure_checkout() -> Path:
    """Clone the repo (or fast-forward an existing checkout) and return it."""
    repo = _data_dir() / "repo"
    if (repo / "run.py").exists():
        subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                       capture_output=True, text=True)
        return repo
    repo.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if REPO_BRANCH:
        cmd += ["--branch", REPO_BRANCH]
    cmd += [REPO_URL, str(repo)]
    print(f"Fetching the swarm code into {repo} …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(
            "Could not fetch the swarm code:\n"
            f"  {result.stderr.strip()}\n\n"
            f"Install git, or clone manually:\n"
            f"    git clone {REPO_URL} && cd tig-swarm-demo && python3 run.py"
        )
    return repo


def main(argv: list[str]) -> int:
    if not argv:
        print('Usage: … | python3 - join "<join-link>"   (or --ui, etc.)',
              file=sys.stderr)
        return 2
    checkout = ensure_checkout()
    args = list(argv)
    if args[0] == "join":
        args = ["--join", *args[1:]]  # map the friendly verb to run.py's flag
    return subprocess.call([sys.executable, "run.py", *args], cwd=str(checkout))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
