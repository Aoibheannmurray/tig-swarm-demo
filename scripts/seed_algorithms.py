#!/usr/bin/env python3
"""Copy the starting algorithms into src/ so a fresh clone compiles.

`src/<challenge>/algorithm/` is gitignored (the swarm overwrites it at
runtime), so `cargo check --features solver,<challenge>` fails with E0583 on a
fresh clone. This copies each challenge's starting code into
`src/<challenge>/algorithm/` for local development and CI:

  initial_algorithms/<ch>/stub/*      (mod.rs [+ kernels.cu], names preserved)
  initial_algorithms/<ch>.rs (+ .cu)  (pre-restructure fallback) -> mod.rs

Usage:
    python3 scripts/seed_algorithms.py              # seed every challenge
    python3 scripts/seed_algorithms.py knapsack ... # seed specific challenges

Existing algorithm files are left alone unless --force is given.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "initial_algorithms"
SRC = ROOT / "src"


def _source_files(challenge: str) -> dict[str, Path] | None:
    """The starting files for a challenge as {destination relpath: source}.

    Prefers the `stub/` directory (multi-file aware, filenames preserved),
    falling back to the pre-restructure flat file."""
    stub_dir = SEEDS / challenge / "stub"
    if (stub_dir / "mod.rs").is_file():
        return {
            str(p.relative_to(stub_dir)): p
            for p in sorted(stub_dir.rglob("*")) if p.is_file()
        }
    legacy_rs = SEEDS / f"{challenge}.rs"
    if legacy_rs.is_file():
        out = {"mod.rs": legacy_rs}
        legacy_cu = SEEDS / f"{challenge}.cu"
        if legacy_cu.is_file():
            out["kernels.cu"] = legacy_cu
        return out
    return None


def challenges() -> list[str]:
    names = {p.stem for p in SEEDS.glob("*.rs")}  # pre-restructure fallback
    for d in SEEDS.iterdir():
        if d.is_dir() and _source_files(d.name):
            names.add(d.name)
    return sorted(names)


def seed(challenge: str, force: bool) -> str:
    sources = _source_files(challenge)
    if sources is None:
        return f"skip  {challenge}: no starting code under initial_algorithms/{challenge}/"
    algo_dir = SRC / challenge / "algorithm"
    if not (SRC / challenge).is_dir():
        return f"skip  {challenge}: no src/{challenge}/ module"
    mod_rs = algo_dir / "mod.rs"
    if mod_rs.exists() and not force:
        return f"keep  {challenge}: src/{challenge}/algorithm/mod.rs exists (use --force to overwrite)"
    algo_dir.mkdir(parents=True, exist_ok=True)
    for rel, src_path in sources.items():
        dst = algo_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_path, dst)
    origin = sources["mod.rs"].relative_to(ROOT)
    return f"seed  {challenge}: {origin} -> src/{challenge}/algorithm/mod.rs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("challenges", nargs="*", help="challenges to seed (default: all)")
    parser.add_argument("--force", action="store_true", help="overwrite existing algorithm files")
    args = parser.parse_args()

    known = challenges()
    targets = args.challenges or known
    unknown = [c for c in targets if c not in known]
    if unknown:
        print(f"unknown challenge(s): {', '.join(unknown)} (known: {', '.join(known)})", file=sys.stderr)
        return 1
    for ch in targets:
        print(seed(ch, args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())
