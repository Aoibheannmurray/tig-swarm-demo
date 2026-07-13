"""Tests for challenge_files.is_stub_code — the placeholder detector.

Regression for the knapsack seed livelock: the greedy seed's doc comment
mentions `unimplemented!()`, and the old substring check classified the real,
feasible seed as a stub — so exploiter agents printed "awaiting seed (cold
start)" forever on code the server had already handed them. Stub markers must
only count OUTSIDE comments.

Self-running: `python scripts/test_is_stub_code.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from challenge_files import is_stub_code  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_empty_and_blank_are_stubs():
    assert is_stub_code("")
    assert is_stub_code("   \n\t")
    print("PASS test_empty_and_blank_are_stubs")


def test_real_stub_markers_are_detected():
    assert is_stub_code("pub fn solve() { unimplemented!() }")
    assert is_stub_code("pub fn solve() { todo!() }")
    # Marker in code stays a stub even when comments are also present.
    assert is_stub_code("// a comment\npub fn solve() { unimplemented!() }")
    print("PASS test_real_stub_markers_are_detected")


def test_markers_inside_comments_do_not_count():
    assert not is_stub_code(
        "// handed instead of `unimplemented!()`, so a weaker model can refine it\n"
        "pub fn solve() { pack_greedily() }"
    )
    assert not is_stub_code(
        "/* the stub uses todo!() — this replaces it */\n"
        "pub fn solve() { pack_greedily() }"
    )
    print("PASS test_markers_inside_comments_do_not_count")


def test_every_authored_seed_is_not_a_stub():
    # The exact artifacts the server hands to seeded agents: none of the
    # repo's authored seeds may classify as a stub, or exploiters livelock.
    seeds = sorted((ROOT / "initial_algorithms").glob("*/seeds/*.rs"))
    assert seeds, "no authored seeds found — layout changed?"
    for rs in seeds:
        code = rs.read_text(encoding="utf-8")
        assert not is_stub_code(code), f"seed misclassified as stub: {rs}"
    print(f"PASS test_every_authored_seed_is_not_a_stub ({len(seeds)} seeds)")


def test_starting_code_slots_classify_correctly():
    # Every challenge has a stub/mod.rs starting-code slot. CPU slots are
    # bare placeholders (must classify as stubs — comment-stripping must not
    # hide a real unimplemented!()); GPU slots are working CUDA algorithms
    # (must NOT classify as stubs, or GPU agents would refuse to refine them).
    slots = sorted((ROOT / "initial_algorithms").glob("*/stub/mod.rs"))
    assert len(slots) == 8, f"expected 8 stub/mod.rs slots, got {slots}"
    for rs in slots:
        is_gpu = (rs.parent / "kernels.cu").is_file()
        classified_stub = is_stub_code(rs.read_text(encoding="utf-8"))
        if is_gpu:
            assert not classified_stub, f"GPU starting code misclassified as stub: {rs}"
        else:
            assert classified_stub, f"CPU placeholder not classified as stub: {rs}"
    print(f"PASS test_starting_code_slots_classify_correctly ({len(slots)} slots)")


if __name__ == "__main__":
    test_empty_and_blank_are_stubs()
    test_real_stub_markers_are_detected()
    test_markers_inside_comments_do_not_count()
    test_every_authored_seed_is_not_a_stub()
    test_starting_code_slots_classify_correctly()
    print("ALL PASS")
