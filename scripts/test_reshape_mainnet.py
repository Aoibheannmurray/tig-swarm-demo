"""Tests for the mainnet -> swarm algorithm reshaper (Component 2).

Self-running: `python scripts/test_reshape_mainnet.py` (no pytest in this repo).

Covers the optimizer-hook reshape for neuralnet_optimizer (strip the
harness-owned solve_challenge + training_loop while keeping the three optimizer
hooks), string/comment-aware brace matching, the pass-through for ordinary
challenges, and the skip-with-error path when the result can't satisfy the
swarm validator.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from challenge_files import reshape_mainnet_for_swarm  # noqa: E402

# A mainnet-style neuralnet entry file: solve_challenge + training_loop live in
# mod.rs (harness-owned in the swarm) alongside the three optimizer hooks. The
# training_loop body deliberately contains braces inside a string, a char
# literal, and comments to exercise the brace matcher.
NEURALNET_MAINNET = r'''use super::*;
use anyhow::Result;

/// Harness entry point in the mainnet layout.
#[allow(clippy::too_many_arguments)]
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
) -> Result<()> {
    training_loop(challenge, save_solution, module)
}

fn training_loop(challenge: &Challenge, save: &dyn Fn(), module: Arc<CudaModule>) -> Result<()> {
    let s = "a string with a } and a { inside";   // comment with } brace
    let c = '}';
    let block = /* nested { braces } in a comment */ 0;
    for _ in 0..3 { let _ = (s, c, block); }
    Ok(())
}

pub fn optimizer_init_state(seed: [u8; 32]) -> Result<Box<dyn OptimizerStateTrait>> {
    Ok(Box::new(MyState { step: 0 }))
}

pub fn optimizer_query_at_params() -> Result<Option<Vec<CudaSlice<f32>>>> {
    Ok(None)
}

pub fn optimizer_step() -> Result<Vec<CudaSlice<f32>>> {
    Ok(vec![])
}
'''

KNAPSACK_MAINNET = r'''use super::*;
use anyhow::Result;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let _ = "items: { value }";
    save_solution(&Solution::default())
}
'''


def test_neuralnet_strips_harness_owned_fns():
    files, err = reshape_mainnet_for_swarm(
        "neuralnet_optimizer", {"mod.rs": NEURALNET_MAINNET, "kernels.cu": "// k\n"})
    assert err is None, f"unexpected reshape error: {err}"
    code = files["mod.rs"]
    assert "fn solve_challenge(" not in code, "solve_challenge not stripped"
    assert "fn training_loop(" not in code, "training_loop not stripped"
    # The harness-owned doc comment / attribute above solve_challenge went too.
    assert "Harness entry point" not in code, "dangling doc comment left behind"
    assert "too_many_arguments" not in code, "dangling attribute left behind"
    # All three hooks survive, with bodies intact.
    for hook in ("optimizer_init_state", "optimizer_query_at_params", "optimizer_step"):
        assert f"fn {hook}(" in code, f"hook {hook} was lost"
    assert "MyState { step: 0 }" in code, "optimizer_init_state body mangled"
    # Sibling kernel preserved.
    assert files["kernels.cu"] == "// k\n"
    print("PASS test_neuralnet_strips_harness_owned_fns")


def test_brace_matcher_handles_strings_and_comments():
    # If the brace matcher miscounted the `}` inside the string/char/comment in
    # training_loop, it would either under-strip (leave a stray `}`) or
    # over-strip into a hook. Assert the boundary is clean: the first hook's
    # signature is the first `fn` left in the file.
    files, err = reshape_mainnet_for_swarm(
        "neuralnet_optimizer", {"mod.rs": NEURALNET_MAINNET})
    assert err is None, err
    code = files["mod.rs"]
    assert code.count("fn ") == 3, f"expected exactly 3 fns left, got {code.count('fn ')}"
    # No orphaned closing brace floating at top level before the first hook.
    head = code[: code.index("fn optimizer_init_state")]
    assert head.count("{") == head.count("}"), f"unbalanced head:\n{head!r}"
    print("PASS test_brace_matcher_handles_strings_and_comments")


def test_ordinary_challenge_passes_through():
    files, err = reshape_mainnet_for_swarm("knapsack", {"mod.rs": KNAPSACK_MAINNET})
    assert err is None, f"knapsack reshape errored: {err}"
    assert "fn solve_challenge(" in files["mod.rs"], "solve_challenge must remain for CPU"
    print("PASS test_ordinary_challenge_passes_through")


def test_missing_hook_is_rejected():
    # Drop optimizer_step: after stripping the harness fns there's no valid
    # hooks-only file, so the reshaper must skip-with-error.
    broken = NEURALNET_MAINNET.replace(
        "pub fn optimizer_step() -> Result<Vec<CudaSlice<f32>>> {\n    Ok(vec![])\n}\n", "")
    files, err = reshape_mainnet_for_swarm("neuralnet_optimizer", {"mod.rs": broken})
    assert files is None, "expected reject, got a files map"
    assert err and "optimizer_step" in err, f"error should name the missing hook: {err}"
    print("PASS test_missing_hook_is_rejected")


def test_anchor_normalized_to_mainnet_form():
    # Legacy `use super::*;` (old swarm-only anchor) is migrated in place…
    files, err = reshape_mainnet_for_swarm(
        "neuralnet_optimizer", {"mod.rs": NEURALNET_MAINNET})
    assert err is None, err
    anchor = "use tig_challenges::neuralnet_optimizer::*;"
    assert anchor in files["mod.rs"], "legacy super::* not migrated to the anchor"
    assert "use super::*;" not in files["mod.rs"], "legacy anchor left behind"
    # …and a bundle with no anchor at all gets it inserted at the top.
    code = NEURALNET_MAINNET.replace("use super::*;\n", "", 1)
    files, err = reshape_mainnet_for_swarm("neuralnet_optimizer", {"mod.rs": code})
    assert err is None, err
    assert files["mod.rs"].startswith(anchor), "anchor not prepended"
    print("PASS test_anchor_normalized_to_mainnet_form")


def test_no_mod_rs_entry_is_rejected():
    files, err = reshape_mainnet_for_swarm("knapsack", {"lib.rs": "// nope\n"})
    assert files is None and err, "bundle without mod.rs must be rejected"
    print("PASS test_no_mod_rs_entry_is_rejected")


if __name__ == "__main__":
    test_neuralnet_strips_harness_owned_fns()
    test_brace_matcher_handles_strings_and_comments()
    test_ordinary_challenge_passes_through()
    test_missing_hook_is_rejected()
    test_anchor_normalized_to_mainnet_form()
    test_no_mod_rs_entry_is_rejected()
    print("\nAll Component 2 reshape tests passed.")
