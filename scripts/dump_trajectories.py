#!/usr/bin/env python3
"""Dump swarm trajectory code evolution for offline analysis.

Pulls per-agent experiment histories (with full algorithm code) from the server
and writes each iteration's code plus unified diffs between consecutive versions.
Point Claude Code at the output file to get an LLM analysis of the trajectory.

Usage:
    # Dump all agents on the active challenge
    python scripts/dump_trajectories.py

    # Specific challenge
    python scripts/dump_trajectories.py --challenge energy_arbitrage

    # Single agent
    python scripts/dump_trajectories.py --agent-id abc123

    # Custom output path
    python scripts/dump_trajectories.py -o /tmp/analysis.md
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

SERVER = os.environ.get("TIG_SWARM_SERVER") or "https://t1-production-0047.up.railway.app///"
if SERVER.startswith("$"):
    sys.exit(
        "dump_trajectories.py: server URL not configured. Run "
        "`python setup.py join <swarm-url>` (or set TIG_SWARM_SERVER)."
    )
SERVER = SERVER.rstrip("/")


def fetch(path: str) -> dict:
    url = f"{SERVER}{path}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def fmt_score(score) -> str:
    if score is None:
        return "n/a"
    return f"{score:,.0f}"


def unified_diff(old: str, new: str, old_label: str, new_label: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines,
                                fromfile=old_label, tofile=new_label, n=3)
    return "".join(diff)


def main():
    parser = argparse.ArgumentParser(
        description="Dump swarm trajectory code for LLM analysis")
    parser.add_argument("--challenge", help="Challenge name (defaults to active)")
    parser.add_argument("--agent-id", help="Dump only this agent")
    parser.add_argument("-o", "--output",
                        help="Output file (default: trajectory_<challenge>.md)")
    args = parser.parse_args()

    challenge_param = f"?challenge={args.challenge}" if args.challenge else ""

    leaderboard = fetch(f"/api/leaderboard{challenge_param}")
    challenge = leaderboard.get("challenge", args.challenge or "unknown")
    entries = leaderboard.get("entries", [])

    if args.agent_id:
        entries = [e for e in entries if e["agent_id"] == args.agent_id]
        if not entries:
            entries = [{"agent_id": args.agent_id, "agent_name": "unknown",
                        "runs": "?", "improvements": "?", "current_score": None}]

    output_path = args.output or f"trajectory_{challenge}.md"

    lines = []
    lines.append(f"# Trajectory Code Evolution — {challenge}")
    lines.append(f"")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Challenge: {challenge}")
    lines.append(f"Agents: {len(entries)}")
    lines.append(f"")

    # Leaderboard summary
    lines.append(f"## Leaderboard")
    lines.append(f"")
    for e in entries:
        lines.append(
            f"- **{e.get('agent_name', '?')}** — "
            f"score {fmt_score(e.get('current_score'))}, "
            f"{e.get('runs', 0)} runs, "
            f"{e.get('improvements', 0)} improvements"
        )
    lines.append(f"")

    for entry in entries:
        agent_id = entry["agent_id"]
        agent_name = entry.get("agent_name", "unknown")

        print(f"Fetching {agent_name}...", file=sys.stderr)
        agent_data = fetch(
            f"/api/agent_experiments?agent_id={agent_id}"
            f"&include_code=true"
            f"{'&challenge=' + challenge if challenge else ''}"
        )
        experiments = agent_data.get("experiments", [])

        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## {agent_name} — {len(experiments)} iterations")
        lines.append(f"")

        if not experiments:
            lines.append(f"_No experiments recorded._")
            lines.append(f"")
            continue

        prev_code = ""
        for i, e in enumerate(experiments, 1):
            score = e.get("score")
            is_best = e.get("beats_own_best", False)
            feasible = e.get("feasible", True)
            tag = e.get("strategy_tag", "?")
            title = e.get("title") or "untitled"
            desc = e.get("description") or ""
            code = e.get("algorithm_code") or ""
            ts = e.get("created_at", "")

            marker = ""
            if is_best:
                marker = " NEW BEST"
            if not feasible:
                marker += " INFEASIBLE"

            lines.append(f"### Iteration {i}{marker}")
            lines.append(f"")
            lines.append(f"- **Score:** {fmt_score(score)}")
            lines.append(f"- **Tag:** {tag}")
            lines.append(f"- **Title:** {title}")
            if desc:
                lines.append(f"- **Description:** {desc}")
            lines.append(f"- **Time:** {ts}")
            lines.append(f"")

            if i == 1:
                lines.append(f"#### Initial code")
                lines.append(f"")
                lines.append(f"```rust")
                lines.append(code)
                lines.append(f"```")
            else:
                diff = unified_diff(prev_code, code,
                                    f"iteration-{i-1}", f"iteration-{i}")
                if diff.strip():
                    lines.append(f"#### Diff from iteration {i-1}")
                    lines.append(f"")
                    lines.append(f"```diff")
                    lines.append(diff.rstrip())
                    lines.append(f"```")
                else:
                    lines.append(f"_No code change from previous iteration._")

            lines.append(f"")
            prev_code = code

    report = "\n".join(lines)
    Path(output_path).write_text(report)

    size_kb = len(report.encode()) / 1024
    print(f"Wrote {output_path} ({size_kb:.0f} KB, {len(lines)} lines)",
          file=sys.stderr)
    print(f"  {len(entries)} agents, {challenge} challenge", file=sys.stderr)
    print(f"\nTo analyze, tell Claude Code:", file=sys.stderr)
    print(f"  Read {output_path} and analyze the code evolution", file=sys.stderr)


if __name__ == "__main__":
    main()
