"""File I/O, code parsing, and validation for swarm challenge files.

Handles reading/writing algorithm source (mod.rs) and optional CUDA
kernels (kernels.cu), plus LLM response parsing and basic code validation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Canonical separator the LLM is told to use. The regex also accepts a few
# common variations (extra dashes/equals, missing trailing dashes).
_KERNEL_SEPARATOR = "// --- kernels.cu ---"
_KERNEL_SEPARATOR_RE = re.compile(
    r"^[ \t]*(?://|/\*)[ \t]*[-=]*[ \t]*kernels\.cu[ \t]*[-=]*[ \t]*(?:\*/)?[ \t]*$",
    re.MULTILINE,
)


# ── Path helpers ───────────────────────────────────────────────────


def algo_path(config: dict) -> Path:
    ap = config.get("algorithm_path")
    if not ap:
        sys.exit(".swarm-cache.json missing `algorithm_path` — run `setup.py sync`.")
    return ROOT / ap


def kernel_path(config: dict) -> Path | None:
    kp = config.get("kernel_path")
    return ROOT / kp if kp else None


def algorithm_dir(config: dict, base: Path = ROOT) -> Path:
    """Directory that holds the algorithm's source files. The entry file
    (`mod.rs`) lives here; multi-file algorithms add sibling `.rs` files (and,
    on GPU, `kernels.cu`) under it. `base` lets callers root the dir somewhere
    other than the repo root (e.g. an agent worktree)."""
    ap = config.get("algorithm_path")
    if not ap:
        sys.exit(".swarm-cache.json missing `algorithm_path` — run `setup.py sync`.")
    return (base / ap).parent


def entry_name(config: dict) -> str:
    """Filename of the entry file (normally `mod.rs`)."""
    return algo_path(config).name


# Source files that make up an algorithm bundle. Anything else in the dir
# (build artifacts, stray files) is ignored by the files-map.
_ALGO_FILE_SUFFIXES = (".rs", ".cu", ".cuh")


# ── Sanitization & charset guard ───────────────────────────────────
#
# Models routinely emit "typographic" Unicode where ASCII belongs — smart
# quotes and em-dashes from markdown rendering, non-breaking spaces, ellipses —
# and occasionally HTML-entity artifacts (e.g. `&current` collapsing to `¤`,
# U+00A4, because `&curren` is a semicolon-optional legacy named entity). These
# compile-fail with opaque `unknown start of token` errors deep in a build.
#
# Two-layer defense:
#   1. `sanitize_source` deterministically rewrites the *reversible* confusables
#      to their ASCII equivalents on every write — silent, lossless recovery.
#   2. `find_suspicious_non_ascii` (used by `validate_code`) flags whatever
#      non-ASCII survives in *code* positions (outside strings/char-literals/
#      comments), where Rust is effectively always ASCII. Un-reversible junk
#      like `¤` is caught here and routed to the repair/skip loop *before* a
#      ~minute-long Docker build, instead of blowing up at the compiler.

# Reversible confusables → ASCII. Deliberately conservative: only characters
# that are never legitimately wanted in solver source (no `×`/`÷`/Greek, which
# could be meaningful in a string literal).
_CONFUSABLE_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",   # ‘ ’ ‚ ‛
    "“": '"', "”": '"', "„": '"', "‟": '"',   # “ ” „ ‟
    "′": "'", "″": '"',                                 # ′ ″ primes
    "–": "-", "—": "-", "―": "-", "−": "-",   # – — ― −
    " ": " ", " ": " ", " ": " ", " ": " ",   # nbsp/thin/figure
    "​": "", "﻿": "",                                   # zero-width space / BOM
    "…": "...",                                              # …
}
_CONFUSABLE_TRANS = str.maketrans(_CONFUSABLE_MAP)


def sanitize_source(text: str) -> str:
    """Rewrite reversible Unicode confusables (smart quotes, dashes, non-breaking
    spaces, ellipsis, zero-width/BOM) to their ASCII equivalents and normalize
    line endings to LF. Lossless for real solver code; see the section comment
    above. Normalizing CR here (not just `newline="\\n"` at write time) strips
    CRLF a model itself emitted, keeping the on-disk solver LF-only."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_CONFUSABLE_TRANS)


