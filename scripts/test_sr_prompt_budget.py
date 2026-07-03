"""Self-running tests for the search/replace prompt file-subset budget
(_sr_prompt_file_subset) — the guard against multi-file algorithms outgrowing
the model's context window (a 2.5MB map once produced a >1M-token prompt that
every provider rejected, stalling the agent into the full-rewrite fallback).

Run directly: `python scripts/test_sr_prompt_budget.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_loop import _sr_prompt_file_subset  # noqa: E402


CFG = {"algorithm_path": "src/job_scheduling/algorithm/mod.rs",
       "sr_prompt_char_budget": 1000}

BIG = "x" * 600  # each track file busts the 1000-char budget on its own


def _map():
    return {
        "mod.rs": "m" * 100,
        "track_t44.rs": BIG,
        "track_t45.rs": BIG,
        "track_t46.rs": BIG,
        "track_t47.rs": BIG,
        "track_t48.rs": BIG,
    }


def test_small_map_untouched():
    fm = {"mod.rs": "tiny", "helper.rs": "also tiny"}
    subset, omitted = _sr_prompt_file_subset(fm, {"title": "t"}, CFG)
    assert subset == fm and omitted == []
    print("PASS map under budget is returned whole")


def test_hypothesis_shorthand_targets_file():
    hyp = {"title": "Increase T48 job_shop_iters", "description": "bump it"}
    subset, omitted = _sr_prompt_file_subset(_map(), hyp, CFG)
    assert "mod.rs" in subset, "entry file must always be shown"
    assert "track_t48.rs" in subset, "T48 shorthand should select track_t48.rs"
    assert "track_t44.rs" in omitted and len(omitted) == 4
    print("PASS 'T48' shorthand selects track_t48.rs; rest omitted")


def test_hypothesis_full_stem():
    hyp = {"title": "tune track_t45 params", "description": ""}
    subset, _ = _sr_prompt_file_subset(_map(), hyp, CFG)
    assert "track_t45.rs" in subset
    print("PASS full file stem in hypothesis selects the file")


def test_target_shown_even_over_budget():
    # One targeted file (600) + entry (100) exceeds budget 500, but the model
    # cannot edit a file it cannot see — the target must still be included.
    cfg = dict(CFG, sr_prompt_char_budget=500)
    hyp = {"title": "T47 tweak", "description": ""}
    subset, _ = _sr_prompt_file_subset(_map(), hyp, cfg)
    assert "track_t47.rs" in subset
    print("PASS targeted file shown even when it alone busts the budget")


def test_no_mention_fills_smallest_first():
    fm = {"mod.rs": "m" * 100, "small.rs": "s" * 200, "big.rs": "b" * 5000}
    subset, omitted = _sr_prompt_file_subset(
        fm, {"title": "general cleanup", "description": ""}, CFG)
    assert set(subset) == {"mod.rs", "small.rs"} and omitted == ["big.rs"]
    print("PASS no-target hypothesis fills remaining budget smallest-first")


if __name__ == "__main__":
    test_small_map_untouched()
    test_hypothesis_shorthand_targets_file()
    test_hypothesis_full_stem()
    test_target_shown_even_over_budget()
    test_no_mention_fills_smallest_first()
    print("\nAll SR prompt-budget tests passed.")
