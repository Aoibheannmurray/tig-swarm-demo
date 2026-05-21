#!/usr/bin/env python3
"""Single contributor entry point for the TIG swarm demo.

One command per session:

    python run.py

Phases (each only runs when it has something to do):

  1. Preflight     - check `docker` is on PATH.
  2. Init wizard   - if fleet.config.json is missing.
  3. Tacit prompt  - ask whether to add/edit tacit knowledge (default No,
                     append-mode so existing notes are preserved).
  4. Launch fleet  - same logic as `python scripts/run_fleet.py`.
  5. Sync-back     - on shutdown, any `- LLM:` notes appended by the agent
                     are copied from the worktree back to the source file.

The underlying scripts still work for power-user / scripted flows:

    python scripts/init_fleet.py        # just the wizard
    python setup.py tacit [<name>]      # just the tacit wizard (append)
    python scripts/run_fleet.py --list  # fleet status
    python scripts/run_fleet.py --clean # tear down worktrees
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
    """Prompt 'Add/edit tacit knowledge? (y/N)'. On yes, run the wizard
    once per unique source file (most fleets share one default file, so
    this is usually a single pass). Append mode — existing notes are
    preserved."""
    try:
        answer = input(
            "\nAdd or edit tacit knowledge for your agent(s)? (y/N): "
        ).strip().lower()
    except EOFError:
        return
    if answer not in ("y", "yes"):
        return

    stagnation_threshold = setup_mod.read_swarm_admin().get(
        "stagnation_threshold", 2,
    )

    # Dedup by destination path: agents that share a source file (the
    # default) edit it once together.
    by_source: dict[Path, list[str]] = {}
    for agent in agents:
        src, _ = run_fleet._resolve_tacit_source(agent, fleet_tacit)
        by_source.setdefault(src, []).append(agent.get("name", "?"))

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
                + "- (replace this with your own hint, or run setup again)\n"
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
    init_fleet._preflight_docker()

    fleet_path = ROOT / "fleet.config.json"
    if not fleet_path.exists():
        print("No fleet.config.json found — running setup wizard.\n")
        rc = init_fleet.run_wizard(force=False)
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
