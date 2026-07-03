"""Guard: swarm algorithms use the mainnet format, verbatim.

The swarm's algorithm format IS the TIG mainnet format — one file compiles
unchanged in both the swarm crate (src/lib.rs's `extern crate self as
tig_challenges`) and the TIG-docker slot (baked tig-bench images), so no
import swapping exists anywhere. This test keeps it that way:

  1. No checked-in seed/stub uses the legacy `use super::*;` anchor.
  2. Every seed/stub carries `use tig_challenges::<its challenge>::*;`
     and `pub fn help(`.
  3. `ensure_challenge_import` migrates legacy code and inserts the anchor.
  4. `validate_code` teaches the mainnet anchor (accepting legacy code that
     adoption may still hand out, until agents migrate it).

Self-running (repo convention): `python scripts/test_algorithm_format.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from challenge_files import ensure_challenge_import, validate_code  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "initial_algorithms"

# neuralnet is the optimizer-hook challenge: its stub/seed keep hook-specific
# imports (Phase 6 of the parity plan) — anchor rules still apply.
def _challenge_of(path: Path) -> str:
    rel = path.relative_to(SEEDS)
    return rel.parts[0].removesuffix(".rs")


def test_no_legacy_anchor_in_seeds():
    offenders = [
        str(p.relative_to(ROOT))
        for p in sorted(SEEDS.rglob("*.rs"))
        if "use super::*;" in p.read_text()
    ]
    assert not offenders, f"legacy `use super::*;` in: {offenders}"
    print("PASS test_no_legacy_anchor_in_seeds")


def test_seeds_carry_mainnet_anchor_and_help():
    missing_anchor, missing_help = [], []
    for p in sorted(SEEDS.rglob("*.rs")):
        code, ch = p.read_text(), _challenge_of(p)
        rel = str(p.relative_to(ROOT))
        if f"use tig_challenges::{ch}::" not in code:
            missing_anchor.append(rel)
        if "pub fn help(" not in code:
            missing_help.append(rel)
    assert not missing_anchor, f"missing mainnet anchor: {missing_anchor}"
    assert not missing_help, f"missing pub fn help(): {missing_help}"
    print("PASS test_seeds_carry_mainnet_anchor_and_help")


def test_ensure_challenge_import_behaviour():
    anchor = "use tig_challenges::knapsack::*;"
    # migrates legacy in place
    got = ensure_challenge_import("use super::*;\nfn x() {}\n", "knapsack")
    assert got.startswith(anchor) and "use super::*;" not in got, got
    # inserts before the first `use` when absent
    got = ensure_challenge_import("// hdr\nuse anyhow::Result;\n", "knapsack")
    assert got.splitlines()[1] == anchor, got
    # idempotent
    assert ensure_challenge_import(got, "knapsack") == got
    print("PASS test_ensure_challenge_import_behaviour")


def test_validator_teaches_mainnet_anchor():
    cfg = {"challenge": "knapsack"}
    ok = ("use tig_challenges::knapsack::*;\n"
          "pub fn solve_challenge() {}\n")
    assert validate_code(ok, cfg) is None, validate_code(ok, cfg)
    # legacy accepted (pre-parity adopted code) …
    legacy = "use super::*;\npub fn solve_challenge() {}\n"
    assert validate_code(legacy, cfg) is None
    # … but no anchor at all is rejected, teaching the mainnet form.
    err = validate_code("pub fn solve_challenge() {}\n", cfg)
    assert err and "use tig_challenges::knapsack::*;" in err, err
    print("PASS test_validator_teaches_mainnet_anchor")


if __name__ == "__main__":
    test_no_legacy_anchor_in_seeds()
    test_seeds_carry_mainnet_anchor_and_help()
    test_ensure_challenge_import_behaviour()
    test_validator_teaches_mainnet_anchor()
    print("\nAll algorithm-format guard tests passed.")
