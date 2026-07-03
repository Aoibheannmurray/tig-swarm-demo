"""Self-running tests for the deterministic cleaner pre-pass (Tier 0).

Shapes mirror the 2026-07-03 incident: a six-file map where one track file
was byte-identical to another (modulo a generated header comment) AND never
dispatched, and where the entry declares every module.

Run directly: `python scripts/test_cleaner_prepass.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cleaner_prepass import (  # noqa: E402
    merge_identical_files,
    remove_unreachable_files,
    run_prepass,
    total_chars,
)

SOLVER_BODY = "pub fn solve() -> u32 {\n    42\n}\n"


def _incident_map():
    """mod.rs declares a/b/c; c is a header-only-different copy of b; d.rs is
    on disk but never declared anywhere (unreachable)."""
    return {
        "mod.rs": (
            "use tig_challenges::job_scheduling::*;\n"
            "mod a;\nmod b;\nmod c;\n"
            "pub fn solve_challenge() -> u32 { a::solve() + b::solve() + c::solve() }\n"
        ),
        "a.rs": "// FLAT: a.rs\npub fn solve() -> u32 {\n    7\n}\n",
        "b.rs": "// FLAT: b.rs\n" + SOLVER_BODY,
        "c.rs": "// FLAT: c.rs - auto-generated from c/ sub-dir\n" + SOLVER_BODY,
        "d.rs": "// orphan\npub fn dead() {}\n",
    }


def test_unreachable_removal():
    out, actions = remove_unreachable_files(_incident_map(), "mod.rs")
    assert "d.rs" not in out and len(actions) == 1
    assert set(out) == {"mod.rs", "a.rs", "b.rs", "c.rs"}
    print("PASS undeclared file is removed")


def test_transitive_reachability():
    fm = {
        "mod.rs": "mod x;\nfn main() { x::go() }\n",
        "x.rs": "mod y;\npub fn go() { y::go() }\n",
        "y.rs": "pub fn go() {}\n",
    }
    out, actions = remove_unreachable_files(fm, "mod.rs")
    assert set(out) == set(fm) and actions == []
    print("PASS transitively-declared files are kept")


def test_identical_merge_rewires_references():
    out, actions = merge_identical_files(_incident_map(), "mod.rs")
    assert "c.rs" not in out and any("merged" in a for a in actions)
    mod = out["mod.rs"]
    assert "mod c;" not in mod, "duplicate's mod decl must be dropped"
    assert "c::solve()" not in mod and mod.count("b::solve()") == 2, \
        "duplicate's paths must point at the kept module"
    assert "a.rs" in out, "non-duplicate must survive"
    print("PASS identical files merge with mod-decl + path rewiring")


def test_inline_mod_not_treated_as_file():
    fm = {
        "mod.rs": "mod helpers { pub fn f() {} }\nmod real;\nfn m() { real::f() }\n",
        "real.rs": "pub fn f() {}\n",
    }
    out, actions = remove_unreachable_files(fm, "mod.rs")
    assert set(out) == set(fm) and actions == []
    print("PASS inline `mod x { }` blocks are ignored")


def test_full_prepass_incident_shape():
    fm = _incident_map()
    out, actions = run_prepass(fm, "mod.rs")
    assert set(out) == {"mod.rs", "a.rs", "b.rs"}
    assert len(actions) == 2  # one merge + one unreachable removal
    assert total_chars(out) < total_chars(fm)
    print("PASS full pre-pass on incident-shaped map: 5 files -> 3")


def test_noop_on_clean_map():
    fm = {
        "mod.rs": "mod a;\nfn m() { a::f() }\n",
        "a.rs": "pub fn f() {}\n",
    }
    out, actions = run_prepass(fm, "mod.rs")
    assert out == fm and actions == []
    print("PASS clean map is untouched (empty action log)")


if __name__ == "__main__":
    test_unreachable_removal()
    test_transitive_reachability()
    test_identical_merge_rewires_references()
    test_inline_mod_not_treated_as_file()
    test_full_prepass_incident_shape()
    test_noop_on_clean_map()
    print("\nAll cleaner pre-pass tests passed.")
