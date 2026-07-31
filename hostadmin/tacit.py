"""Tacit-knowledge wizard: guided capture, paste, and $EDITOR flows.

This is the slice run.py and control_server.py embed (via `import setup`).
Moved verbatim from the root setup.py."""

from __future__ import annotations

import json
import os
import shlex
import subprocess as sp
import sys
from pathlib import Path

from .config_io import ROOT, read_swarm_admin


def tacit_header(stagnation_threshold: int = 2) -> str:
    """Standard header text written into the personal tacit-knowledge file.
    Parameterised on stagnation_threshold so the >= condition matches the
    swarm's actual config (the server reads it from swarm.admin.json on the
    host, POSTed at `setup.py create` time)."""
    return (
    "# Personal tacit knowledge\n\n"
    "Private strategy notes for your local agents; never uploaded. All\n"
    "agents in the fleet share this file unless a per-agent\n"
    "`tacit_knowledge` path is set in `fleet.config.json`.\n\n"
    "Shown to an agent as extra hints when it stagnates\n"
    f"(`my_runs_since_improvement >= {stagnation_threshold}`).\n\n"
    "Agents may also append their own `- LLM:` lessons here before a\n"
    "trajectory reset. They won't if `tacit_write` is off or the swarm's\n"
    "failed-attempts archive is on (lessons then go to the server instead).\n\n"
    "## Strategies\n\n"
    )


# ── Tacit-knowledge guided capture ────────────────────────────────────


TACIT_QUESTIONS = [
    {
        "title": "What do you try when the standard approaches stop working?",
        "hint": (
            "The things you try after the obvious ones fail."
        ),
    },
    {
        "title": "What rules of thumb have you picked up that aren't written down?",
        "hint": (
            "The practical know-how you'd give a new student."
        ),
    },
    {
        "title": "What looks promising on paper but underperforms in practice?",
        "hint": (
            "Things that sound good in talks or papers but lose to simpler\n"
            "approaches when you actually run them."
        ),
    },
    {
        "title": "Anything else worth writing down?",
        "hint": (
            "Judgment calls, instincts, anything that didn't fit above.\n"
            "Skip if you've already covered everything."
        ),
    },
]


def _read_block_until_dot() -> str:
    """Read multi-line input terminated by `.` on its own line, EOF, or
    an empty first line (= skip). Returns the captured text (stripped) or
    empty string."""
    lines: list[str] = []
    while True:
        prompt = "  > " if not lines else "    "
        try:
            line = input(prompt)
        except EOFError:
            break
        if line.strip() == ".":
            break
        if not lines and not line.strip():
            return ""
        lines.append(line)
    return "\n".join(lines).rstrip()


def _guided_tacit_capture() -> str:
    """Walk the user through TACIT_QUESTIONS and assemble a markdown body
    (no header — caller prepends `tacit_header`). Returns empty string if
    every question was skipped or the user cancelled."""
    bar = "═" * 72
    rule = "─" * 72
    print()
    print(bar)
    print("  Tacit knowledge — guided capture".center(72))
    print(bar)
    print(
        "\n  Tacit knowledge is the practical know-how you rarely write down:\n"
        "  the strategies and judgment calls you reach for instinctively.\n"
        "  Your local agents read these notes whenever they stagnate.\n"
        f"\n  {len(TACIT_QUESTIONS)} short prompts follow. Press Enter on an empty\n"
        "  answer to skip any one of them; finish a multi-line answer with a\n"
        "  single `.` on its own line.\n"
    )

    sections: list[str] = []
    for idx, q in enumerate(TACIT_QUESTIONS, 1):
        print(rule)
        print(f"  Question {idx} / {len(TACIT_QUESTIONS)}")
        print()
        print(f"  {q['title']}")
        print()
        for line in q["hint"].splitlines():
            print(f"    {line}")
        print()
        try:
            body = _read_block_until_dot()
        except KeyboardInterrupt:
            print("\n  cancelled — partial input discarded")
            return ""
        if body:
            sections.append(f"### {q['title']}\n\n{body}")
            print("  ✓ recorded\n")
        else:
            print("  · skipped\n")

    print(rule)
    return "\n\n".join(sections)


_TACIT_STUB_LINE = "- (replace this with your own hint, or run setup again)\n"


def _has_user_content(tk_path: Path) -> bool:
    """True when the tacit file has substantive content beyond the
    auto-generated header + placeholder stub. Used to decide whether the
    wizard should show the 'create' menu (empty/stubbed) or the 'edit'
    menu (real notes already there)."""
    if not tk_path.exists():
        return False
    body = tk_path.read_text(encoding="utf-8", errors="replace").replace(_TACIT_STUB_LINE, "")
    if "## Strategies" in body:
        _, after = body.split("## Strategies", 1)
        return bool(after.strip())
    return bool(body.strip())


