"""Code-similarity diversity for the seed pool.

The seed pool exists to hand exploiters *simple, structurally-distinct* launch
points covering different regions of the search space — NOT a hall of fame of the
best algorithms. So admission is driven by:

  - **Simplicity**: only harvest algorithms under a LOC ceiling (a seed should
    be simple by nature; complex high-scoring descendants stay in trajectories).
  - **Novelty**: admit a candidate only if it is sufficiently *dissimilar* (low
    code similarity) from every existing seed — a genuinely new region.
  - **Bounded + sticky**: cap the pool at K. When full, evict the *most
    redundant* seed (the one most similar to a neighbor), NEVER by score. A
    higher-scoring algorithm never displaces a seed on score alone.

Similarity is a cheap identifier/token Jaccard — no AST, no parsing. Pure
functions; unit-tested in test_seed_diversity.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Word-ish tokens: identifiers, numbers, and operators. Good enough to tell two
# algorithms apart by their vocabulary without parsing Rust.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+\.?\d*|[^\s\w]")
_LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(code: str) -> str:
    code = _BLOCK_COMMENT_RE.sub(" ", code)
    code = _LINE_COMMENT_RE.sub("", code)
    return code


def loc(code: str) -> int:
    """Non-blank, non-comment source lines — the simplicity proxy."""
    stripped = _strip_comments(code or "")
    return sum(1 for ln in stripped.splitlines() if ln.strip())


def _shingles(code: str, n: int = 3) -> set:
    """Set of n-gram token shingles. Shingling (vs a bare token set) captures
    local structure, so two algorithms that share a vocabulary but use it
    differently still read as dissimilar."""
    toks = _TOKEN_RE.findall(_strip_comments(code or ""))
    if len(toks) < n:
        return set(toks)
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of two algorithms' token shingles, in [0, 1]."""
    sa, sb = _shingles(a), _shingles(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def max_similarity(code: str, others: list[str]) -> float:
    """Highest similarity between `code` and any algorithm in `others` (0 if
    `others` is empty)."""
    return max((similarity(code, o) for o in others), default=0.0)


def most_redundant_index(codes: list[str]) -> int | None:
    """Index of the seed that contributes least to diversity — the one whose
    nearest neighbor (within the set) is the most similar. None for <2 seeds."""
    if len(codes) < 2:
        return None
    best_idx, best_nn = None, -1.0
    for i, c in enumerate(codes):
        nn = max(
            (similarity(c, other) for j, other in enumerate(codes) if j != i),
            default=0.0,
        )
        if nn > best_nn:
            best_nn, best_idx = nn, i
    return best_idx


@dataclass
class Decision:
    admit: bool
    evict_index: int | None  # index into the existing-seed list to remove first
    reason: str  # "admit" | "admit_evict" | "too_complex" | "too_similar"


def decide_admission(
    candidate: str,
    seed_codes: list[str],
    *,
    pool_size: int,
    similarity_threshold: float,
    max_loc: int,
) -> Decision:
    """Whether (and how) to admit a candidate algorithm into the seed pool.

    - Reject if it exceeds the LOC ceiling (not simple enough).
    - Reject if it's too similar to an existing seed (not novel).
    - Otherwise admit; if the pool is full, evict the most-redundant seed to
      make room (never by score).
    """
    if loc(candidate) > max_loc:
        return Decision(False, None, "too_complex")
    if seed_codes and max_similarity(candidate, seed_codes) >= similarity_threshold:
        return Decision(False, None, "too_similar")
    if len(seed_codes) < pool_size:
        return Decision(True, None, "admit")
    return Decision(True, most_redundant_index(seed_codes), "admit_evict")
