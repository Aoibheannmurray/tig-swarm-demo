"""Self-running tests for the code sanitizer + non-ASCII charset guard.

Run directly: `python scripts/test_code_sanitize.py`
"""

import html
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from challenge_files import (  # noqa: E402
    _safe_write,
    find_suspicious_non_ascii,
    sanitize_source,
    validate_code,
)

# Minimal code that passes the non-charset checks in validate_code.
_OK_HEAD = "use super::*;\nfn solve_challenge(c: &Challenge) -> Solution { todo() }\n"


def test_sanitize_rewrites_confusables():
    src = "let s ‘x’ = a — b; let t = “y”;…"
    out = sanitize_source(src)
    assert out == "let s 'x' = a - b; let t = \"y\";...", repr(out)
    print("PASS sanitize rewrites smart quotes / dashes / nbsp / ellipsis")


def test_sanitize_leaves_plain_ascii_untouched():
    src = "let x = &current;\nfn f() -> i32 { 0 }\n"
    assert sanitize_source(src) == src
    print("PASS sanitize is a no-op on clean ASCII")


def test_flags_html_entity_collapse():
    # The exact production failure: DeepSeek returned `&current` already
    # collapsed to U+00A4 by a semicolon-optional HTML entity decode.
    corrupt = html.unescape("    save_solution(&current)?;")
    assert "¤" in corrupt, "precondition: html.unescape collapses &current"
    hits = find_suspicious_non_ascii(corrupt)
    assert len(hits) == 1 and hits[0][2] == "¤", hits
    line, col, _ = hits[0]
    assert line == 1 and col == 19, (line, col)
    print("PASS flags the &current->¤ collapse at the right position")


def test_does_not_flag_legit_non_ascii_contexts():
    samples = [
        'let s = "café";',          # string literal
        "// café comment",           # line comment
        "/* café */ let x = 1;",     # block comment
        "/* a /* nested é */ b */",  # nested block comment
        "let c = '€';",              # char literal (legit non-ASCII)
        'let r = r#"café "quoted""#;',  # raw string
    ]
    for s in samples:
        assert find_suspicious_non_ascii(s) == [], (s, find_suspicious_non_ascii(s))
    print("PASS legit non-ASCII in strings/chars/comments is not flagged")


def test_lifetime_does_not_swallow_following_code():
    # A lifetime `'a` must NOT be parsed as a char-literal opener, or it would
    # skip past real code and miss corruption after it.
    src = "fn f<'a>(x: &'a i32) { let y = ¤; }"
    hits = find_suspicious_non_ascii(src)
    assert len(hits) == 1 and hits[0][2] == "¤", hits
    print("PASS lifetime is not mistaken for a char literal")


def test_validate_rejects_corruption():
    err = validate_code(_OK_HEAD + "    let z = a ¤ b;\n", {})
    assert err and "Non-ASCII" in err and "U+00A4" in err, err
    print("PASS validate_code rejects ¤ corruption")


def test_validate_accepts_reversible_confusables():
    # Smart quotes are non-ASCII but sanitize fixes them, so validate (which
    # evaluates the post-sanitize form) must NOT reject them.
    code = _OK_HEAD + 'let label = "hi";\n'.replace('"', "“", 1).replace('"', "”", 1)
    assert validate_code(code, {}) is None, validate_code(code, {})
    print("PASS validate_code tolerates reversible confusables")


def test_validate_passes_clean_code():
    assert validate_code(_OK_HEAD, {}) is None
    print("PASS validate_code passes clean code")


def test_safe_write_is_lf_sanitized_and_crash_proof():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "mod.rs"
        # CRLF + smart quote + a lone surrogate (un-encodable) in one shot.
        _safe_write(p, "a\r\nlet s = “x”;\n\ud800\n")
        raw = p.read_bytes()
        assert b"\r\n" not in raw, "CRLF leaked into the written file"
        text = p.read_text(encoding="utf-8", errors="replace")
        assert "“" not in text and '"x"' in text, text
        print("PASS _safe_write forces LF, sanitizes, and survives a surrogate")


if __name__ == "__main__":
    test_sanitize_rewrites_confusables()
    test_sanitize_leaves_plain_ascii_untouched()
    test_flags_html_entity_collapse()
    test_does_not_flag_legit_non_ascii_contexts()
    test_lifetime_does_not_swallow_following_code()
    test_validate_rejects_corruption()
    test_validate_accepts_reversible_confusables()
    test_validate_passes_clean_code()
    test_safe_write_is_lf_sanitized_and_crash_proof()
    print("\nAll code-sanitize tests passed.")