def _append_or_seed(
    tk_path: Path, new_body: str, stagnation_threshold: int, *, append: bool,
) -> None:
    """Write `new_body` into the tacit file. When append=True and the file
    already has user content, the body is appended at the end; otherwise the
    file is (re)written from the header + body. The boilerplate "replace
    this with your own hint" stub line is stripped on first real append so
    the contributor's notes don't get prefixed with placeholder noise."""
    if append and tk_path.exists():
        existing = tk_path.read_text(encoding="utf-8", errors="replace").replace(_TACIT_STUB_LINE, "")
        if not existing.endswith("\n"):
            existing += "\n"
        tk_path.write_text(existing + "\n" + new_body + "\n", encoding="utf-8")
    else:
        tk_path.write_text(tacit_header(stagnation_threshold) + new_body + "\n", encoding="utf-8")


def _gather_via_guided(
    tk_path: Path, stagnation_threshold: int, *, append: bool,
) -> None:
    body = _guided_tacit_capture()
    if not body:
        print("  every question skipped — leaving existing file in place")
        return
    _append_or_seed(tk_path, body, stagnation_threshold, append=append)
    verb = "appended to" if append and tk_path.exists() else "wrote"
    print(f"\n  {verb} {tk_path.relative_to(ROOT)}")


def _gather_via_paste(
    tk_path: Path, stagnation_threshold: int, *, append: bool,
) -> None:
    print(
        "\nPaste or type ALL of your strategies below — one per line, any\n"
        "format you like. When finished, press Ctrl-D (Unix/macOS) or\n"
        "Ctrl-Z then Enter (Windows) to submit.\n"
    )
    try:
        text = sys.stdin.read()
    except KeyboardInterrupt:
        print("\n  cancelled — leaving existing file in place")
        return
    text = text.strip()
    if not text:
        print("  no text entered; leaving existing file in place")
        return
    _append_or_seed(tk_path, text, stagnation_threshold, append=append)
    verb = "appended to" if append and tk_path.exists() else "wrote"
    print(f"  {verb} {tk_path.relative_to(ROOT)}")


def _open_in_editor(tk_path: Path) -> None:
    """Hand the file off to the contributor's $EDITOR (or $VISUAL) for
    direct editing. Falls back to a sensible platform default. Whatever
    they save when the editor exits is the new file content."""
    editor = (
        os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or ("notepad" if os.name == "nt" else "vi")
    )
    # $EDITOR may carry arguments ("code --wait", "emacs -nw") — split it
    # into argv rather than treating the whole string as one binary name.
    # posix=False on Windows keeps backslashed paths intact.
    editor_argv = shlex.split(editor, posix=(os.name != "nt")) or [editor]
    try:
        sp.run([*editor_argv, str(tk_path)], check=False)
    except FileNotFoundError:
        print(
            f"  could not launch editor {editor!r}. Set $EDITOR or $VISUAL "
            "to your preferred editor and try again."
        )
        return
    try:
        shown = tk_path.relative_to(ROOT)
    except ValueError:
        shown = tk_path
    print(f"  editor closed; saved as-is ({shown})")