# A Rust char literal: 'x', '\n', '\x41', '\u{1F600}', '\''. Used to skip over
# char literals (whose contents may legitimately be non-ASCII, e.g. '€') while
# scanning. A lifetime ('a, 'static) won't match (no closing quote), so it's
# correctly left in CODE state.
_CHAR_LIT_RE = re.compile(r"'(?:\\u\{[0-9A-Fa-f]+\}|\\x[0-9A-Fa-f]{2}|\\.|[^'\\\n])'")
# Opening of a (byte) raw string: r"…", r#"…"#, br##"…"##, …
_RAW_OPEN_RE = re.compile(r'b?r(#*)"')


def _line_col(text: str, idx: int) -> tuple[int, int]:
    """1-based line, 1-based column for a character offset."""
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1) + 1
    return line, col


def find_suspicious_non_ascii(code: str) -> list[tuple[int, int, str]]:
    """Find non-ASCII characters sitting in *code* positions — outside string
    literals, char literals, and comments. Returns (line, col, char) tuples.

    Rust source is ASCII outside literals/comments, so a hit is almost always
    model corruption. A lightweight lexer skips the contexts where non-ASCII is
    legitimate; it errs toward flagging (treating an ambiguous `'` as code, not
    a char literal) so corruption is never silently skipped — a false positive
    just costs one cheap repair round."""
    hits: list[tuple[int, int, str]] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        # Line comment: // … to end of line.
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            nl = code.find("\n", i + 2)
            i = n if nl == -1 else nl
            continue
        # Block comment: /* … */, nestable in Rust.
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if code[i] == "/" and i + 1 < n and code[i + 1] == "*":
                    depth, i = depth + 1, i + 2
                elif code[i] == "*" and i + 1 < n and code[i + 1] == "/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        # Raw string: r"…" / r#"…"# / br##"…"## — no escapes inside.
        rm = _RAW_OPEN_RE.match(code, i)
        if rm:
            closer = '"' + "#" * len(rm.group(1))
            end = code.find(closer, rm.end())
            i = n if end == -1 else end + len(closer)
            continue
        # Normal/byte string: "…" with backslash escapes.
        if c == '"':
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if code[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        # Char literal: skip it (contents may be legit non-ASCII).
        cm = _CHAR_LIT_RE.match(code, i)
        if cm:
            i = cm.end()
            continue
        # Plain code position.
        if ord(c) > 0x7F:
            line, col = _line_col(code, i)
            hits.append((line, col, c))
        i += 1
    return hits


# ── Read / write ───────────────────────────────────────────────────


# Algorithm/kernel writes go through `_safe_write`: UTF-8 (Rust source is UTF-8
# by definition), `newline="\n"` so a Windows host can't inject CRLF into the
# solver (mirrors c3_compute._write_container_file and the repo's LF
# .gitattributes), `errors="replace"` so an un-encodable stray (e.g. a lone
# surrogate from a bad decode) degrades to a marker instead of crashing the run,
# and `sanitize_source` to clean reversible confusables. Reads use
# errors="replace" so a file left half-written by a prior crash still loads.
def _safe_write(path: Path, text: str) -> None:
    path.write_text(
        sanitize_source(text), encoding="utf-8", errors="replace", newline="\n"
    )


def read_optional(path: Path | None) -> str:
    if path and path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def write_algorithm(code: str, config: dict) -> None:
    p = algo_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    _safe_write(p, code)


def write_kernel(code: str, config: dict) -> None:
    p = kernel_path(config)
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        _safe_write(p, code)


def read_algorithm(config: dict) -> str:
    p = algo_path(config)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def read_kernel(config: dict) -> str:
    p = kernel_path(config)
    if p and p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return ""


# ── Multi-file (files-map) read / write ────────────────────────────
#
# An algorithm is represented as a {relpath: content} map keyed by paths
# RELATIVE to the algorithm directory (POSIX separators). A single-file
# algorithm is just {"mod.rs": <code>}. The entry file (mod.rs) declares any
# submodules (`mod helpers;`). This subsumes the GPU kernel: kernels.cu shows
# up as a normal entry in the map.


def read_files(config: dict, base: Path = ROOT) -> dict[str, str]:
    """All algorithm source files on disk as a {relpath: content} map.

    Walks the algorithm directory for source files (`.rs`/`.cu`/`.cuh`). Falls
    back to the single-file path if the directory layout isn't present yet.
    `base` roots the lookup (defaults to the repo root; pass a worktree)."""
    d = algorithm_dir(config, base)
    files: dict[str, str] = {}
    if d.exists() and d.is_dir():
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix in _ALGO_FILE_SUFFIXES:
                rel = p.relative_to(d).as_posix()
                files[rel] = p.read_text(encoding="utf-8", errors="replace")
    if not files:
        # No directory yet — fall back to the single entry file if it exists.
        entry = d / entry_name(config)
        if entry.exists():
            files[entry_name(config)] = entry.read_text(
                encoding="utf-8", errors="replace")
    return files


def write_files(files: dict[str, str], config: dict, base: Path = ROOT) -> None:
    """Write a {relpath: content} map into the algorithm directory, then prune
    any stale source files no longer in the map.

    Pruning keeps multi-file rewrites from leaving orphaned `.rs` modules that
    break the build. It only runs when the map is non-empty and contains the
    entry file, so a bad/empty map can never wipe the algorithm."""
    if not files:
        return
    d = algorithm_dir(config, base)
    keys = set(files.keys())
    for rel, content in files.items():
        # Keep all writes inside the algorithm dir (defend against `..`).
        dest = (d / rel).resolve()
        if not str(dest).startswith(str(d.resolve())):
            raise ValueError(f"refusing to write outside algorithm dir: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _safe_write(dest, content)
    if entry_name(config) in keys and d.exists():
        for p in d.rglob("*"):
            if p.is_file() and p.suffix in _ALGO_FILE_SUFFIXES:
                rel = p.relative_to(d).as_posix()
                if rel not in keys:
                    p.unlink()


def read_challenge_md() -> str:
    p = ROOT / "CHALLENGE.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def read_tacit_knowledge() -> str:
    p = ROOT / "tacit_knowledge_personal.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def is_stub_code(code: str) -> bool:
    """True when the algorithm is a placeholder that can't produce solutions."""
    if not code or not code.strip():
        return True
    return "unimplemented!" in code or "todo!" in code


# ── Response parsing ───────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


_FENCED_BLOCK_RE = re.compile(r"```(?:[\w+-]*)\s*\n(.*?)\n```", re.DOTALL)


def ensure_challenge_import(code: str, challenge: str) -> str:
    """Normalize the entry file's challenge-type import to the mainnet form.

    Algorithms author against `use tig_challenges::<challenge>::*;` — the ONE
    import that resolves in BOTH builds: the swarm crate (src/lib.rs's
    `extern crate self as tig_challenges` makes it an alias of
    `crate::<challenge>::*`) and the TIG-docker slot (the baked tig-bench
    images), so algorithms move between the swarm and mainnet with no import
    swapping. Legacy `use super::*;` (the old swarm-only anchor) is rewritten
    in place, which migrates pre-parity trajectories as agents touch them. If
    no anchor is present at all (agents sometimes rewrite the import block and
    drop it), the import is inserted: worst case is an unused-import warning,
    never an error.
    """
    if not code:
        return code
    anchor = f"use tig_challenges::{challenge}::*;"
    if anchor in code:
        return code
    if "use super::*;" in code:
        return code.replace("use super::*;", anchor, 1)
    lines = code.splitlines(keepends=True)
    # Insert before the first top-level `use` (which sits after any leading
    # comments and `#![...]` inner attributes), else at the very top.
    for i, line in enumerate(lines):
        if line.lstrip().startswith("use "):
            lines.insert(i, anchor + "\n")
            return "".join(lines)
    return anchor + "\n" + code


# Anchor probes for chopping chatty-LLM prose that precedes the code. Exact
# import lines only (never a bare "use " — prose can start a line with it).
_PROSE_CHOP_PROBES = ("use tig_challenges::", "use super::*;", "use crate::")


def _clean_rust(text: str) -> str:
    # Defensive against chatty LLMs that ignore "no preamble / no fences":
    # if the response wraps the code in ```...```, take the first fenced
    # block's contents; then drop any prose still sitting before the first
    # recognizable import anchor.
    text = text.strip()
    m = _FENCED_BLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()
    else:
        text = _strip_fences(text)
    hits = [i for i in (text.find(p) for p in _PROSE_CHOP_PROBES) if i > 0]
    if hits:
        text = text[min(hits):]
    return text.strip()


def parse_code(text: str) -> str:
    return _clean_rust(text)


def parse_gpu_code(text: str) -> tuple[str, str]:
    """Extract Rust + CUDA code from a GPU two-file LLM response.

    Returns (rust_code, cuda_code). If no separator is found,
    returns the whole text as rust_code and empty cuda_code.

    The Rust half goes through the same chatty-model hardening as the CPU
    `parse_code` path (`_clean_rust`): smaller models prepend an English
    preamble and/or wrap the code in a ```rust fence, which the bare
    `_strip_fences` (only triggered when the WHOLE response is fenced) lets
    leak into mod.rs — producing `unknown start of token` / `expected ! or
    ::, found at` compile errors from prose being compiled as Rust.
    """
    text = _strip_fences(text)
    m = _KERNEL_SEPARATOR_RE.search(text)
    if m is None:
        return _clean_rust(text), ""
    rust = text[: m.start()]
    cuda = text[m.end():].strip()
    return _clean_rust(rust), _strip_fences(cuda)


# Challenges whose `solve_challenge` + training loop are harness-owned and
# non-editable: the agent supplies ONLY the optimizer hooks. For these we must
# NOT require `fn solve_challenge(` (it lives in the locked challenge module),
# and instead require the three optimizer hooks the harness calls.
_OPTIMIZER_HOOK_CHALLENGES = {"neuralnet_optimizer"}
_OPTIMIZER_HOOKS = (
    "fn optimizer_init_state(",
    "fn optimizer_query_at_params(",
    "fn optimizer_step(",
)


def validate_code(code: str, config: dict | None = None) -> str | None:
    """Basic sanity check on LLM-generated code.

    Returns None if valid, or an error description."""
    # Charset guard: catch corruption that survives sanitization (e.g. `¤` from
    # an HTML-entity collapse) before it reaches the compiler. Evaluate the
    # post-sanitize form so reversible confusables — which the write path fixes
    # automatically — don't trip this.
    bad = find_suspicious_non_ascii(sanitize_source(code))
    if bad:
        line, col, ch = bad[0]
        return (
            f"Non-ASCII character {ch!r} (U+{ord(ch):04X}) at line {line}, col {col} "
            f"sits in code, outside any string or comment — this is almost certainly "
            f"corruption (an HTML-entity artifact or a pasted glyph), not valid Rust. "
            f"Replace it with the intended ASCII character. "
            f"({len(bad)} suspicious character(s) found.)"
        )
    challenge = (config or {}).get("challenge")
    if challenge:
        anchor = f"use tig_challenges::{challenge}::*;"
        # Legacy `use super::*;` is accepted (pre-parity trajectories adopted
        # from the server) — ensure_challenge_import migrates it on the next
        # agent write; new code must carry the mainnet-form anchor.
        if anchor not in code and "use super::*;" not in code:
            return (
                f"`{anchor}` is missing — import the challenge types via this "
                f"exact line (it compiles both in the swarm and on TIG mainnet)."
            )
    if challenge in _OPTIMIZER_HOOK_CHALLENGES:
        if "fn solve_challenge(" in code:
            return (
                "`solve_challenge` is harness-owned for this challenge and must NOT be "
                "defined here — the benchmark runs the fixed training loop and calls "
                "your optimizer hooks. Remove your `solve_challenge` and implement only "
                "`optimizer_init_state` / `optimizer_query_at_params` / `optimizer_step`."
            )
        missing = [h for h in _OPTIMIZER_HOOKS if h not in code]
        if missing:
            names = ", ".join(h[3:-1] for h in missing)  # strip "fn " and "("
            return (
                f"Missing required optimizer hook(s): {names}. `solve_challenge` and "
                "the training loop are harness-owned — implement only the optimizer "
                "functions, and keep them `pub fn` with their exact signatures."
            )
    elif "fn solve_challenge(" not in code:
        return "`fn solve_challenge(` not found — the function signature must not change."
    if is_stub_code(code):
        return (
            "Code still contains `unimplemented!()` or `todo!()` — "
            "you must provide a complete working implementation."
        )
    return None


# ── Mainnet → swarm reshaping ──────────────────────────────────────
#
# Functions whose definitions are harness-owned on optimizer-hook challenges:
# a fetched mainnet algorithm ships them in its `mod.rs`, but in the swarm they
# live in the locked challenge module (`src/<challenge>/mod.rs`) and must NOT
# appear in the agent's file (the validator rejects them). We strip them before
# seeding so the bundle matches the swarm's "hooks-only" layout.
_HARNESS_OWNED_FNS = ("solve_challenge", "training_loop")


def _match_brace(code: str, open_idx: int) -> int | None:
    """Index of the `}` matching the `{` at `open_idx`, skipping braces inside
    Rust string/char literals and line/block comments. None if unbalanced."""
    depth = 0
    i = open_idx
    n = len(code)
    while i < n:
        c = code[i]
        two = code[i:i + 2]
        if two == "//":
            nl = code.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if two == "/*":
            end = code.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if c == '"':
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if code[i] == '"':
                    break
                i += 1
            i += 1
            continue
        if c == "'":
            # Char literal or lifetime. Treat `'x'` / `'\n'` as a literal; a
            # lifetime (`'a`) has no closing quote on the token and is skipped
            # harmlessly since we only special-case a matched pair.
            j = i + 1
            if j < n and code[j] == "\\":
                j += 2
            else:
                j += 1
            if j < n and code[j] == "'":
                i = j + 1
                continue
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _strip_top_level_fn(code: str, name: str) -> tuple[str, bool]:
    """Remove a `fn <name>` / `pub fn <name>` definition (body + any attached
    leading attribute/doc-comment lines) from `code`. Returns (code, removed)."""
    m = re.search(r"\b(?:pub\s*(?:\([^)]*\)\s*)?)?fn\s+" + re.escape(name) + r"\b",
                  code)
    if not m:
        return code, False
    # Body opens at the first `{` after the signature (parameter parens / `<>`
    # generics / `-> Type` returns never introduce a `{`).
    open_idx = code.find("{", m.end())
    if open_idx == -1:
        return code, False
    close_idx = _match_brace(code, open_idx)
    if close_idx is None:
        return code, False
    # Extend removal up to the start of the fn's line, then back over any
    # contiguous attribute (`#[...]`) / doc-comment (`///`, `//!`) lines so we
    # don't leave a dangling doc comment with nothing to attach to.
    start = code.rfind("\n", 0, m.start()) + 1
    while start > 0:
        prev_end = start - 1
        prev_begin = code.rfind("\n", 0, prev_end) + 1
        prev = code[prev_begin:prev_end].strip()
        if prev.startswith("#[") or prev.startswith("///") or prev.startswith("//!"):
            start = prev_begin
        else:
            break
    end = close_idx + 1
    if end < len(code) and code[end] == "\n":
        end += 1
    return code[:start] + code[end:], True


def reshape_mainnet_for_swarm(
    challenge: str, files: dict[str, str]
) -> tuple[dict[str, str] | None, str | None]:
    """Best-effort reshape of a fetched mainnet algorithm bundle into the
    swarm's expected layout. Returns (files, None) on success or (None, reason)
    when it can't produce something the swarm validator accepts — the caller
    should skip-with-error so the host notices.

    The only structural delta today is optimizer-hook challenges
    (`neuralnet_optimizer`): their `solve_challenge` + training loop are
    harness-owned, so we strip those definitions from the entry file and keep
    only the optimizer hooks. Imports are kept in the mainnet form verbatim
    (`use tig_challenges::<ch>::*;` compiles unchanged in the swarm — see
    ensure_challenge_import); we only add the anchor if absent. All challenges
    are validated via `validate_code` before seeding.
    """
    entry = "mod.rs"
    if entry not in files:
        return None, "bundle has no mod.rs entry file"
    out = dict(files)
    code = out[entry]

    if challenge in _OPTIMIZER_HOOK_CHALLENGES:
        for fn in _HARNESS_OWNED_FNS:
            code, _removed = _strip_top_level_fn(code, fn)

    code = ensure_challenge_import(code, challenge)
    out[entry] = code

    err = validate_code(code, {"challenge": challenge})
    if err:
        return None, err
    return out, None


# ── ChallengeFiles ─────────────────────────────────────────────────


class ChallengeFiles:
    """Encapsulates file I/O differences between CPU and GPU challenges."""

    def __init__(self, config: dict):
        self._config = config
        self.is_gpu = bool(config.get("is_gpu"))

    def parse_response(self, text: str) -> tuple[str, str]:
        # Normalize the challenge-type import after parsing: LLMs sometimes
        # rewrite the import block and drop the anchor (previously auto-fixed
        # inside _clean_rust, which no longer knows the challenge).
        challenge = self._config["challenge"]
        if self.is_gpu:
            code, kernel = parse_gpu_code(text)
            return ensure_challenge_import(code, challenge), kernel
        return ensure_challenge_import(parse_code(text), challenge), ""

    def write(self, code: str, kernel: str = "") -> None:
        write_algorithm(code, self._config)
        if self.is_gpu and kernel:
            write_kernel(kernel, self._config)

    def read(self) -> tuple[str, str]:
        code = read_algorithm(self._config)
        kernel = read_kernel(self._config) if self.is_gpu else ""
        return code, kernel

    # ── Multi-file accessors ──
    @property
    def entry_name(self) -> str:
        return entry_name(self._config)

    def read_files(self) -> dict[str, str]:
        """All algorithm source files as a {relpath: content} map."""
        return read_files(self._config)

    def write_files(self, files: dict[str, str]) -> None:
        write_files(files, self._config)

    def is_multifile(self) -> bool:
        return len(self.read_files()) > 1

    def separator_suffix(self) -> str:
        if self.is_gpu:
            return (
                "\nReturn BOTH files separated by: // --- kernels.cu ---"
                "\nEnsure kernel function names match between mod.rs and kernels.cu."
            )
        return ""

    def describe_write(self, code: str, kernel: str) -> str:
        if self.is_gpu and kernel:
            return "Wrote both mod.rs + kernels.cu"
        if self.is_gpu:
            return "Wrote mod.rs only (no kernel changes)"
        return f"Wrote mod.rs ({len(code)} chars)"

    def describe_parse(self, code: str, kernel: str) -> str:
        if self.is_gpu:
            if kernel:
                return f"Got two-file response (rust: {len(code)} chars, cuda: {len(kernel)} chars)"
            return f"WARNING: No kernel separator found — got rust only ({len(code)} chars)"
        return f"Got code ({len(code)} chars)"
