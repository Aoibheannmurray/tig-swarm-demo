"""Deterministic code-bloat reduction for multi-file algorithms (Tier 0).

The LLM-free half of the cleaner (docs/cleaner-agent-plan.md). Operates on an
algorithm files-map ({relpath: content}, entry usually `mod.rs`) and applies
only mechanical, provably-safe transformations:

  1. Unreachable-file removal — drop `.rs` files whose module is never
     declared by any file reachable from the entry (mod-declaration BFS).
  2. Identical-file merge — collapse `.rs` files whose bodies are identical
     after ignoring leading `//` comment headers, rewiring `mod` declarations
     and `name::` path references to the kept module.

Both transformations came straight from a real incident (2026-07-03): a
2.5MB six-file best contained one file that was byte-identical to another
AND never dispatched, plus three 99%-identical forks. The caller still
benchmarks the result before accepting it (the run_loop cleaner gate), so
this module optimizes for "never change behavior", not for maximum shrink.

No LLM, no filesystem, no network — pure functions, so the self-running
test (test_cleaner_prepass.py) covers it hermetically.
"""

from __future__ import annotations

import re

# `mod foo;` / `pub mod foo;` declarations — the semicolon form that pulls in
# a sibling file. Deliberately does NOT match inline `mod foo { ... }`.
_MOD_DECL_RE = re.compile(
    r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)


def _declared_mods(code: str) -> set[str]:
    return set(_MOD_DECL_RE.findall(code))


def _stem(name: str) -> str:
    return name.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _dir(name: str) -> str:
    """Directory part of a relative POSIX path ('' for top-level files)."""
    return name.rsplit("/", 1)[0] if "/" in name else ""


def _module_dir(name: str) -> str:
    """Directory a `mod x;` declared in `name` pulls its file from: `d/` for
    `d/mod.rs` (and lib.rs/main.rs), `d/f/` for a non-mod file `d/f.rs`."""
    d = _dir(name)
    stem = _stem(name)
    if stem in ("mod", "lib", "main"):
        return d
    return f"{d}/{stem}" if d else stem


def _mod_candidates(declaring: str, mod_name: str) -> list[str]:
    """Relative paths a `mod <name>;` in `declaring` can resolve to.

    Keyed on the full relative path (not the bare basename) so same-named
    files in different subdirectories can't be confused for each other.
    Rust resolves the declaration under the declaring file's module dir
    (`d/f.rs` -> `d/f/<name>.rs`); we also accept the declaring file's own
    directory (`d/<name>.rs`) — the layout flattened algorithm maps use.
    Over-matching is safe here: an extra candidate can only *keep* a file
    during unreachable-removal, never drop one."""
    dirs = [_module_dir(declaring)]
    sibling_dir = _dir(declaring)
    if sibling_dir != dirs[0]:
        dirs.append(sibling_dir)
    candidates = []
    for d in dirs:
        prefix = f"{d}/" if d else ""
        candidates += [
            f"{prefix}{mod_name}.rs",
            f"{prefix}{mod_name}/mod.rs",
        ]
    return candidates


def _body_lines(code: str) -> list[str]:
    """Lines with the leading `//` comment header (and blank lines) stripped —
    the two byte-identical files in the wild differed only in a generated
    `// FLAT: track_tXX.rs …` first line."""
    lines = code.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("//")):
        i += 1
    return lines[i:]


def remove_unreachable_files(
    file_map: dict[str, str], entry: str,
) -> tuple[dict[str, str], list[str]]:
    """Drop `.rs` files not reachable from `entry` via `mod x;` declarations.

    Non-`.rs` files (e.g. `kernels.cu` — referenced via include_str!/nvrtc,
    not `mod`) are always kept.
    """
    rs = {n for n in file_map if n.endswith(".rs")}
    reachable: set[str] = set()
    frontier = [entry] if entry in file_map else []
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for m in _declared_mods(file_map[name]):
            for child in _mod_candidates(name, m):
                if child in rs and child not in reachable:
                    frontier.append(child)
    dropped = sorted(rs - reachable)
    if not dropped:
        return dict(file_map), []
    kept = {n: c for n, c in file_map.items() if n not in dropped}
    return kept, [f"removed unreachable file {n}" for n in dropped]


def merge_identical_files(
    file_map: dict[str, str], entry: str,
) -> tuple[dict[str, str], list[str]]:
    """Collapse non-entry `.rs` files with identical bodies (modulo leading
    comment headers) into one module, rewiring all references.

    Grouping and rewiring are keyed on the full relative path: only files in
    the SAME directory are merge candidates (identical bodies in different
    subdirs are different modules with different `mod`/path wiring), and only
    files that can actually reference the dropped module — its directory
    siblings plus the file that declares that directory — get their
    `mod dup;` declarations and `dup::` paths rewired. A same-named module
    in another subdir is never touched."""
    names = sorted(n for n in file_map if n.endswith(".rs") and n != entry)
    bodies = {n: _body_lines(file_map[n]) for n in names}
    # Group by (directory, identical body); keep the alphabetically-first.
    groups: dict[tuple, list[str]] = {}
    for n in names:
        groups.setdefault((_dir(n), tuple(bodies[n])), []).append(n)

    out = dict(file_map)
    actions: list[str] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        keep, drops = members[0], members[1:]
        keep_stem = _stem(keep)
        for d in drops:
            d_stem = _stem(d)
            d_dir = _dir(d)
            del out[d]
            actions.append(f"merged duplicate file {d} into {keep}")
            for name in list(out):
                if not name.endswith(".rs"):
                    continue
                # Same-dir siblings + the declarer of this dir's modules.
                if _dir(name) != d_dir and _module_dir(name) != d_dir:
                    continue
                code = out[name]
                # Drop the `mod dup;` declaration line…
                code = re.sub(
                    rf"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+{d_stem}\s*;\s*\n?",
                    "", code, flags=re.MULTILINE,
                )
                # …and point `dup::` paths at the kept module.
                code = re.sub(rf"\b{d_stem}\s*::", f"{keep_stem}::", code)
                out[name] = code
    return out, actions


def run_prepass(
    file_map: dict[str, str], entry: str,
) -> tuple[dict[str, str], list[str]]:
    """Full Tier-0 pass: merge duplicates, then drop unreachable files.

    Order matters: merging first turns "two identical files, one referenced"
    into one referenced file; a dropped duplicate whose `mod` line was
    removed everywhere is then also unreachable-safe. Returns
    (new_map, action log); an empty action log means nothing to do.
    """
    out, actions = merge_identical_files(file_map, entry)
    out, more = remove_unreachable_files(out, entry)
    return out, actions + more


def total_chars(file_map: dict[str, str]) -> int:
    return sum(len(v) for v in file_map.values())
