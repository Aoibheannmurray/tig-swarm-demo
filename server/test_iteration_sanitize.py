"""IterationCreate sanitizes agent-controlled fields instead of 422-ing.

A publish that violates a label/score constraint must be accepted (sanitized)
rather than rejected — a 422 drops the iteration's score, hypothesis and token
accounting. Runs standalone: `python server/test_iteration_sanitize.py`.
"""

import os
import sys
import tempfile

# Hermetic: point DATA_DIR at a temp dir before importing server modules.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(__file__))

import models  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def _make(**kw):
    base = dict(agent_id="a", title="t", score=1.0)
    base.update(kw)
    return models.IterationCreate(**base)


def test_overlong_title_is_clamped_not_rejected():
    m = _make(title="x" * 500)
    assert len(m.title) == models.MAX_LABEL_LEN, len(m.title)
    print("PASS test_overlong_title_is_clamped_not_rejected")


def test_none_or_missing_score_becomes_zero():
    assert _make(score=None).score == 0.0
    assert _make(score="not a number").score == 0.0
    assert _make(score="42.5").score == 42.5   # numeric strings still parse
    # A legit negative float is preserved (VRP scores run negative).
    assert _make(score=-121021.0).score == -121021.0
    print("PASS test_none_or_missing_score_becomes_zero")


def test_none_feasible_becomes_false():
    assert _make(feasible=None).feasible is False
    assert _make(feasible=True).feasible is True
    print("PASS test_none_feasible_becomes_false")


def test_huge_notes_and_none_label_fields_coerced():
    m = _make(notes="x" * 70000, description=None, title=None)
    assert len(m.notes) == models.MAX_NOTES_LEN
    assert m.description == ""
    assert m.title == ""
    print("PASS test_huge_notes_and_none_label_fields_coerced")


def test_oversized_solution_data_dropped_not_rejected():
    # solution_data is optional viz; if it overflows the 2MB cap the publish
    # must still succeed (score kept) with solution_data dropped to None.
    big = {"inst": {"blob": "x" * (models.MAX_CODE_LEN + 10)}}
    m = _make(solution_data=big)
    assert m.solution_data is None
    # A normal-size payload is preserved untouched.
    small = {"inst": {"routes": [1, 2, 3]}}
    assert _make(solution_data=small).solution_data == small
    print("PASS test_oversized_solution_data_dropped_not_rejected")


def test_invalid_challenge_still_rejected():
    # An unknown challenge is a genuine error we can't sanitize (no intent to
    # infer) — it must still surface, not silently pass.
    try:
        _make(challenge="not_a_challenge")
    except ValidationError:
        print("PASS test_invalid_challenge_still_rejected")
        return
    raise AssertionError("expected an invalid challenge to raise")


def _main():
    test_overlong_title_is_clamped_not_rejected()
    test_none_or_missing_score_becomes_zero()
    test_none_feasible_becomes_false()
    test_huge_notes_and_none_label_fields_coerced()
    test_oversized_solution_data_dropped_not_rejected()
    test_invalid_challenge_still_rejected()
    print("\nAll iteration-sanitize tests passed.")


if __name__ == "__main__":
    _main()
