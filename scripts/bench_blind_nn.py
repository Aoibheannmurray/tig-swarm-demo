#!/usr/bin/env python3
"""Baked anti-cheat for neuralnet_optimizer: blind the seed the harness passes to
the agent's ``optimizer_init_state`` hook.

Run at IMAGE-BUILD time from Dockerfile.bench, BEFORE the warm cargo build, so
``tig-challenges`` compiles and caches already-blinded (and the re-added ``/app``
source is blinded too — it can't be un-blinded even from the public image). It's a
no-op for every challenge except ``neuralnet_optimizer``.

Why blind at all: the optimizer legitimately needs a *deterministic, per-instance*
randomness source (to initialise weights reproducibly — the run must be verifiable,
so no OS/clock entropy). The only per-instance deterministic entropy is the nonce
seed — but that same ``challenge.seed`` also regenerates the instance's *true
targets*, so an agent handed the raw seed could reconstruct the dataset and emit the
answers directly. We hand the hook a one-way ``StdRng``-derived seed instead: still
deterministic and per-instance, but not invertible to the real seed.

Usage:  python3 bench_blind_nn.py [ROOT]     (ROOT defaults to ".", cwd in the image
is /app). Reads $CHALLENGE to decide whether to act.
"""
import os
import sys

# The un-blinded call site in the pinned monorepo (two lines: the `let` and the
# first arg). Must match tig-challenges/src/neuralnet_optimizer/mod.rs exactly.
ANCHOR = (
    "    let mut optimizer_state = optimizer_init_state(\n"
    "        challenge.seed.clone(),"
)
# Derive a scrambled seed from challenge.seed and pass THAT to the hook instead.
BLINDED = (
    "    let optimizer_seed = { use rand::{RngCore, SeedableRng}; "
    "let mut sd = rand::rngs::StdRng::from_seed(challenge.seed); "
    "let mut b = [0u8; 32]; sd.fill_bytes(&mut b); b };\n"
    "    let mut optimizer_state = optimizer_init_state(\n"
    "        optimizer_seed,"
)
# First line of the blinded form — its presence means we've already patched.
_BLINDED_MARK = "let optimizer_seed = { use rand::{RngCore, SeedableRng};"

_REL_PATH = "tig-challenges/src/neuralnet_optimizer/mod.rs"


def blind(root: str = ".") -> bool:
    """Patch the neuralnet harness source under ``root`` in place.

    Returns True if it applied the blind, False if it was already blinded
    (idempotent — safe to re-run). Raises AssertionError if the anchor is missing:
    failing loud here is deliberate, because silently skipping would ship an
    un-blinded, seed-leaking image.
    """
    path = os.path.join(root, _REL_PATH)
    text = open(path).read()
    if _BLINDED_MARK in text:
        return False
    assert ANCHOR in text, f"blinding anchor not found in {path}"
    open(path, "w").write(text.replace(ANCHOR, BLINDED, 1))
    return True


def main(argv) -> int:
    challenge = os.environ.get("CHALLENGE", "")
    root = argv[1] if len(argv) > 1 else "."
    if challenge != "neuralnet_optimizer":
        print(f"[build] no seed-blind needed for {challenge!r}")
        return 0
    changed = blind(root)
    print("[build] blinded optimizer seed" if changed
          else "[build] optimizer seed already blinded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