def gather_tacit_knowledge(
    tk_path: Path, stagnation_threshold: int = 2, *, append: bool = True,
) -> None:
    """Populate or edit the personal tacit-knowledge file.

    Auto-detects whether the file has user content yet and shows the
    appropriate menu.

    Create flow (no real content yet):
      1. Guided capture — answer a few short prompts (recommended).
      2. Paste a single block — power-user escape hatch.
      3. Skip — don't add any tacit knowledge yet.

    Edit flow (file already has user content):
      1. Add more via guided capture (recommended; appends).
      2. Add more via paste (appends).
      3. Open the file in your $EDITOR for direct hand-editing.
      4. Cancel — leave the file as-is.

    With append=True (default), guided/paste modes append to existing
    content. The editor mode edits the file in place.
    """
    is_edit = _has_user_content(tk_path)

    if is_edit:
        try:
            rel = tk_path.relative_to(ROOT)
        except ValueError:
            rel = tk_path
        print(f"\n── Editing tacit knowledge ({rel}) ──")
        print("How would you like to update it?")
        print("  1. Add more via guided capture (recommended; appends)")
        print("  2. Add more via paste (appends)")
        print("  3. Open the file in your $EDITOR")
        print("  4. Cancel — leave the file as-is\n")
        valid = ("1", "2", "3", "4")
        default = "1"
        prompt = "Choice 1/2/3/4 [1]: "
    else:
        print(
            "\n── Tacit knowledge (optional) ──\n"
            "Give your local agent private strategy hints for when it gets\n"
            "stuck. This file is gitignored and never sent to the server.\n"
        )
        print("How would you like to provide them?")
        print("  1. Guided capture — answer a few short prompts (recommended)")
        print("  2. Paste a single block of free-form text")
        print("  3. Skip — don't add any tacit knowledge yet\n")
        valid = ("1", "2", "3")
        default = "1"
        prompt = "Choice 1/2/3 [1]: "

    while True:
        try:
            choice = input(prompt).strip() or default
        except EOFError:
            # Non-interactive caller (closed stdin, piped, headless agent).
            # Treat as "skip / cancel" rather than crashing the launcher.
            print("  (stdin closed — skipping tacit wizard)")
            return
        if choice in valid:
            break
        print(f"  invalid choice; pick one of {', '.join(valid)}")

    if is_edit:
        if choice == "1":
            _gather_via_guided(tk_path, stagnation_threshold, append=append)
        elif choice == "2":
            _gather_via_paste(tk_path, stagnation_threshold, append=append)
        elif choice == "3":
            _open_in_editor(tk_path)
        else:  # "4"
            try:
                shown = tk_path.relative_to(ROOT)
            except ValueError:
                shown = tk_path
            print(f"  no change (edit {shown} any time)")
    else:
        if choice == "1":
            _gather_via_guided(tk_path, stagnation_threshold, append=append)
        elif choice == "2":
            _gather_via_paste(tk_path, stagnation_threshold, append=append)
        else:  # "3"
            try:
                shown = tk_path.relative_to(ROOT)
            except ValueError:
                shown = tk_path
            print(f"  no hints added (edit {shown} any time)")


def run_tacit(agent_name: str | None = None) -> int:
    """Interactive tacit-knowledge helper. The one piece of the old wizard
    that survives: paste-a-block UX is awkward to express in JSON, so the
    file pointed to by the fleet entry's `tacit_knowledge` field is what
    the user edits via this command.

    With no argument, picks the first agent in fleet.config.json (or the
    only one if there's exactly one). With an argument, picks that named
    agent. Creates the tacit file if missing and hooks it back into
    fleet.config.json's `tacit_knowledge` field if currently unset."""
    fleet_path = ROOT / "fleet.config.json"
    if not fleet_path.exists():
        print(
            "fleet.config.json not found — run `python setup.py create` (host) "
            "or copy fleet.config.example.json (contributor) first."
        )
        return 1
    try:
        fleet = json.loads(fleet_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"fleet.config.json is malformed: {e}")
        return 1
    agents = fleet.get("agents") or []
    if not agents:
        print("fleet.config.json has no agents.")
        return 1

    fleet_tacit = fleet.get("tacit_knowledge") or None

    # Resolve the source path. Precedence: per-agent override > top-level
    # fleet default > implicit shared `tacit_knowledge.md`. By default all
    # agents share the same file so their LLM-distilled lessons collate
    # into one pool — set per-agent / top-level only to override that.
    if agent_name:
        match = next((a for a in agents if a.get("name") == agent_name), None)
        if not match:
            print(f"agent {agent_name!r} not found in fleet.config.json.")
            print(f"available: {', '.join(a.get('name', '?') for a in agents)}")
            return 1
        explicit_rel = match.get("tacit_knowledge") or fleet_tacit
    else:
        match = None
        explicit_rel = fleet_tacit

    tk_rel = explicit_rel or "tacit_knowledge.md"
    tk_path = Path(tk_rel)
    if not tk_path.is_absolute():
        tk_path = ROOT / tk_path

    # The stagnation threshold lives in swarm.admin.json on the host and is
    # not visible to a plain contributor — use the documented default.
    stagnation_threshold = read_swarm_admin().get("stagnation_threshold", 2)
    if not tk_path.exists():
        tk_path.parent.mkdir(parents=True, exist_ok=True)
        tk_path.write_text(
            tacit_header(stagnation_threshold)
            + "- (replace this with your own hint, or run setup again)\n",
            encoding="utf-8",
        )
        print(f"  created {tk_path.relative_to(ROOT)} (gitignored)")

    if match is None:
        # Editing the shared file directly — show which agents will pick it up.
        sharing = [
            a.get("name", "?") for a in agents
            if not a.get("tacit_knowledge")
        ]
        if sharing:
            print(f"  shared file — used by: {', '.join(sharing)}")

    gather_tacit_knowledge(tk_path, stagnation_threshold)
    return 0
