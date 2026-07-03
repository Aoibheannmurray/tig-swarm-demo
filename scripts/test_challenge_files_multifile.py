"""Unit tests for multi-file algorithm I/O + write hardening (F1, F10).

Self-running: `python scripts/test_challenge_files_multifile.py`.

Covers `challenge_files.write_files`/`read_files` (the {relpath:content} map),
stale-file pruning, single-file collapse/fallback, `..`-escape rejection,
`_safe_write` CRLF->LF normalization, and `run_loop._state_files_map`.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import challenge_files as cf   # noqa: E402
import run_loop                # noqa: E402

CONFIG = {"algorithm_path": "src/ch/algorithm/mod.rs"}


def _base():
    return Path(tempfile.mkdtemp())


def _algo_dir(base):
    return base / "src" / "ch" / "algorithm"


def test_write_read_roundtrip_multifile():
    base = _base()
    files = {
        "mod.rs": "use super::*;\nmod helpers;\n",
        "helpers.rs": "pub fn h() {}\n",
        "kernels.cu": "// kernel\n",
    }
    cf.write_files(files, CONFIG, base=base)
    assert cf.read_files(CONFIG, base=base) == files
    print("PASS test_write_read_roundtrip_multifile")


def test_write_files_prunes_stale_modules():
    base = _base()
    cf.write_files({"mod.rs": "m1\n", "old.rs": "o\n"}, CONFIG, base=base)
    # Rewrite without old.rs — it must be pruned so the build has no orphan.
    cf.write_files({"mod.rs": "m2\n", "new.rs": "n\n"}, CONFIG, base=base)
    got = cf.read_files(CONFIG, base=base)
    assert "old.rs" not in got, f"stale module not pruned: {got}"
    assert got["new.rs"] == "n\n" and got["mod.rs"] == "m2\n", got
    print("PASS test_write_files_prunes_stale_modules")


def test_empty_map_never_wipes():
    base = _base()
    cf.write_files({"mod.rs": "keep\n"}, CONFIG, base=base)
    cf.write_files({}, CONFIG, base=base)  # no-op guard
    assert cf.read_files(CONFIG, base=base) == {"mod.rs": "keep\n"}
    print("PASS test_empty_map_never_wipes")


def test_single_file_collapse_and_fallback():
    base = _base()
    cf.write_files({"mod.rs": "single\n"}, CONFIG, base=base)
    assert cf.read_files(CONFIG, base=base) == {"mod.rs": "single\n"}
    print("PASS test_single_file_collapse_and_fallback")


def test_escape_is_rejected():
    base = _base()
    try:
        cf.write_files({"mod.rs": "m\n", "../evil.rs": "x\n"}, CONFIG, base=base)
        assert False, "writing outside the algorithm dir must raise"
    except ValueError:
        pass
    assert not (base / "src" / "ch" / "evil.rs").exists(), "escape wrote outside dir"
    print("PASS test_escape_is_rejected")


def test_safe_write_normalizes_crlf():
    base = _base()
    cf.write_files({"mod.rs": "a\r\nb\r\n"}, CONFIG, base=base)
    raw = (_algo_dir(base) / "mod.rs").read_bytes()
    assert b"\r" not in raw, "CRLF was not normalized to LF"
    print("PASS test_safe_write_normalizes_crlf")


def test_state_files_map_prefers_map_then_falls_back():
    # Multi-file map wins.
    m = run_loop._state_files_map(
        {"best_algorithm_files": {"mod.rs": "a", "x.rs": "b"}}, CONFIG)
    assert m == {"mod.rs": "a", "x.rs": "b"}, m
    # Legacy single-file state -> {entry: code}.
    m2 = run_loop._state_files_map({"best_algorithm_code": "code"}, CONFIG)
    assert m2 == {"mod.rs": "code"}, m2
    print("PASS test_state_files_map_prefers_map_then_falls_back")


def test_normalize_role():
    assert run_loop._normalize_role("exploiter") == "exploiter"
    assert run_loop._normalize_role("EXPLOITER") == "exploiter"
    assert run_loop._normalize_role("explorer") == "explorer"
    assert run_loop._normalize_role("nonsense") == "explorer"
    assert run_loop._normalize_role(None) == "explorer"
    print("PASS test_normalize_role")


if __name__ == "__main__":
    test_write_read_roundtrip_multifile()
    test_write_files_prunes_stale_modules()
    test_empty_map_never_wipes()
    test_single_file_collapse_and_fallback()
    test_escape_is_rejected()
    test_safe_write_normalizes_crlf()
    test_state_files_map_prefers_map_then_falls_back()
    test_normalize_role()
    print("\nAll multi-file / hardening tests passed.")
