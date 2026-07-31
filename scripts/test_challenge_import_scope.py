#!/usr/bin/env python3
"""Self-running tests for challenge-import normalisation scope.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_challenge_import_scope.py

Algorithms author against `use tig_challenges::<challenge>::*;`. Where that
anchor is missing, both the swarm codegen path (challenge_files) and the
mainnet importer (server/mainnet_seed) rewrite a `use super::*;` into it.

The hazard: `use super::*;` is ALSO the ordinary Rust idiom for an inner module
pulling in its parent's scope — `mod hpf { use super::*; }` appears in the
current knapsack mainnet winner. A bare substring replace rewrites the first
occurrence wherever it sits, so a nested one gets clobbered: the module loses
access to its parent and the challenge glob lands in the wrong scope. Only a
column-0 occurrence is the swarm anchor being migrated.

Both implementations are covered — they are separate code (server/ cannot
import from scripts/, see server/CLAUDE.md) and have drifted apart before.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server"))

import challenge_files
import mainnet_seed

_failures = 0

_IMPLS = (
    ("challenge_files.ensure_challenge_import", challenge_files.ensure_challenge_import),
    ("mainnet_seed._ensure_challenge_import", mainnet_seed._ensure_challenge_import),
)


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def test_nested_use_super_is_never_rewritten() -> None:
    print("nested `use super::*;` is left alone")
    code = (
        "use tig_challenges::knapsack::{Challenge, Solution};\n"
        "\n"
        "mod helper {\n"
        "    use super::*;\n"
        "    pub fn f() {}\n"
        "}\n"
    )
    for name, fn in _IMPLS:
        out = fn(code, "knapsack")
        check("    use super::*;\n" in out,
              f"{name}: inner module keeps its parent import")
        # The anchor still has to arrive — just not by hijacking the inner one.
        check("use tig_challenges::knapsack::*;" in out,
              f"{name}: anchor still added")
        body = out.split("mod helper {", 1)[1]
        check("use tig_challenges::knapsack::*;" not in body,
              f"{name}: anchor not injected into the inner module")


def test_top_level_legacy_anchor_is_migrated() -> None:
    print("top-level `use super::*;` still migrates")
    code = "use super::*;\n\npub fn solve_challenge() {}\n"
    for name, fn in _IMPLS:
        out = fn(code, "knapsack")
        check(out.startswith("use tig_challenges::knapsack::*;"),
              f"{name}: rewritten in place")
        check("use super::*;" not in out, f"{name}: old anchor gone")


def test_both_present() -> None:
    """Top-level migrates; the nested one survives untouched."""
    print("both present")
    code = "use super::*;\n\nmod helper {\n    use super::*;\n}\n"
    for name, fn in _IMPLS:
        out = fn(code, "knapsack")
        check(out.startswith("use tig_challenges::knapsack::*;"),
              f"{name}: top-level migrated")
        check("    use super::*;\n" in out, f"{name}: nested preserved")


def test_existing_anchor_is_a_noop() -> None:
    print("idempotence")
    code = "use tig_challenges::knapsack::*;\n\nmod helper {\n    use super::*;\n}\n"
    for name, fn in _IMPLS:
        check(fn(code, "knapsack") == code, f"{name}: unchanged when anchored")


def test_real_mainnet_winner_untouched() -> None:
    """superfast_knap_v1 has `mod hpf { use super::*; }`. It is only safe today
    because it also carries the glob anchor further down — so this guards the
    file that would have been corrupted first if that stopped being true."""
    print("real mainnet algorithm")
    path = (ROOT / "tig-monorepo" / "tig-algorithms" / "src" / "knapsack"
            / "superfast_knap_v1" / "track1.rs")
    if not path.is_file():
        print("  [skip] no local tig-monorepo checkout")
        return
    src = path.read_text(encoding="utf-8")
    check("mod hpf {" in src and "use super::*;" in src,
          "fixture still has the nested-module shape this guards")
    for name, fn in _IMPLS:
        check(fn(src, "knapsack") == src, f"{name}: leaves it byte-identical")


def main() -> int:
    test_nested_use_super_is_never_rewritten()
    test_top_level_legacy_anchor_is_migrated()
    test_both_present()
    test_existing_anchor_is_a_noop()
    test_real_mainnet_winner_untouched()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all challenge-import scope checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
