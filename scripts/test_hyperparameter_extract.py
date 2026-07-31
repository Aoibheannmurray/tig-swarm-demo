"""Tests for the multi-file HPO extraction parse + edit-apply contract (C5).

Run directly: `python scripts/test_hyperparameter_extract.py` (no pytest here).
"""

from prompts import parse_hyperparameter_response, HYPERPARAM_EDITS_SEP
import search_replace

SPEC = """{
  "hyperparameters": [
    {"name": "alpha", "type": "float", "range": [0.0, 1.0], "scale": "linear", "default": 0.1}
  ],
  "suggested_configs": [{"alpha": 0.1}, {"alpha": 0.3}]
}"""


def test_spec_only_case0():
    # No separator => Case 0 (algorithm already Map-aware), no code edits.
    parsed = parse_hyperparameter_response(SPEC)
    assert parsed["ok"], parsed
    assert parsed["edits_text"] == "", parsed["edits_text"]
    assert parsed["hyperparameters"][0]["name"] == "alpha"
    assert len(parsed["suggested_configs"]) == 2
    print("PASS test_spec_only_case0")


def test_spec_with_edits():
    block = (
        "<<<<<<< SEARCH helpers.rs\n"
        "const ALPHA: f64 = 0.1;\n"
        "=======\n"
        "let alpha = hp_alpha(hyperparameters);\n"
        ">>>>>>> REPLACE"
    )
    response = f"```json\n{SPEC}\n```\n{HYPERPARAM_EDITS_SEP}\n{block}"
    parsed = parse_hyperparameter_response(response)
    assert parsed["ok"], parsed
    assert "SEARCH helpers.rs" in parsed["edits_text"], parsed["edits_text"]
    # The edits round-trip through the shared search/replace engine.
    blocks = search_replace.parse_blocks(parsed["edits_text"])
    assert len(blocks) == 1 and blocks[0].file == "helpers.rs"
    files = {"mod.rs": "use super::*;\nmod helpers;\n",
             "helpers.rs": "const ALPHA: f64 = 0.1;\n"}
    new_map, misses = search_replace.apply_blocks(files, blocks)
    assert not misses
    assert "hp_alpha(hyperparameters)" in new_map["helpers.rs"]
    assert new_map["mod.rs"] == files["mod.rs"]  # untouched
    print("PASS test_spec_with_edits")


def test_invalid_spec_rejected():
    parsed = parse_hyperparameter_response("{ not valid json")
    assert not parsed["ok"] and parsed["error"]
    print("PASS test_invalid_spec_rejected")


def test_missing_spec_rejected():
    parsed = parse_hyperparameter_response("no json here at all")
    assert not parsed["ok"]
    print("PASS test_missing_spec_rejected")


def test_unknown_config_key_rejected():
    bad = SPEC.replace('{"alpha": 0.1}', '{"beta": 0.1}')
    parsed = parse_hyperparameter_response(bad)
    assert not parsed["ok"] and "beta" in parsed["error"], parsed
    print("PASS test_unknown_config_key_rejected")


if __name__ == "__main__":
    test_spec_only_case0()
    test_spec_with_edits()
    test_invalid_spec_rejected()
    test_missing_spec_rejected()
    test_unknown_config_key_rejected()
    print("\nAll hyperparameter-extract tests passed.")
