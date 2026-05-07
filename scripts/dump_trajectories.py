#!/usr/bin/env python3
"""Dump swarm trajectory code evolution for offline analysis.

Pulls experiments grouped by trajectory (not by agent) and writes each
trajectory's code evolution with unified diffs between consecutive iterations.
Point Claude Code at the output file for LLM analysis.

Usage:
    # Dump all trajectories on the active challenge
    python scripts/dump_trajectories.py

    # Specific challenge
    python scripts/dump_trajectories.py --challenge energy_arbitrage

    # Single trajectory
    python scripts/dump_trajectories.py --trajectory-id abc123

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
        description="Dump trajectory code evolution for LLM analysis")
    parser.add_argument("--challenge", help="Challenge name (defaults to active)")
    parser.add_argument("--trajectory-id", help="Dump only this trajectory")
    parser.add_argument("-o", "--output",
                        help="Output file (default: trajectory_<challenge>.md)")
    args = parser.parse_args()

    params = []
    if args.challenge:
        params.append(f"challenge={args.challenge}")
    if args.trajectory_id:
        params.append(f"trajectory_id={args.trajectory_id}")
    params.append("include_code=true")
    query = "&".join(params)

    print("Fetching trajectory experiments...", file=sys.stderr)
    data = fetch(f"/api/trajectory_experiments?{query}")
    challenge = data.get("challenge", args.challenge or "unknown")
    trajectories = data.get("trajectories", {})

    # Also fetch trajectory metadata for lifecycle info
    traj_meta_params = f"?challenge={challenge}" if args.challenge else ""
    traj_meta = fetch(f"/api/trajectories{traj_meta_params}")
    meta_by_id = {t["id"]: t for t in traj_meta.get("trajectories", [])}

    output_path = args.output or f"trajectory_{challenge}.md"

    lines = []
    lines.append(f"# Trajectory Code Evolution — {challenge}")
    lines.append(f"")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Challenge: {challenge}")
    lines.append(f"Trajectories: {len(trajectories)}")
    lines.append(f"")

    # Sort trajectories by their best score (descending)
    def traj_best_score(tid):
        meta = meta_by_id.get(tid)
        if meta and meta.get("current_score") is not None:
            return meta["current_score"]
        exps = trajectories[tid]
        scores = [e["score"] for e in exps if e.get("score") is not None]
        return max(scores) if scores else float("-inf")

    sorted_tids = sorted(trajectories.keys(), key=traj_best_score, reverse=True)

    # Summary table
    lines.append(f"## Summary")
    lines.append(f"")
    lines.append(f"| Trajectory | Status | Score | Edits | Improvements | Agents |")
    lines.append(f"|------------|--------|-------|-------|--------------|--------|")
    for tid in sorted_tids:
        meta = meta_by_id.get(tid, {})
        exps = trajectories[tid]
        agents = sorted(set(e.get("agent_name", "?") for e in exps))
        status = meta.get("status", "?")
        lines.append(
            f"| {tid[:8]} | {status} "
            f"| {fmt_score(meta.get('current_score'))} "
            f"| {meta.get('num_edits', len(exps))} "
            f"| {meta.get('num_improvements', sum(1 for e in exps if e.get('beats_own_best')))} "
            f"| {', '.join(agents)} |"
        )
    lines.append(f"")

    # Per-trajectory code evolution
    for tid in sorted_tids:
        exps = trajectories[tid]
        meta = meta_by_id.get(tid, {})
        status = meta.get("status", "active")
        agents = sorted(set(e.get("agent_name", "?") for e in exps))
        score_hist = meta.get("score_history", [])

        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## Trajectory {tid[:8]} [{status.upper()}] — {len(exps)} iterations")
        lines.append(f"")
        lines.append(f"- **Agents:** {', '.join(agents)}")
        lines.append(f"- **Best score:** {fmt_score(meta.get('current_score'))}")
        lines.append(f"- **Momentum:** {meta.get('momentum', 0):.4f}")
        if score_hist:
            lines.append(f"- **Score progression:** {' → '.join(fmt_score(h['score']) for h in score_hist)}")
        lines.append(f"")

        if not exps:
            lines.append(f"_No experiments recorded._")
            lines.append(f"")
            continue

        prev_code = ""
        for i, e in enumerate(exps, 1):
            score = e.get("score")
            is_best = e.get("beats_own_best", False)
            feasible = e.get("feasible", True)
            tag = e.get("strategy_tag", "?")
            title = e.get("title") or "untitled"
            desc = e.get("description") or ""
            code = e.get("algorithm_code") or ""
            ts = e.get("created_at", "")
            agent = e.get("agent_name", "?")

            marker = ""
            if is_best:
                marker = " NEW BEST"
            if not feasible:
                marker += " INFEASIBLE"

            lines.append(f"### Iteration {i}{marker}")
            lines.append(f"")
            lines.append(f"- **Score:** {fmt_score(score)}")
            lines.append(f"- **Agent:** {agent}")
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
    print(f"  {len(trajectories)} trajectories, {challenge} challenge",
          file=sys.stderr)


if __name__ == "__main__":
    main()
