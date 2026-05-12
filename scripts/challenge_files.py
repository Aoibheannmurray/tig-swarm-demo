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


def parse_code(text: str) -> str:
    return _strip_fences(text)


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
    """Basic sanity check on LLM-generated code.

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
