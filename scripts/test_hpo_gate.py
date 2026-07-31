"""Self-running tests for the HPO gate band logic (_hpo_gate_open).

Run directly: `python scripts/test_hpo_gate.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_loop import _hpo_gate_open  # noqa: E402


def _bench(score, feasible=True):
    return {"score": score, "feasible": feasible}


CFG_MAX = {"hpo_min_improvements": 4, "hpo_first_tune_improvements": 4,
           "scoring_direction": "max"}
CFG_MIN = {"hpo_min_improvements": 4, "hpo_first_tune_improvements": 4,
           "scoring_direction": "min"}
# max: improvements rise → floor=100 (oldest), parent=130 (latest)
IMP_MAX = [100.0, 110.0, 120.0, 130.0]
# min: improvements fall → floor=130 (oldest), parent=100 (latest/best)
IMP_MIN = [130.0, 120.0, 110.0, 100.0]


def test_infeasible_or_missing_closed():
    assert not _hpo_gate_open(CFG_MAX, _bench(120, feasible=False), IMP_MAX, True)
    assert not _hpo_gate_open(CFG_MAX, _bench(None), IMP_MAX, True)
    print("PASS infeasible / missing score -> closed")


def test_immature_trajectory_closed():
    assert not _hpo_gate_open(CFG_MAX, _bench(120), [100.0, 110.0], True)
    print("PASS fewer than min_improvements -> closed")


def test_first_tune_waives_band():
    # has_tuned False: opens even when score is above the parent.
    assert _hpo_gate_open(CFG_MAX, _bench(999), IMP_MAX, False)
    print("PASS first tune opens (band waived)")


def test_first_tune_higher_bar():
    # The FIRST tune needs hpo_first_tune_improvements (default 10);
    # subsequent tunes need only hpo_min_improvements.
    cfg = {"hpo_min_improvements": 2, "hpo_first_tune_improvements": 4,
           "scoring_direction": "max"}
    assert not _hpo_gate_open(cfg, _bench(999), [100.0, 110.0, 120.0], False), \
        "first tune below first_tune_improvements should stay closed"
    assert _hpo_gate_open(cfg, _bench(999), IMP_MAX, False), \
        "first tune at first_tune_improvements should open"
    # After the first tune, 3 improvements >= min_improvements(2): band applies.
    assert _hpo_gate_open(cfg, _bench(115), [100.0, 110.0, 120.0], True), \
        "post-first-tune in-band candidate should open at min_improvements"
    # Defaults: min=4, first=10 — 4 improvements is mature for the band but
    # not enough for a first tune.
    cfg_default = {"scoring_direction": "max"}
    assert not _hpo_gate_open(cfg_default, _bench(999), IMP_MAX, False), \
        "default first tune should need 10 improvements"
    assert _hpo_gate_open(cfg_default, _bench(120), IMP_MAX, True), \
        "default post-first-tune band should apply at 4 improvements"
    print("PASS first tune uses the higher first_tune_improvements bar")


def test_max_band():
    g = lambda s: _hpo_gate_open(CFG_MAX, _bench(s), IMP_MAX, True)
    assert g(120) and g(110), "in-band near-miss should open"
    assert not g(130), "== parent should close"
    assert not g(140), "above parent (a win) should close"
    assert not g(100), "== floor should close"
    assert not g(90), "below floor (regressed) should close"
    print("PASS max-direction band (floor < score < parent)")


def test_min_band():
    g = lambda s: _hpo_gate_open(CFG_MIN, _bench(s), IMP_MIN, True)
    assert g(110), "in-band near-miss should open"
    assert not g(100), "== parent should close"
    assert not g(90), "better than parent (a win) should close"
    assert not g(130), "== floor should close"
    assert not g(140), "worse than floor (regressed) should close"
    print("PASS min-direction band (parent < score < floor)")


if __name__ == "__main__":
    test_infeasible_or_missing_closed()
    test_immature_trajectory_closed()
    test_first_tune_waives_band()
    test_first_tune_higher_bar()
    test_max_band()
    test_min_band()
    print("\nAll HPO gate tests passed.")
