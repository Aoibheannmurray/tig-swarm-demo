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
        sys.exit("swarm.config.json missing `algorithm_path` — run `setup.py sync`.")
    return ROOT / ap


def kernel_path(config: dict) -> Path | None:
    kp = config.get("kernel_path")
    return ROOT / kp if kp else None


# ── Read / write ───────────────────────────────────────────────────


def read_optional(path: Path | None) -> str:
    if path and path.exists():
        return path.read_text()
    return ""


def write_algorithm(code: str, config: dict) -> None:
    p = algo_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code)


def write_kernel(code: str, config: dict) -> None:
    p = kernel_path(config)
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code)


def read_algorithm(config: dict) -> str:
    p = algo_path(config)
    return p.read_text() if p.exists() else ""


def read_kernel(config: dict) -> str:
    p = kernel_path(config)
    if p and p.exists():
        return p.read_text()
    return ""


def read_challenge_md() -> str:
    p = ROOT / "CHALLENGE.md"
    return p.read_text() if p.exists() else ""


def read_tacit_knowledge() -> str:
    p = ROOT / "tacit_knowledge_personal.md"
    return p.read_text() if p.exists() else ""


def is_stub_code(code: str) -> bool:
    """True when the algorithm is a placeholder that can't produce solutions."""
    if not code or not code.strip():
        return True
    return "unimplemented!" in code or "todo!" in code


# ── Multi-file algorithm directory helpers ─────────────────────────


def algo_dir(config: dict) -> Path:
    """Directory holding the active algorithm's mod.rs + sibling modules.

    Always the parent of `algorithm_path` — even for single-file challenges
    this is well-defined; the directory just only contains mod.rs in that
    case. Lets the rest of the helpers stay file-set-agnostic.
    """
    return algo_path(config).parent


def read_algorithm_dir(config: dict) -> dict[str, str]:
    """Snapshot every `.rs` / `.cu` file under the algorithm directory as a
    {relative_path: contents} dict (paths relative to the algorithm dir,
    e.g. ``"mod.rs"``, ``"builder.rs"``, ``"kernels.cu"``). Returns ``{}``
    when the directory doesn't exist yet.

    Used at publish time so the iteration's full multi-file snapshot
    rides the wire alongside the legacy single-string ``algorithm_code``.
    """
    d = algo_dir(config)
    if not d.exists():
        return {}
    out: dict[str, str] = {}
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in (".rs", ".cu"):
            continue
        rel = p.relative_to(d).as_posix()
        out[rel] = p.read_text()
    return out


def write_algorithm_dir(files: dict[str, str], algo_dir_path: Path) -> None:
    """Write a multi-file algorithm dict to disk under ``algo_dir_path``,
    *and* delete any pre-existing `.rs` / `.cu` files in that directory
    that are not in ``files``.

    Cleanup matters because the directory persists across iterations:
    without it, a trajectory reset that drops `helper.rs` would leave a
    stale orphan whose ``mod helper;`` is no longer declared in mod.rs —
    typically a hard cargo error, sometimes a silent compile of dead code
    that masks the agent's actual edits.
    """
    algo_dir_path.mkdir(parents=True, exist_ok=True)
    keep = {rel.replace("\\", "/") for rel in files}
    for existing in algo_dir_path.rglob("*"):
        if not existing.is_file():
            continue
        if existing.suffix not in (".rs", ".cu"):
            continue
        rel = existing.relative_to(algo_dir_path).as_posix()
        if rel not in keep:
            existing.unlink()
    for rel, body in files.items():
        out = algo_dir_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)


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

# Per-file header used by the multi-file LLM output format. Generalises the
# GPU `// --- kernels.cu ---` pattern; relative path captured in group 1.
# Accepts trailing `===` decoration and either `//` or `/*…*/` comment
# styles so an LLM that misremembers the delimiter still parses.
_FILE_HEADER_RE = re.compile(
    r"^[ \t]*(?://|/\*)[ \t]*=+[ \t]*file:[ \t]*(?P<path>[^=\s][^=]*?)"
    r"[ \t]*=+[ \t]*(?:\*/)?[ \t]*$",
    re.MULTILINE,
)

