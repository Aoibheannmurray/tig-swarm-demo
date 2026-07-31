#!/usr/bin/env python3
"""The control-ui bundle stamp: one digest, one writer.

`control-ui/dist/` is committed (contributors run the companion straight from a
clone — see .gitignore), so the repo has to be able to tell "this bundle was
built from these sources" from "someone edited src/ and forgot to rebuild".
mtimes can't answer that; a git checkout scrambles them. So dist/ carries
`.buildstamp`: a content digest of everything that feeds the Svelte build.

The stamp is only meaningful if it is rewritten by whoever rebuilds the bundle.
It used to be written solely by control_server's startup freshener, which meant
the documented workflow — edit control-ui/, `npm run build`, commit — always
produced a correct bundle with a stale stamp, and every contributor then paid a
pointless full rebuild on first launch. `npm run build` now calls this module
(the postbuild hook), and control_server imports it rather than carrying a
second copy of the algorithm that could drift.

Usage:

    python3 scripts/ui_buildstamp.py --write   # after a rebuild
    python3 scripts/ui_buildstamp.py --check   # is the committed stamp current?

stdlib only, like everything under scripts/ (see scripts/CLAUDE.md).
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_SRC_ROOT = ROOT / "control-ui"
UI_DIST = UI_SRC_ROOT / "dist"
STAMP_PATH = UI_DIST / ".buildstamp"

# Excluded from the digest: dist/ (the build's own output — including it would
# make the stamp depend on itself), node_modules/ (not a source, and absent on
# a fresh clone), and dotfiles (.DS_Store and friends must not perturb the
# digest across machines).
_SKIP_DIRS = ("dist", "node_modules")


def source_digest() -> str:
    """Content hash of everything that feeds the Svelte build — sources, static
    assets, entry HTML, configs, lockfile."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(UI_SRC_ROOT):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            h.update(path.relative_to(UI_SRC_ROOT).as_posix().encode())
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def read_stamp() -> str | None:
    """The committed stamp, or None when it's missing/unreadable."""
    try:
        return STAMP_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_stamp(digest: str | None = None) -> str:
    """Write (and return) the stamp for the current sources."""
    digest = digest or source_digest()
    STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    STAMP_PATH.write_text(digest + "\n", encoding="utf-8")
    return digest


def is_fresh() -> bool:
    """True when the committed stamp matches the sources on disk."""
    return read_stamp() == source_digest()


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--check"
    if mode == "--write":
        print(write_stamp())
        return 0
    if mode == "--check":
        actual, stamped = source_digest(), read_stamp()
        if actual == stamped:
            print(f"control-ui bundle stamp is current ({actual[:12]}…)")
            return 0
        print(
            f"control-ui/dist/.buildstamp is stale.\n"
            f"  stamped: {stamped}\n"
            f"  sources: {actual}\n"
            f"  Rebuild and commit dist/:  cd control-ui && npm run build",
            file=sys.stderr,
        )
        return 1
    print(f"usage: {Path(argv[0]).name} [--write|--check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
