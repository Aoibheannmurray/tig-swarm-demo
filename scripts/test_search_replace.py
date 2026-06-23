"""Self-running tests for the search/replace edit engine.

Run directly: `python scripts/test_search_replace.py` (no pytest in this repo).
"""

from search_replace import parse_blocks, apply_blocks, Block


def _blk(file, search, replace):
    body = f"<<<<<<< SEARCH {file}\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"
    return parse_blocks(body)


def test_exact_unique():
    files = {"mod.rs": "use super::*;\nlet x = 1;\n"}
    out, misses = apply_blocks(files, _blk("mod.rs", "let x = 1;", "let x = 42;"))
    assert not misses
    assert "let x = 42;" in out["mod.rs"]
    print("PASS test_exact_unique")


def test_whitespace_insensitive():
    files = {"mod.rs": "fn f() {\n          let x = 1;\n}\n"}
    out, misses = apply_blocks(files, _blk("mod.rs", "let x = 1;", "let x = 2;"))
    assert not misses, misses
    assert "let x = 2;" in out["mod.rs"]
    print("PASS test_whitespace_insensitive")


def test_ambiguous_not_applied():
    files = {"mod.rs": "let a = 1;\nlet a = 1;\n"}
    out, misses = apply_blocks(files, _blk("mod.rs", "let a = 1;", "let a = 9;"))
    assert len(misses) == 1 and misses[0].reason == "ambiguous"
    assert out == files
    print("PASS test_ambiguous_not_applied")


def test_not_found_is_miss():
    files = {"mod.rs": "let a = 1;\n"}
    out, misses = apply_blocks(files, _blk("mod.rs", "let z = 0;", "let z = 1;"))
    assert len(misses) == 1 and misses[0].reason == "not_found"
    print("PASS test_not_found_is_miss")


def test_multifile_targeting():
    files = {"mod.rs": "mod helpers;\n", "helpers.rs": "pub fn h() -> i32 { 1 }\n"}
    out, misses = apply_blocks(
        files, _blk("helpers.rs", "pub fn h() -> i32 { 1 }", "pub fn h() -> i32 { 2 }")
    )
    assert not misses
    assert "{ 2 }" in out["helpers.rs"]
    assert out["mod.rs"] == files["mod.rs"]
    print("PASS test_multifile_targeting")


def test_pathless_single_file():
    files = {"mod.rs": "let q = 0;\n"}
    out, misses = apply_blocks(
        files, parse_blocks("<<<<<<< SEARCH\nlet q = 0;\n=======\nlet q = 5;\n>>>>>>> REPLACE")
    )
    assert not misses and "let q = 5;" in out["mod.rs"]
    print("PASS test_pathless_single_file")


def test_pathless_multifile_is_miss():
    # Without a path on a multi-file algorithm the target is ambiguous.
    files = {"mod.rs": "a\n", "helpers.rs": "a\n"}
    out, misses = apply_blocks(
        files, parse_blocks("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE")
    )
    assert len(misses) == 1 and misses[0].reason == "no_file"
    print("PASS test_pathless_multifile_is_miss")


def test_multiple_ordered_blocks():
    files = {"mod.rs": "a\nb\nc\n"}
    body = (
        "<<<<<<< SEARCH mod.rs\na\n=======\nA\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH mod.rs\nc\n=======\nC\n>>>>>>> REPLACE"
    )
    out, misses = apply_blocks(files, parse_blocks(body))
    assert not misses and out["mod.rs"] == "A\nb\nC\n"
    print("PASS test_multiple_ordered_blocks")


def test_partial_apply_skips_only_misses():
    files = {"mod.rs": "keep1\ntarget\nkeep2\n"}
    body = (
        "<<<<<<< SEARCH mod.rs\ntarget\n=======\nCHANGED\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH mod.rs\nNOPE\n=======\nX\n>>>>>>> REPLACE"
    )
    out, misses = apply_blocks(files, parse_blocks(body))
    assert "CHANGED" in out["mod.rs"]      # good block applied
    assert len(misses) == 1                 # bad block reported
    print("PASS test_partial_apply_skips_only_misses")


if __name__ == "__main__":
    test_exact_unique()
    test_whitespace_insensitive()
    test_ambiguous_not_applied()
    test_not_found_is_miss()
    test_multifile_targeting()
    test_pathless_single_file()
    test_pathless_multifile_is_miss()
    test_multiple_ordered_blocks()
    test_partial_apply_skips_only_misses()
    print("\nAll search_replace tests passed.")