# Explicit delete marker used in diff-mode multi-file responses. Lets the
# LLM remove a file without having to remember which other files it must
# keep (the "omit = unchanged" semantic means there's no other way to
# express a deletion).
_DELETE_HEADER_RE = re.compile(
    r"^[ \t]*(?://|/\*)[ \t]*=+[ \t]*delete:[ \t]*(?P<path>[^=\s][^=]*?)"
    r"[ \t]*=+[ \t]*(?:\*/)?[ \t]*$",
    re.MULTILINE,
)


def parse_code(text: str) -> str:
    # Defensive against chatty LLMs that ignore "no preamble / no fences":
    # if the response wraps the code in ```...```, take the first fenced
    # block's contents; then drop any prose still sitting before the
    # required `use super::*;` anchor.
    text = text.strip()
    m = _FENCED_BLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()
    else:
        text = _strip_fences(text)
    idx = text.find("use super::*;")
    if idx > 0:
        text = text[idx:]
    return text.strip()


def parse_multi_file_response(text: str, default_path: str = "mod.rs") -> dict[str, str]:
    """Split a multi-file LLM response into ``{rel_path: contents}``.

    Expected format — one header per file, contents follow until the next
    header or end of text::

        // === file: mod.rs ===
        use super::*;
        ...

        // === file: builder.rs ===
        ...

    Back-compat: if no header is found, the entire response (after fence
    stripping + `use super::*;` anchoring) becomes the value at
    ``default_path``. That lets the multi-file parser also accept the old
    single-file response shape — useful when the LLM forgets the header
    on a one-file edit.

    Chatty-LLM safety: when headers ARE present, any text before the first
    header is treated as chat preamble and DISCARDED, not assigned to
    ``default_path``. Claude/GPT routinely ignore "no preamble" rules and
    emit a brief "Looking at this task, I'll …" before the first header;
    mapping that prose to mod.rs would silently overwrite a valid baseline
    mod.rs with English text and break the next compile.
    """
    text = text.strip()
    m = _FENCED_BLOCK_RE.search(text)
    if m and not _FILE_HEADER_RE.search(text):
        # Pure single-block response wrapped in a markdown fence — unwrap
        # before searching for headers. If the response *contains* headers
        # we leave the fence in place; the section parser will strip per
        # block.
        text = m.group(1).strip()

    matches = list(_FILE_HEADER_RE.finditer(text))
    if not matches:
        body = _strip_fences(text)
        idx = body.find("use super::*;")
        if idx > 0:
            body = body[idx:]
        return {default_path: body.strip()} if body.strip() else {}

    out: dict[str, str] = {}
    # Headers present → drop the prefix (it's chat preamble). See the
    # chatty-LLM safety note in the docstring above.
    for i, m in enumerate(matches):
        path = m.group("path").strip().strip("\"'")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _strip_fences(text[start:end])
        if body.strip():
            out[path] = body.strip()
    return out


def parse_multi_file_diff(
    text: str, default_path: str = "mod.rs",
) -> tuple[dict[str, str], set[str]]:
    """Parse a diff-style multi-file response into ``(changes, deletes)``.

    Same header grammar as ``parse_multi_file_response`` plus an explicit
    delete marker::

        // === file: builder.rs ===     # add or replace builder.rs
        ...full file contents...

        // === delete: gene_pool.rs === # remove gene_pool.rs

    Semantics (caller responsibility): files NOT mentioned in either
    header set are kept unchanged. ``changes`` and ``deletes`` are
    disjoint — a path appearing in both is treated as a change (the file
    block wins, the delete is dropped) so a model that mis-orders headers
    doesn't accidentally wipe a file it also rewrote.

    Back-compat: if no headers of either kind are found, the entire
    response is returned as ``{default_path: text}`` with no deletes —
    same as the old single-file fallback.
    """
    text_clean = text.strip()
    m = _FENCED_BLOCK_RE.search(text_clean)
    if m and not _FILE_HEADER_RE.search(text_clean) and not _DELETE_HEADER_RE.search(text_clean):
        text_clean = m.group(1).strip()

    deletes: set[str] = set()
    for dm in _DELETE_HEADER_RE.finditer(text_clean):
        path = dm.group("path").strip().strip("\"'")
        if path:
            deletes.add(path)

    # Strip delete-header lines before file parsing — otherwise a delete
    # marker that appears before the first `// === file: ===` header gets
    # captured as the mod.rs prefix body.
    file_only_text = _DELETE_HEADER_RE.sub("", text)
    changes = parse_multi_file_response(file_only_text, default_path=default_path)

    # If a path appears in both, the change wins and the delete is dropped
    # — so a mis-ordered response can't silently wipe a file it rewrote.
    deletes -= changes.keys()
    return changes, deletes


