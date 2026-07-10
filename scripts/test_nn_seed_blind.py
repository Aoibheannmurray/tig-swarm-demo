#!/usr/bin/env python3
"""bench_blind_nn: the baked neuralnet_optimizer seed-blinding (anti-cheat).

This is security-critical: the blind is what stops an agent's optimizer_init_state
hook from receiving the raw challenge.seed (which regenerates the instance's true
targets). It runs at image-build time, so these tests exercise the patch logic
directly against a fixture mirroring tig-challenges/src/neuralnet_optimizer/mod.rs.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_blind_nn  # noqa: E402

_REL = "tig-challenges/src/neuralnet_optimizer/mod.rs"

# Minimal fixture containing the exact anchor the real monorepo source has.
_FIXTURE = """\
pub fn training_loop(challenge: &Challenge) -> anyhow::Result<()> {
    let mut optimizer_state = optimizer_init_state(
        challenge.seed.clone(),
        &challenge,
    )?;
    Ok(())
}
"""


def _write_fixture(root: Path, body: str = _FIXTURE) -> Path:
    p = root / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_blind_replaces_raw_seed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p = _write_fixture(root)
        changed = bench_blind_nn.blind(str(root))
        out = p.read_text()
        assert changed is True
        # The hook is no longer handed the raw seed…
        assert "optimizer_init_state(\n        challenge.seed.clone()," not in out
        # …it gets the scrambled derivative instead.
        assert "optimizer_init_state(\n        optimizer_seed," in out
        assert "StdRng::from_seed(challenge.seed)" in out
    print("PASS test_blind_replaces_raw_seed")


def test_blind_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p = _write_fixture(root)
        assert bench_blind_nn.blind(str(root)) is True
        after_first = p.read_text()
        # Re-running (e.g. a rebuild layer) must not double-patch.
        assert bench_blind_nn.blind(str(root)) is False
        assert p.read_text() == after_first
        assert after_first.count("let optimizer_seed =") == 1
    print("PASS test_blind_is_idempotent")


def test_missing_anchor_fails_loud():
    # A silent skip here would ship a seed-leaking image — must raise instead.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_fixture(root, "pub fn training_loop() { /* no anchor */ }\n")
        try:
            bench_blind_nn.blind(str(root))
        except AssertionError as exc:
            assert "anchor not found" in str(exc)
        else:
            raise AssertionError("expected missing anchor to raise")
    print("PASS test_missing_anchor_fails_loud")


def test_main_noop_for_other_challenges():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p = _write_fixture(root)
        prev = os.environ.get("CHALLENGE")
        os.environ["CHALLENGE"] = "knapsack"
        try:
            rc = bench_blind_nn.main(["bench_blind_nn.py", str(root)])
        finally:
            if prev is None:
                os.environ.pop("CHALLENGE", None)
            else:
                os.environ["CHALLENGE"] = prev
        assert rc == 0
        # Untouched for non-neuralnet challenges.
        assert p.read_text() == _FIXTURE
    print("PASS test_main_noop_for_other_challenges")


if __name__ == "__main__":
    test_blind_replaces_raw_seed()
    test_blind_is_idempotent()
    test_missing_anchor_fails_loud()
    test_main_noop_for_other_challenges()
    print("\nAll nn seed-blind tests passed.")
