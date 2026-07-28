"""Failed-attempts archive, client side: retrospective parsing + the
distillation trigger matrix.

Standalone: `python scripts/test_failure_retrospective.py`.

Covers:
  - parse_failure_retrospective: well-formed output, RETROSPECTIVE_SKIP,
    LESSON: SKIP with a valid retrospective (and the reverse), garbage,
    multi-line accumulation, clamping.
  - parse_archive_lesson: labeled and bare-format fallbacks.
  - _should_distill_tacit: tacit_write x failed_attempts x provider x
    DRIVER_DISTILL_FOR_AGENTIC.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompts
import run_loop
from prompts import (
    parse_archive_lesson,
    parse_failure_retrospective,
    build_tacit_distillation_prompts,
)


WELL_FORMED = """\
LESSON: - LLM: greedy construction plateaus when the feasible region is narrow.
APPROACH_SUMMARY: Greedy density ordering with local 2-opt repair.
WHAT_WAS_TRIED: Sorted items by value/weight; then randomized tie-breaks;
then a 2-opt swap pass on the greedy result.
OBSERVED_OUTCOME: Score plateaued ~4% below the trajectory best across
three consecutive edits.
POSSIBLE_REASONS: The greedy start biases every repair pass into the same
local optimum; no diversification step.
"""


def test_parse_well_formed():
    retro = parse_failure_retrospective(WELL_FORMED)
    assert retro is not None
    assert retro["approach_summary"] == "Greedy density ordering with local 2-opt repair."
    # Multi-line values accumulate until the next label.
    assert "randomized tie-breaks" in retro["what_was_tried"]
    assert "2-opt swap pass" in retro["what_was_tried"]
    assert retro["observed_outcome"].startswith("Score plateaued")
    assert "local optimum" in retro["possible_reasons"]
    assert retro["lesson"] == (
        "- LLM: greedy construction plateaus when the feasible region is narrow."
    )
    assert parse_archive_lesson(WELL_FORMED) == retro["lesson"]
    print("PASS test_parse_well_formed")


def test_retrospective_skip():
    assert parse_failure_retrospective("RETROSPECTIVE_SKIP") is None
    # Lesson survives a retrospective skip (the two skips are independent).
    out = "LESSON: - LLM: a lesson without material for a retrospective.\nRETROSPECTIVE_SKIP"
    assert parse_failure_retrospective(out) is None
    assert parse_archive_lesson(out) == "- LLM: a lesson without material for a retrospective."
    print("PASS test_retrospective_skip")


def test_lesson_skip_keeps_retrospective():
    out = WELL_FORMED.replace(
        "LESSON: - LLM: greedy construction plateaus when the feasible region is narrow.",
        "LESSON: SKIP",
    )
    retro = parse_failure_retrospective(out)
    assert retro is not None and retro["lesson"] is None
    assert retro["approach_summary"], retro
    assert parse_archive_lesson(out) is None
    print("PASS test_lesson_skip_keeps_retrospective")


def test_garbage_and_empty():
    assert parse_failure_retrospective("") is None
    assert parse_failure_retrospective(None) is None
    assert parse_failure_retrospective("I could not follow the format, sorry!") is None
    # Bare-format fallback: a model that ignored the labels entirely.
    assert parse_archive_lesson("- LLM: bare-format lesson line.") == (
        "- LLM: bare-format lesson line."
    )
    assert parse_archive_lesson("SKIP") is None
    print("PASS test_garbage_and_empty")


def test_clamping():
    long_text = "x" * 10_000
    out = f"APPROACH_SUMMARY: {long_text}\nWHAT_WAS_TRIED: y"
    retro = parse_failure_retrospective(out)
    assert retro is not None
    assert len(retro["approach_summary"]) == prompts._RETRO_FIELD_MAX
    assert retro["what_was_tried"] == "y"
    # Headers never emitted default to empty strings.
    assert retro["observed_outcome"] == "" and retro["possible_reasons"] == ""
    print("PASS test_clamping")


def test_prompt_variants():
    state = {"prior_hypotheses": [], "my_runs_since_improvement": 3}
    sys_plain, user_plain = build_tacit_distillation_prompts(state, {}, "", "")
    sys_arch, user_arch = build_tacit_distillation_prompts(
        state, {}, "", "", include_retrospective=True,
    )
    assert "APPROACH_SUMMARY" not in sys_plain
    assert "APPROACH_SUMMARY" in sys_arch
    assert "RETROSPECTIVE_SKIP" in user_arch
    assert "RETROSPECTIVE_SKIP" not in user_plain
    print("PASS test_prompt_variants")


def _distill_state(limit=4):
    return {"my_runs_since_improvement": limit - 1}


def test_should_distill_matrix():
    base_cfg = {"stagnation_limit": 4}
    should = run_loop._should_distill_tacit

    # Baseline: both channels on their defaults (tacit on, archive off).
    assert should(_distill_state(), dict(base_cfg), False, "anthropic")

    # tacit_write off + archive off: nothing to produce -> no fire.
    cfg = dict(base_cfg, tacit_write=False)
    assert not should(_distill_state(), cfg, False, "anthropic")

    # tacit_write off + archive ON: the retrospective still fires.
    cfg = dict(base_cfg, tacit_write=False, failed_attempts_archive=1)
    assert should(_distill_state(), cfg, False, "anthropic")

    # ...unless the contributor opted this agent out of the archive too.
    cfg = dict(base_cfg, tacit_write=False, failed_attempts_archive=1,
               failed_attempts_write="false")
    assert not should(_distill_state(), cfg, False, "anthropic")

    # Agentic providers stay in-band while DRIVER_DISTILL_FOR_AGENTIC=False,
    # even with the archive on (the in-band prompt block owns it).
    cfg = dict(base_cfg, failed_attempts_archive=1)
    assert prompts.DRIVER_DISTILL_FOR_AGENTIC is False
    assert not should(_distill_state(), cfg, False, "claude-code-agentic")
    old = prompts.DRIVER_DISTILL_FOR_AGENTIC
    try:
        prompts.DRIVER_DISTILL_FOR_AGENTIC = True
        run_loop._prompts.DRIVER_DISTILL_FOR_AGENTIC = True
        assert should(_distill_state(), cfg, False, "claude-code-agentic")
    finally:
        prompts.DRIVER_DISTILL_FOR_AGENTIC = old
        run_loop._prompts.DRIVER_DISTILL_FOR_AGENTIC = old

    # Improvement or a short limit suppresses it regardless of toggles.
    assert not should(_distill_state(), dict(base_cfg), True, "anthropic")
    cfg = dict(stagnation_limit=2, failed_attempts_archive=1)
    assert not should({"my_runs_since_improvement": 1}, cfg, False, "anthropic")
    print("PASS test_should_distill_matrix")


def test_agentic_prompt_block_routing():
    """With the archive ON, the in-band prompt must ask for the
    retrospective JSON (including the `lesson` key) and must NOT instruct
    appending to tacit_knowledge_personal.md — the DB is the source of
    truth. Archive OFF keeps the legacy file-append block."""
    from prompts import build_agentic_user_prompt
    state = {"my_runs_since_improvement": 3}  # = stagnation_limit - 1

    cfg_off = {"stagnation_limit": 4, "challenge": "knapsack"}
    p = build_agentic_user_prompt(state, cfg_off)
    assert "## Tacit-knowledge contribution" in p
    assert "failure_retrospective.json" not in p

    cfg_on = dict(cfg_off, failed_attempts_archive=1)
    p = build_agentic_user_prompt(state, cfg_on)
    assert "## Tacit-knowledge contribution" not in p, (
        "archive on must suppress the tacit-file append instruction"
    )
    assert "failure_retrospective.json" in p
    assert '"lesson"' in p, "the lesson must ride the retrospective JSON"

    # Off-trigger iterations get neither block regardless of toggles.
    p = build_agentic_user_prompt({"my_runs_since_improvement": 1}, cfg_on)
    assert "failure_retrospective.json" not in p
    assert "## Tacit-knowledge contribution" not in p
    print("PASS test_agentic_prompt_block_routing")


def test_enabled_helpers_agree():
    """run_loop.failed_attempts_enabled and prompts._failed_attempts_enabled
    must agree — one gates the driver path, the other the in-band block."""
    cases = [
        {},
        {"failed_attempts_archive": 1},
        {"failed_attempts_archive": 0},
        {"failed_attempts_archive": 1, "failed_attempts_write": False},
        {"failed_attempts_archive": 1, "failed_attempts_write": "off"},
        {"failed_attempts_archive": 1, "failed_attempts_write": "true"},
    ]
    for cfg in cases:
        assert run_loop.failed_attempts_enabled(cfg) == \
            prompts._failed_attempts_enabled(cfg), cfg
    assert run_loop.failed_attempts_enabled({"failed_attempts_archive": 1})
    assert not run_loop.failed_attempts_enabled({})
    print("PASS test_enabled_helpers_agree")


if __name__ == "__main__":
    test_parse_well_formed()
    test_retrospective_skip()
    test_lesson_skip_keeps_retrospective()
    test_garbage_and_empty()
    test_clamping()
    test_prompt_variants()
    test_should_distill_matrix()
    test_agentic_prompt_block_routing()
    test_enabled_helpers_agree()
    print("\nAll failure-retrospective tests passed.")
