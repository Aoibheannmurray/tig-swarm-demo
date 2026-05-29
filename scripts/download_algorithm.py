#!/usr/bin/env python3
"""Fetch a named algorithm from tig-foundation/tig-monorepo and stage it
under `initial_algorithms/` so the next swarm seeds with it on iteration 0.

CLI:
    python3 scripts/download_algorithm.py <challenge> <algorithm> [--force] [--ref BRANCH]

The upstream layout is `tig-algorithms/src/{challenge}/{algorithm}` on the
branch named `{challenge}/{algorithm}` (override with `--ref`). If the
fetched algorithm is a single `mod.rs` (+ optional `*.cu`) we drop it into
`initial_algorithms/{challenge}.rs` (+ `{challenge}.cu`); otherwise we
materialize the directory under `initial_algorithms/{challenge}/`.

Importable: `download_algorithm(challenge, algorithm, *, force, ref=None)`.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INITIAL_DIR = ROOT / "initial_algorithms"

_API_BASE = "https://api.github.com/repos/tig-foundation/tig-monorepo/contents"
_HTTP_TIMEOUT = 8


class DownloadError(RuntimeError):
    """Raised when the upstream fetch or staging cannot be completed."""


# ── HTTP ──────────────────────────────────────────────────────────────


def _api_get(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "tig-swarm-demo"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise DownloadError(f"not found: {url}") from None
        raise DownloadError(f"GitHub API error {e.code}: {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DownloadError(f"network error fetching {url}: {e}") from None


def _walk_contents(challenge: str, algorithm: str, branch: str) -> dict[str, str]:
    """Recursively walk the algorithm directory on GitHub. Returns a
    {relative_path: file_contents} map."""
    base = f"{_API_BASE}/tig-algorithms/src/{challenge}/{algorithm}"
    files: dict[str, str] = {}

    def _recurse(api_url: str, prefix: str) -> None:
        listing = _api_get(f"{api_url}?ref={branch}")
        if isinstance(listing, dict) and listing.get("type") == "file":
            # Single file at the top level (rare but valid).
            files[prefix or listing["name"]] = _decode_blob(listing)
            return
        if not isinstance(listing, list):
            raise DownloadError(f"unexpected GitHub response for {api_url}")
        for entry in listing:
            etype = entry.get("type")
            ename = entry.get("name", "")
            rel = f"{prefix}{ename}" if prefix else ename
            if etype == "file":
                blob = _api_get(f"{base}/{rel}?ref={branch}")
                files[rel] = _decode_blob(blob)
            elif etype == "dir":
                _recurse(f"{base}/{rel}", f"{rel}/")
            # Skip submodules / symlinks silently.

    _recurse(base, "")
    return files


def _decode_blob(blob: object) -> str:
    if not isinstance(blob, dict):
        raise DownloadError("unexpected blob shape from GitHub")
    encoding = blob.get("encoding")
    content = blob.get("content") or ""
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    if encoding is None and not content:
        return ""
    raise DownloadError(f"unsupported blob encoding: {encoding!r}")


# ── Cleaning ──────────────────────────────────────────────────────────


def _clean_rust(challenge: str, code: str) -> str:
    """Replace `use tig_challenges::{challenge}::...;` imports with the
    in-tree equivalents. Leaves everything else (seeded_hasher, HashMap,
    custom imports) untouched — those are rare and risky to rewrite."""
    pattern = re.compile(
        rf"^\s*use\s+tig_challenges::{re.escape(challenge)}::[^;]*;\s*$",
        re.MULTILINE,
    )
    return pattern.sub("use super::*;", code)


def _clean_cuda(challenge: str, code: str) -> str:
    pattern = re.compile(
        rf"^\s*use\s+tig_challenges::{re.escape(challenge)}::[^;]*;\s*$",
        re.MULTILINE,
    )
    return pattern.sub(f"use crate::{challenge}::*;", code)


def _clean(rel_path: str, challenge: str, content: str) -> str:
    if rel_path.endswith(".rs"):
        return _clean_rust(challenge, content)
    if rel_path.endswith(".cu"):
        return _clean_cuda(challenge, content)
    return content


# ── Staging ───────────────────────────────────────────────────────────


def _is_simple_layout(files: dict[str, str]) -> bool:
    """True iff the upstream bundle is just `mod.rs` (+ at most one `*.cu`)."""
    rs_files = [p for p in files if p.endswith(".rs")]
    cu_files = [p for p in files if p.endswith(".cu")]
    other = [p for p in files if not (p.endswith(".rs") or p.endswith(".cu"))]
    if other:
        return False
    if rs_files != ["mod.rs"]:
        return False
    return len(cu_files) <= 1


def _stage(challenge: str, files: dict[str, str], force: bool) -> Path:
    """Drop the cleaned files into initial_algorithms/. Returns the staged
    path (file or directory)."""
    INITIAL_DIR.mkdir(parents=True, exist_ok=True)
    legacy_rs = INITIAL_DIR / f"{challenge}.rs"
    legacy_cu = INITIAL_DIR / f"{challenge}.cu"
    bundle_dir = INITIAL_DIR / challenge

    cleaned = {p: _clean(p, challenge, c) for p, c in files.items()}

    if _is_simple_layout(cleaned):
        target = legacy_rs
        cu_target = legacy_cu if any(p.endswith(".cu") for p in cleaned) else None
        existing = [p for p in (target, cu_target, bundle_dir) if p and p.exists()]
        if existing and not force:
            raise DownloadError(
                f"refusing to overwrite {[str(p.relative_to(ROOT)) for p in existing]}; pass --force"
            )
        # Clean stale directory if we're switching from multi-file to single-file.
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        target.write_text(cleaned["mod.rs"], encoding="utf-8")
        if cu_target is not None:
            cu_path = next(p for p in cleaned if p.endswith(".cu"))
            cu_target.write_text(cleaned[cu_path], encoding="utf-8")
        elif legacy_cu.exists() and force:
            legacy_cu.unlink()
        return target

    # Multi-file directory layout.
    if "mod.rs" not in cleaned:
        raise DownloadError("upstream algorithm has no mod.rs; refusing to seed")
    existing = [p for p in (legacy_rs, legacy_cu, bundle_dir) if p.exists()]
    if existing and not force:
        raise DownloadError(
            f"refusing to overwrite {[str(p.relative_to(ROOT)) for p in existing]}; pass --force"
        )
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    if legacy_rs.exists():
        legacy_rs.unlink()
    if legacy_cu.exists():
        legacy_cu.unlink()
    bundle_dir.mkdir(parents=True)
    for rel, body in cleaned.items():
        out = bundle_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
    return bundle_dir


# ── Public API ────────────────────────────────────────────────────────


def fetch_algorithm(
    challenge: str, algorithm: str, *, ref: str | None = None,
) -> dict[str, str]:
    """Fetch + clean an algorithm from upstream. Returns
    ``{relative_path: cleaned_content}``. Does NOT write to disk — use
    ``download_algorithm`` for that.

    Separated from ``download_algorithm`` so callers that want to inspect
    or transmit the source without persisting it locally (e.g. the swarm's
    ``setup.py --seed-inactive-pool`` flow, which POSTs the code straight
    to the server) don't have the side effect of mutating
    ``initial_algorithms/``.
    """
    branch = ref or f"{challenge}/{algorithm}"
    print(f"  fetch_algorithm: {challenge}/{algorithm} (ref={branch})")
    files = _walk_contents(challenge, algorithm, branch)
    if not files:
        raise DownloadError(f"upstream returned no files for {challenge}/{algorithm}")
    return {p: _clean(p, challenge, c) for p, c in files.items()}


def download_algorithm(
    challenge: str, algorithm: str, *, force: bool, ref: str | None = None,
) -> Path:
    """Fetch + clean + stage. Returns the path written under initial_algorithms/."""
    cleaned = fetch_algorithm(challenge, algorithm, ref=ref)
    # _stage re-cleans internally; passing pre-cleaned content is idempotent
    # because the regexes only rewrite `use tig_challenges::...` lines that
    # the fetch pass has already replaced with `use super::*;`.
    staged = _stage(challenge, cleaned, force)
    rel = staged.relative_to(ROOT)
    print(f"    staged {len(cleaned)} file(s) -> {rel}")
    return staged


# ── CLI ───────────────────────────────────────────────────────────────


def _main() -> int:
    p = argparse.ArgumentParser(
        prog="download_algorithm",
        description=(
            "Fetch a named algorithm from tig-foundation/tig-monorepo and "
            "stage it under initial_algorithms/."
        ),
    )
    p.add_argument("challenge", help="e.g. satisfiability, hypergraph")
    p.add_argument("algorithm", help="e.g. sat_global_opt, sigma_freud_v7")
    p.add_argument("--force", action="store_true", help="Overwrite existing seed file/dir.")
    p.add_argument(
        "--ref",
        help="Git ref/branch to fetch from (default: {challenge}/{algorithm}).",
    )
    args = p.parse_args()
    try:
        download_algorithm(
            args.challenge, args.algorithm, force=args.force, ref=args.ref,
        )
    except DownloadError as e:
        msg = str(e)
        if "not found" in msg:
            branch = args.ref or f"{args.challenge}/{args.algorithm}"
            print(
                f"algorithm '{args.algorithm}' not found on branch '{branch}'",
                file=sys.stderr,
            )
        else:
            print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
