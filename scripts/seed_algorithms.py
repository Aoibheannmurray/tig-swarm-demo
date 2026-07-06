#!/usr/bin/env python3
"""Copy the seed algorithms into src/ so a fresh clone compiles.

`src/<challenge>/algorithm/` is gitignored (the swarm overwrites it at
runtime), so `cargo check --features solver,<challenge>` fails with E0583 on a
fresh clone. This copies `initial_algorithms/<challenge>.rs` to
`src/<challenge>/algorithm/mod.rs` (and `<challenge>.cu` to `kernels.cu` for
GPU challenges) for local development and CI.

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


def challenges() -> list[str]:
    return sorted(p.stem for p in SEEDS.glob("*.rs"))


def seed(challenge: str, force: bool) -> str:
    seed_rs = SEEDS / f"{challenge}.rs"
    if not seed_rs.exists():
        return f"skip  {challenge}: no seed at {seed_rs.relative_to(ROOT)}"
    algo_dir = SRC / challenge / "algorithm"
    if not (SRC / challenge).is_dir():
        return f"skip  {challenge}: no src/{challenge}/ module"
    mod_rs = algo_dir / "mod.rs"
    if mod_rs.exists() and not force:
        return f"keep  {challenge}: src/{challenge}/algorithm/mod.rs exists (use --force to overwrite)"
    algo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_rs, mod_rs)
    seed_cu = SEEDS / f"{challenge}.cu"
    if seed_cu.exists():
        shutil.copyfile(seed_cu, algo_dir / "kernels.cu")
    return f"seed  {challenge}: initial_algorithms/{challenge}.rs -> src/{challenge}/algorithm/mod.rs"


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