def parse_gpu_code(text: str) -> tuple[str, str]:
    """Extract Rust + CUDA code from a GPU two-file LLM response.

    Returns (rust_code, cuda_code). If no separator is found,
    returns the whole text as rust_code and empty cuda_code.
    """
    text = _strip_fences(text)
    m = _KERNEL_SEPARATOR_RE.search(text)
    if m is None:
        return text.strip(), ""
    rust = text[: m.start()].strip()
    cuda = text[m.end():].strip()
    return _strip_fences(rust), _strip_fences(cuda)


def validate_code(code: str) -> str | None:
    """Basic sanity check on a single-file (mod.rs) LLM response.

    For single-file challenges, mod.rs IS the algorithm and must contain
    ``use super::*;`` (parent types like ``Challenge`` / ``Solution`` are
    not in scope otherwise) plus ``fn solve_challenge(``.

    For multi-file challenges use :func:`validate_files` instead — the
    entry point may live in a sibling (e.g. ``solver.rs`` for
    job_scheduling) and mod.rs is mostly module wiring.

    Returns None if valid, or an error description."""
    if "use super::*;" not in code:
        return "`use super::*;` is missing — it must remain as the first import."
    if "fn solve_challenge(" not in code:
        return "`fn solve_challenge(` not found — the function signature must not change."
    if is_stub_code(code):
        return (
            "Code still contains `unimplemented!()` or `todo!()` — "
            "you must provide a complete working implementation."
        )
    return None


def validate_files(files: dict[str, str]) -> str | None:
    """Sanity check on the merged multi-file algorithm set.

    Operates on the file map AFTER the LLM's diff has been layered over
    the baseline — so we're validating the algorithm the build will see,
    not just the files the LLM emitted this turn.

    Rules:
    1. ``fn solve_challenge(`` must appear in at least one file. The
       entry point may live in any file (e.g. ``mod.rs`` for
       vehicle_routing, ``solver.rs`` for job_scheduling).
    2. The file owning the entry point must bring parent-scope types
       into scope, via ``use super::*;``, ``use super::{...};``, or a
       fully-qualified ``super::Type`` reference. Without this the
       build fails because ``Challenge`` / ``Solution`` aren't in scope.
    3. No file in the set may still contain ``unimplemented!()`` or
       ``todo!()`` — those mark stub code that can't produce solutions.

    Returns None if valid, or an error description.
    """
    if not files:
        return "No algorithm files — the merged file set is empty."

    entry_points = [p for p, body in files.items() if "fn solve_challenge(" in body]
    if not entry_points:
        return (
            "`fn solve_challenge(` not found in any file — the entry "
            "point must exist somewhere in the algorithm directory "
            "(e.g. mod.rs or solver.rs)."
        )

    entry = entry_points[0]
    body = files[entry]
    if not any(tok in body for tok in ("use super::*;", "use super::{", "super::")):
        return (
            f"`{entry}` defines `fn solve_challenge(` but has no `super::` "
            f"reference — parent types like Challenge / Solution won't be "
            f"in scope. Add `use super::*;`, `use super::{{...}};`, or use "
            f"fully-qualified `super::Type` paths."
        )

    for path, body in files.items():
        if "unimplemented!" in body or "todo!" in body:
            return (
                f"`{path}` still contains `unimplemented!()` or `todo!()` "
                "— provide a complete working implementation."
            )

    return None


# ── ChallengeFiles ─────────────────────────────────────────────────


class ChallengeFiles:
    """Encapsulates file I/O differences between CPU and GPU challenges."""

    def __init__(self, config: dict):
        self._config = config
        self.is_gpu = bool(config.get("is_gpu"))

    def parse_response(self, text: str) -> tuple[str, str]:
        if self.is_gpu:
            return parse_gpu_code(text)
        return parse_code(text), ""

    def write(self, code: str, kernel: str = "") -> None:
        write_algorithm(code, self._config)
        if self.is_gpu and kernel:
            write_kernel(kernel, self._config)

    def read(self) -> tuple[str, str]:
        code = read_algorithm(self._config)
        kernel = read_kernel(self._config) if self.is_gpu else ""
        return code, kernel

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
