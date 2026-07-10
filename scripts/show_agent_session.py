#!/usr/bin/env python3
"""Render a headless `claude -p` session from a swarm worktree.

The agentic backend runs `claude -p` inside worktrees/<name>/, and Claude Code
logs the whole conversation to ~/.claude/projects/<slug>/<sessionId>.jsonl where
<slug> is the worktree's absolute path with '/' -> '-'. This prints that
transcript: the swarm's stdin prompt, the agent's thinking/text, and every tool
call + result.

Usage:
  python scripts/show_agent_session.py <agent-worktree-name> [--index N] [--full]
  python scripts/show_agent_session.py world-cup            # newest session
  python scripts/show_agent_session.py world-cup --index 1  # 2nd-newest
  python scripts/show_agent_session.py world-cup --list     # list sessions
  python scripts/show_agent_session.py --path <file.jsonl>  # explicit file
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def slug_for(worktree: Path) -> str:
    # ~/.claude/projects encodes the abs path with non-alphanumerics -> '-'.
    s = str(worktree.resolve())
    return "".join(c if c.isalnum() else "-" for c in s)


def project_dir(agent: str) -> Path:
    wt = ROOT / "worktrees" / agent
    return Path.home() / ".claude" / "projects" / slug_for(wt)


def sessions(pdir: Path) -> list[Path]:
    return sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def clip(s: str, n: int, full: bool) -> str:
    s = str(s)
    return s if full or len(s) <= n else s[:n] + f"\n… [+{len(s)-n} chars, use --full]"


def render(path: Path, full: bool, worktree: Path | None = None) -> None:
    print(f"# session {path.stem}\n# {path}\n")

    # The system prompt is NOT in the session log. Its swarm-authored half is the
    # worktree's CLAUDE.md/AGENTS.md, which the harness folds into the system
    # prompt. Print it so the trace is self-contained. (The base Claude Code
    # system prompt still isn't shown — capture that with `claude --debug` on a
    # live run.) Caveat: this is the CURRENT file on disk; if the challenge
    # switched since this session it may differ from what that run actually saw.
    if worktree is not None:
        for doc in ("CLAUDE.md", "AGENTS.md"):
            p = worktree / doc
            if p.is_file():
                print("═" * 70)
                print(f"SYSTEM  ({doc}, folded into the system prompt — current on-disk copy)\n")
                print(clip(p.read_text(), 4000, full)); print()
                break

    for line in path.open():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue  # skip queue-operation / attachment / ai-title / last-prompt noise
        msg = o.get("message") or {}
        content = msg.get("content")

        if t == "user":
            if isinstance(content, str):
                print("═" * 70)
                print("USER  (swarm stdin prompt)\n")
                print(clip(content, 4000, full)); print()
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        body = b.get("content")
                        if isinstance(body, list):
                            body = "".join(p.get("text", "") for p in body if isinstance(p, dict))
                        print(f"  └─ TOOL RESULT: {clip(body, 600, full)}")
            continue

        # assistant
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                print(f"\n[thinking] {clip(b.get('thinking',''), 500, full)}")
            elif bt == "text":
                print(f"\nASSISTANT: {clip(b.get('text',''), 2000, full)}")
            elif bt == "tool_use":
                inp = json.dumps(b.get("input", {}))
                print(f"  ⚙ TOOL CALL {b.get('name')}: {clip(inp, 400, full)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("agent", nargs="?", help="worktree name, e.g. world-cup")
    ap.add_argument("--path", help="explicit .jsonl transcript path")
    ap.add_argument("--index", type=int, default=0, help="0=newest session (default)")
    ap.add_argument("--list", action="store_true", help="list sessions and exit")
    ap.add_argument("--full", action="store_true", help="don't truncate long blocks")
    a = ap.parse_args()

    if a.path:
        render(Path(a.path), a.full); return
    if not a.agent:
        ap.error("give an agent worktree name or --path")

    worktree = ROOT / "worktrees" / a.agent
    pdir = project_dir(a.agent)
    if not pdir.is_dir():
        sys.exit(f"no session dir for '{a.agent}': {pdir}")
    sess = sessions(pdir)
    if not sess:
        sys.exit(f"no .jsonl sessions under {pdir}")
    if a.list:
        for i, p in enumerate(sess):
            print(f"[{i}] {p.stem}  ({p.stat().st_size} bytes)")
        return
    render(sess[a.index], a.full, worktree=worktree)


if __name__ == "__main__":
    main()
