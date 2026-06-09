#!/usr/bin/env python3
"""Single contributor entry point for the TIG swarm demo.

One command per session (use `python` instead of `python3` on Windows):

    python3 run.py

Phases (each only runs when it has something to do):

  1. Preflight     - check `docker` is on PATH.
  2. Init wizard   - if fleet.config.json is missing.
  3. Tacit prompt  - ask whether to add/edit tacit knowledge (default No,
                     append-mode so existing notes are preserved).
  4. Launch fleet  - same logic as `python3 scripts/run_fleet.py`.
  5. Sync-back     - on shutdown, any `- LLM:` notes appended by the agent
                     are copied from the worktree back to the source file.

The underlying scripts still work for power-user / scripted flows:

    python3 scripts/init_fleet.py        # just the wizard
    python3 setup.py tacit [<name>]      # just the tacit wizard (append)
    python3 scripts/run_fleet.py --list  # fleet status
    python3 scripts/run_fleet.py --clean # tear down worktrees
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import init_fleet
import run_fleet
import setup as setup_mod


def _tacit_phase(agents: list[dict], fleet_tacit: str | None) -> None:
    """Tacit-knowledge phase.

    Skipped entirely when stdin isn't a TTY — coding-agent / piped flows
    can't drive the interactive wizard (the guided capture asks six
    questions and the edit menu opens $EDITOR), so the right pattern
    there is for the assistant to write `tacit_knowledge.md` directly via
    its file-write tools before launching `run.py`. See AGENTS.md.

    First-run interactive experience (no source file has real content
    yet): skip the y/N preamble and go straight to the create wizard —
    there's nothing to be "adding to" yet, so the question is just noise.

    Returning-contributor experience (at least one source file has real
    content): ask "Add or edit tacit knowledge? (y/N)" first. On yes, run
    the per-path wizard, which auto-picks the edit menu (with Open in
    $EDITOR, etc.) for files that already have content and the create
    menu for any that don't yet.
    """
    if not sys.stdin.isatty():
        return

    # Dedup by destination path: agents that share a source file (the
    # default) edit it once together.
    by_source: dict[Path, list[str]] = {}
    for agent in agents:
        src, _ = run_fleet._resolve_tacit_source(agent, fleet_tacit)
        by_source.setdefault(src, []).append(agent.get("name", "?"))

    stagnation_threshold = setup_mod.read_swarm_admin().get(
        "stagnation_threshold", 2,
    )

    any_existing = any(
        setup_mod._has_user_content(p) for p in by_source.keys()
    )

    if any_existing:
        try:
            answer = input(
                "\nAdd or edit tacit knowledge for your agent(s)? (y/N): "
            ).strip().lower()
        except EOFError:
            return
        if answer not in ("y", "yes"):
            return
    # else: skip the y/N — go straight to the create menu below.

    for tk_path, names in by_source.items():
        if len(names) > 1:
            print(
                f"\n=== Tacit knowledge — shared by: {', '.join(names)} ==="
            )
        elif len(agents) > 1:
            print(f"\n=== Tacit knowledge for agent: {names[0]} ===")

        if not tk_path.exists():
            tk_path.parent.mkdir(parents=True, exist_ok=True)
            tk_path.write_text(
                setup_mod.tacit_header(stagnation_threshold)
                + "- (replace this with your own hint, or run setup again)\n",
                encoding="utf-8",
            )
            try:
                shown = tk_path.relative_to(ROOT)
            except ValueError:
                shown = tk_path
            print(f"  created {shown} (gitignored)")

        setup_mod.gather_tacit_knowledge(
            tk_path, stagnation_threshold, append=True,
        )


def main() -> int:
    fleet_path = ROOT / "fleet.config.json"
    if not fleet_path.exists():
        print("No fleet.config.json found — running setup wizard.\n")
        rc = init_fleet.run_wizard(force=False)
        if rc != 0:
            return rc
    elif sys.stdin.isatty():
        # Only ask interactive contributors. Coding-agent / piped-stdin
        # callers want a deterministic launch and would have to answer No
        # blind anyway.
        try:
            ans = input(
                "\nUpdate your fleet config (provider / model / agent count)? (y/N): "
            ).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            rc = init_fleet.run_wizard(force=True)
            if rc != 0:
                return rc

    server_url, username, swarm_password, agents, fleet_tacit = (
        run_fleet._load_fleet()
    )

    try:
        _tacit_phase(agents, fleet_tacit)
    except KeyboardInterrupt:
        print("\n  tacit prompt cancelled — continuing to launch")

    print()
    return run_fleet.cmd_run(
        agents, only=None,
        server_url=server_url,
        username=username,
        swarm_password=swarm_password,
        fleet_tacit=fleet_tacit,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
