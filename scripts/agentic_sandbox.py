"""Sandbox primitives shared by all agentic backends.

Worktree creation, per-iteration state reset, and reading the hypothesis the
agent wrote back. Backend-specific scaffolding (CLAUDE.md, settings.json,
AGENTS.md, codex config, etc.) lives in agentic_backends.py — this module is
the bits any tooled agent harness needs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKTREES_DIR = ROOT / "worktrees"

# Where the agent writes its hypothesis before stopping. Read back by the
# loop after the agent exits; if missing/malformed, the loop falls back to a
# synthesized hypothesis so the iteration still publishes.
HYPOTHESIS_RELPATH = ".swarm/hypothesis.json"

# Worktree branch namespace for agentic runs that aren't part of a fleet.
# Fleet runs use the fleet/<name> namespace (see run_fleet.py); agentic
# single-contributor runs use agentic/<short-id> so the two don't collide.
_AGENTIC_BRANCH_PREFIX = "agentic"


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _existing_worktree_paths() -> set[str]:
    out = _git(["worktree", "list", "--porcelain"])
    paths: set[str] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.add(line[len("worktree "):])
    return paths


def _branch_exists(branch: str) -> bool:
    return bool(_git(["branch", "--list", branch]))


def _is_main_checkout() -> bool:
    """True iff ROOT is the main git checkout, not a worktree.

    In the main checkout, `.git` is a directory; in any worktree, `.git` is
    a file containing `gitdir: <path-to-common-dir>`. Used to skip creating
    a nested worktree when run_loop runs as a fleet child (cwd is already
    `worktrees/<name>/`).
    """
    return (ROOT / ".git").is_dir()


def resolve_workdir(agent_id: str, agent_name: str | None = None) -> Path:
    """Pick (or create) the worktree the agentic backend should run in.

    Two scenarios:
      - Standalone (`python scripts/run_loop.py`): ROOT is the main checkout,
        so we lazily create `worktrees/<safe-name>/` keyed by agent_id and
        return it.
      - Fleet child (spawned by run_fleet.py): ROOT is already
        `worktrees/<fleet-name>/`. Don't nest — return ROOT directly so the
        agent edits the same worktree the fleet allocated.
    """
    if not _is_main_checkout():
        return ROOT
    return ensure_worktree(worktree_name_for_agent(agent_id, agent_name))


def ensure_worktree(name: str, *, branch_prefix: str = _AGENTIC_BRANCH_PREFIX) -> Path:
    """Create (or reuse) a git worktree at worktrees/<name>/.

    Same recovery semantics as scripts/run_fleet.py: if the directory exists
    but isn't tracked as a worktree (stale state from a crashed run), prune
    and rebuild. Idempotent — call every iteration if you like.
    """
    path = WORKTREES_DIR / name
    branch = f"{branch_prefix}/{name}"
    known = _existing_worktree_paths()

    if path.exists() and str(path) not in known:
        _git(["worktree", "prune"])
        shutil.rmtree(path)

    if not path.exists():
        WORKTREES_DIR.mkdir(exist_ok=True)
        if _branch_exists(branch):
            _git(["worktree", "add", str(path), branch])
        else:
            _git(["worktree", "add", "-b", branch, str(path)])

    return path


def seed_worktree_config(workdir: Path) -> None:
    """Copy swarm.config.json from the main checkout into the worktree.

    The agent doesn't read this — but `scripts/benchmark.py` does when the
    loop runs it with cwd=workdir. agent.config.json is intentionally NOT
    copied: agent identity stays in the main checkout, the worktree is just
    a workspace.
    """
    src = ROOT / "swarm.config.json"
    if src.exists():
        shutil.copy2(src, workdir / "swarm.config.json")


def reset_iteration_state(workdir: Path) -> None:
    """Clear per-iteration scratch state before launching the agent.

    Right now that's just .swarm/hypothesis.json — if a previous iteration's
    file lingered, the loop would mis-attribute it to this iteration's
    edits.
    """
    swarm_dir = workdir / ".swarm"
    swarm_dir.mkdir(exist_ok=True)
    hyp = workdir / HYPOTHESIS_RELPATH
    if hyp.exists():
        hyp.unlink()


_STRATEGY_TAG_FALLBACK = "other"


def read_agent_hypothesis(workdir: Path) -> dict | None:
    """Read .swarm/hypothesis.json. Returns None on missing/malformed.

    The agent is instructed (via CLAUDE.md) to write {title, description,
    strategy_tag, notes}. Missing fields get defaults; if the file is
    unreadable JSON, returns None so the caller can fall back to a
    synthesized hypothesis from stdout.
    """
    hyp = workdir / HYPOTHESIS_RELPATH
    if not hyp.exists():
        return None
    try:
        data = json.loads(hyp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "title": str(data.get("title") or "untitled")[:200],
        "description": str(data.get("description") or "")[:2000],
        "strategy_tag": str(data.get("strategy_tag") or _STRATEGY_TAG_FALLBACK),
        "notes": str(data.get("notes") or "")[:2000],
    }


def synthesize_hypothesis_from_stdout(stdout: str) -> dict:
    """Fallback when the agent forgot to write .swarm/hypothesis.json.

    Pulls the first non-empty line as a title and uses the head of the
    output as the description. Strategy tag defaults to "other". The
    iteration still publishes — the dashboard just shows a generic entry.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    title = lines[0] if lines else "agentic iteration (no hypothesis file)"
    return {
        "title": title[:200],
        "description": (stdout or "Agent did not write .swarm/hypothesis.json")[:500],
        "strategy_tag": _STRATEGY_TAG_FALLBACK,
        "notes": "synthesized from agent stdout — agent did not write hypothesis.json",
    }


_SAFE_NAME_RE = re.compile(r"[^a-z0-9-]+")


def worktree_name_for_agent(agent_id: str, agent_name: str | None = None) -> str:
    """Stable, filesystem-safe worktree name for a non-fleet agentic run.

    Used when run_loop.py runs as a single contributor (no fleet); each
    swarm agent gets its own worktree keyed by a short slice of agent_id so
    re-runs with the same agent_id land back in the same workspace.
    """
    short = agent_id[:8] if agent_id else "anon"
    base = (agent_name or "agentic").lower()
    base = _SAFE_NAME_RE.sub("-", base).strip("-") or "agentic"
    return f"{base}-{short}"
