#!/usr/bin/env python3
"""Self-running tests for the control-ui bundle stamp.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_ui_buildstamp.py

The headline check is the last one: the stamp committed in control-ui/dist/
matches the control-ui sources committed alongside it. That is the invariant
that keeps `python3 run.py --ui` from rebuilding the bundle on a contributor's
first launch — control_server rebuilds whenever stamp != digest, so a stale
stamp costs every contributor an npm install + build to reproduce a bundle that
was already byte-identical, and prints "control-ui sources changed" at someone
who changed nothing.

CI picks this file up through the existing `for f in scripts/test_*.py` loop.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ui_buildstamp

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    status = "ok  " if cond else "FAIL"
    if not cond:
        _failures += 1
    print(f"  [{status}] {label}")


def test_digest_is_stable_and_content_addressed() -> None:
    print("digest")

    first = ui_buildstamp.source_digest()
    check(first == ui_buildstamp.source_digest(), "digest is deterministic")
    check(len(first) == 64, "digest is a full sha256 hex")


def test_digest_ignores_dist_and_dotfiles() -> None:
    print("digest exclusions")

    # dist/ is the build's own output — folding it in would make the stamp
    # depend on itself and never settle. Dotfiles are cross-machine junk.
    before = ui_buildstamp.source_digest()

    junk = ui_buildstamp.UI_DIST / "_stamp_probe.js"
    dotjunk = ui_buildstamp.UI_SRC_ROOT / ".DS_Store"
    created = []
    try:
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("// noise\n", encoding="utf-8")
        created.append(junk)
        check(ui_buildstamp.source_digest() == before,
              "a new file under dist/ does not move the digest")

        dotjunk.write_text("noise", encoding="utf-8")
        created.append(dotjunk)
        check(ui_buildstamp.source_digest() == before,
              "a dotfile in control-ui/ does not move the digest")
    finally:
        for p in created:
            p.unlink(missing_ok=True)

    check(ui_buildstamp.source_digest() == before, "cleanup restored the digest")


def test_a_real_source_edit_moves_the_digest() -> None:
    print("digest sensitivity")

    before = ui_buildstamp.source_digest()
    probe = ui_buildstamp.UI_SRC_ROOT / "src" / "_stamp_probe.ts"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("export const probe = 1\n", encoding="utf-8")
        check(ui_buildstamp.source_digest() != before,
              "an added source file moves the digest")
    finally:
        probe.unlink(missing_ok=True)

    check(ui_buildstamp.source_digest() == before, "removing it restores the digest")


def test_write_and_read_round_trip() -> None:
    print("write/read")

    original = ui_buildstamp.read_stamp()
    with tempfile.TemporaryDirectory() as td:
        saved = ui_buildstamp.STAMP_PATH
        try:
            ui_buildstamp.STAMP_PATH = Path(td) / "nested" / ".buildstamp"
            written = ui_buildstamp.write_stamp("deadbeef")
            check(written == "deadbeef", "write_stamp returns what it wrote")
            check(ui_buildstamp.read_stamp() == "deadbeef",
                  "read_stamp round-trips (and mkdirs its parent)")
        finally:
            ui_buildstamp.STAMP_PATH = saved

    check(ui_buildstamp.read_stamp() == original,
          "the real stamp file was not touched")


def test_missing_stamp_reads_as_none() -> None:
    print("missing stamp")

    saved = ui_buildstamp.STAMP_PATH
    try:
        ui_buildstamp.STAMP_PATH = ROOT / "does" / "not" / "exist" / ".buildstamp"
        check(ui_buildstamp.read_stamp() is None, "absent stamp reads as None")
        check(not ui_buildstamp.is_fresh(), "absent stamp is not fresh")
    finally:
        ui_buildstamp.STAMP_PATH = saved


def test_committed_stamp_matches_committed_sources() -> None:
    print("committed bundle")

    stamped, actual = ui_buildstamp.read_stamp(), ui_buildstamp.source_digest()
    check(
        stamped == actual,
        "committed .buildstamp matches control-ui sources"
        + ("" if stamped == actual else
           f"\n         stamped: {stamped}"
           f"\n         sources: {actual}"
           f"\n         fix: cd control-ui && npm run build   (then commit dist/)"),
    )


def main() -> int:
    test_digest_is_stable_and_content_addressed()
    test_digest_ignores_dist_and_dotfiles()
    test_a_real_source_edit_moves_the_digest()
    test_write_and_read_round_trip()
    test_missing_stamp_reads_as_none()
    test_committed_stamp_matches_committed_sources()

    print()
    if _failures:
        print(f"{_failures} buildstamp check(s) FAILED")
        return 1
    print("all buildstamp checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
