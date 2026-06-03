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


# ── Read / write ───────────────────────────────────────────────────


# LLM output and user-authored files are always written/read as UTF-8 so a
# model emitting non-ASCII punctuation (em-dash, →, non-breaking hyphen) can't
# crash the run on a host whose default encoding is cp125x (Windows). Reads use
# errors="replace" so a file left half-written by a prior crash still loads.
def read_optional(path: Path | None) -> str:
    if path and path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def write_algorithm(code: str, config: dict) -> None:
    p = algo_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code, encoding="utf-8")


def write_kernel(code: str, config: dict) -> None:
    p = kernel_path(config)
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")


def read_algorithm(config: dict) -> str:
    p = algo_path(config)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def read_kernel(config: dict) -> str:
    p = kernel_path(config)
    if p and p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return ""


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


def ensure_super_import(code: str) -> str:
    """Re-insert the required `use super::*;` anchor if it's missing.

    The swarm puts each algorithm at `src/<challenge>/algorithm/mod.rs`, a
    submodule of the challenge module, so `use super::*;` (equivalently
    `use crate::<challenge>::*;`) pulls the Challenge/Solution types into
    scope. Agents — especially the tooled agentic backend — sometimes rewrite
    the import block and drop the literal anchor (or spell it the long way),
    which previously discarded an otherwise-valid candidate. Inserting it is
    safe: if the parent glob is already imported some other way the worst case
    is an unused-import warning, never an error.
    """
    if not code or "use super::*;" in code:
        return code
    lines = code.splitlines(keepends=True)
    # Insert before the first top-level `use` (which sits after any leading
    # comments and `#![...]` inner attributes), else at the very top.
    for i, line in enumerate(lines):
        if line.lstrip().startswith("use "):
            lines.insert(i, "use super::*;\n")
            return "".join(lines)
    return "use super::*;\n" + code


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
    return ensure_super_import(text.strip())


def parse_gpu_code(text: str) -> tuple[str, str]:
    """Extract Rust + CUDA code from a GPU two-file LLM response.

    Returns (rust_code, cuda_code). If no separator is found,
    returns the whole text as rust_code and empty cuda_code.
    """
    text = _strip_fences(text)
    m = _KERNEL_SEPARATOR_RE.search(text)
    if m is None:
        return ensure_super_import(text.strip()), ""
    rust = text[: m.start()].strip()
    cuda = text[m.end():].strip()
    return ensure_super_import(_strip_fences(rust)), _strip_fences(cuda)


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
    if "use super::*;" not in code:
        return "`use super::*;` is missing — it must remain as the first import."
    challenge = (config or {}).get("challenge")
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
