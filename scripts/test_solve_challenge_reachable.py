"""`solve_challenge` may live in a submodule and be re-exported from the entry.

Why this exists: mainnet's larger algorithms split across modules and leave
mod.rs as declarations + `pub use`. TIG calls `{ALGORITHM}::solve_challenge`,
which resolves through that re-export — but our validator checked the entry
file for a literal `fn solve_challenge(`, so it rejected them. That cost us
job_scheduling/adaptive_js_v9 at every seeding attempt, and would have frozen
any agent that refactored into submodules: every later edit failing validation
on a file that was never wrong.

Self-running: `python scripts/test_solve_challenge_reachable.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "server"))

from challenge_files import (  # noqa: E402
    reshape_mainnet_for_swarm,
    solve_challenge_reachable,
    validate_code,
)

# job_scheduling/adaptive_js_v9's actual mod.rs, verbatim.
ADAPTIVE_JS_V9_MOD = """pub mod types;
pub mod preprocess;
mod infra_shared;
pub mod solver;
pub mod flow_shop;
pub mod hybrid_flow_shop;
pub mod job_shop;
pub mod fjsp_medium;
pub mod fjsp_high;

pub use solver::{solve_challenge, help};
"""

SOLVER_RS = """use anyhow::Result;
use serde_json::{Map, Value};
use tig_challenges::job_scheduling::*;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    Ok(())
}

pub fn help() {}
"""


def test_defined_in_entry():
    assert solve_challenge_reachable("pub fn solve_challenge(c: &C) {}") is True
    print("PASS test_defined_in_entry")


def test_reexported_from_submodule():
    files = {"mod.rs": ADAPTIVE_JS_V9_MOD, "solver.rs": SOLVER_RS}
    assert solve_challenge_reachable(ADAPTIVE_JS_V9_MOD, files) is True
    print("PASS test_reexported_from_submodule")


def test_glob_reexport_of_the_defining_module():
    entry = "pub mod solver;\npub use solver::*;\n"
    files = {"mod.rs": entry, "solver.rs": SOLVER_RS}
    assert solve_challenge_reachable(entry, files) is True
    print("PASS test_glob_reexport_of_the_defining_module")


def test_glob_reexport_of_an_unrelated_module_does_not_count():
    """`pub use helpers::*;` must not vouch for a solve_challenge that lives in
    solver.rs and is never re-exported — that genuinely doesn't compile."""
    entry = "pub mod solver;\npub mod helpers;\npub use helpers::*;\n"
    files = {"mod.rs": entry, "solver.rs": SOLVER_RS, "helpers.rs": "pub fn h() {}"}
    assert solve_challenge_reachable(entry, files) is False
    print("PASS test_glob_reexport_of_an_unrelated_module_does_not_count")


def test_defined_but_never_reexported_is_still_a_failure():
    entry = "pub mod solver;\n"
    files = {"mod.rs": entry, "solver.rs": SOLVER_RS}
    assert solve_challenge_reachable(entry, files) is False
    print("PASS test_defined_but_never_reexported_is_still_a_failure")


def test_absent_everywhere_is_a_failure():
    entry = "pub mod helpers;\npub use helpers::*;\n"
    files = {"mod.rs": entry, "helpers.rs": "pub fn h() {}"}
    assert solve_challenge_reachable(entry, files) is False
    assert solve_challenge_reachable(entry) is False       # no bundle in view
    print("PASS test_absent_everywhere_is_a_failure")


def test_validate_code_accepts_the_multifile_bundle():
    files = {"mod.rs": ADAPTIVE_JS_V9_MOD, "solver.rs": SOLVER_RS}
    config = {"challenge": "job_scheduling"}

    # Entry alone: still rejected, correctly — nothing proves it resolves.
    assert validate_code(ADAPTIVE_JS_V9_MOD, config) is not None
    # With the bundle: accepted.
    assert validate_code(ADAPTIVE_JS_V9_MOD, config, files=files) is None
    print("PASS test_validate_code_accepts_the_multifile_bundle")


def test_declarations_only_entry_needs_no_anchor_import():
    """The entry names no challenge types, so demanding
    `use tig_challenges::<ch>::*;` there would force an unused import (rustc
    warns). A entry that DOES define code still needs the anchor."""
    files = {"mod.rs": ADAPTIVE_JS_V9_MOD, "solver.rs": SOLVER_RS}
    config = {"challenge": "job_scheduling"}
    assert validate_code(ADAPTIVE_JS_V9_MOD, config, files=files) is None

    inline = ("pub fn solve_challenge(c: &Challenge) -> Result<()> { Ok(()) }\n")
    assert validate_code(inline, config) is not None          # anchor missing
    assert validate_code(
        "use tig_challenges::job_scheduling::*;\n" + inline, config) is None
    print("PASS test_declarations_only_entry_needs_no_anchor_import")


def test_reshape_seeds_adaptive_js_v9_shape():
    """The end-to-end regression: this bundle must now survive reshaping."""
    files = {"mod.rs": ADAPTIVE_JS_V9_MOD, "solver.rs": SOLVER_RS,
             "types.rs": "pub struct EffortConfig;"}
    out, err = reshape_mainnet_for_swarm("job_scheduling", files)
    assert err is None, err
    assert out is not None
    # Left byte-identical to mainnet: a declarations-only entry gets no anchor.
    assert out["mod.rs"] == ADAPTIVE_JS_V9_MOD
    assert out["solver.rs"] == SOLVER_RS      # submodules pass through untouched
    assert set(out) == set(files)
    print("PASS test_reshape_seeds_adaptive_js_v9_shape")


def test_reshape_still_rejects_an_unreachable_bundle():
    files = {"mod.rs": "pub mod solver;\n", "solver.rs": SOLVER_RS}
    out, err = reshape_mainnet_for_swarm("job_scheduling", files)
    assert out is None
    assert "not reachable" in err, err
    print("PASS test_reshape_still_rejects_an_unreachable_bundle")


def test_server_side_reshape_agrees():
    """server/mainnet_seed.py duplicates the check (it ships without scripts/).
    The two must not drift — the Admin Console seeds through that copy."""
    import mainnet_seed

    files = {"mod.rs": ADAPTIVE_JS_V9_MOD, "solver.rs": SOLVER_RS}
    assert mainnet_seed._solve_challenge_reachable(ADAPTIVE_JS_V9_MOD, files) is True

    out, err = mainnet_seed.reshape_for_swarm("job_scheduling", files)
    assert err == "", err
    assert out is not None and "solver.rs" in out

    bad = {"mod.rs": "pub mod solver;\n", "solver.rs": SOLVER_RS}
    out, err = mainnet_seed.reshape_for_swarm("job_scheduling", bad)
    assert out is None and "not reachable" in err, err

    unrelated_entry = "pub mod solver;\npub mod helpers;\npub use helpers::*;\n"
    assert mainnet_seed._solve_challenge_reachable(
        unrelated_entry, {"solver.rs": SOLVER_RS, "helpers.rs": "pub fn h() {}"}
    ) is False
    print("PASS test_server_side_reshape_agrees")


def test_neuralnet_still_rejects_solve_challenge():
    """Optimizer-hook challenges are unaffected: solve_challenge is harness-
    owned there and must stay absent, however it's spelled."""
    hooks = ("use tig_challenges::neuralnet_optimizer::*;\n"
             "pub fn optimizer_init_state() {}\n"
             "pub fn optimizer_query_at_params() {}\n"
             "pub fn optimizer_step() {}\n")
    config = {"challenge": "neuralnet_optimizer"}
    assert validate_code(hooks, config) is None
    assert validate_code(hooks + "pub fn solve_challenge() {}", config) is not None
    print("PASS test_neuralnet_still_rejects_solve_challenge")


if __name__ == "__main__":
    test_defined_in_entry()
    test_reexported_from_submodule()
    test_glob_reexport_of_the_defining_module()
    test_glob_reexport_of_an_unrelated_module_does_not_count()
    test_defined_but_never_reexported_is_still_a_failure()
    test_absent_everywhere_is_a_failure()
    test_validate_code_accepts_the_multifile_bundle()
    test_declarations_only_entry_needs_no_anchor_import()
    test_reshape_seeds_adaptive_js_v9_shape()
    test_reshape_still_rejects_an_unreachable_bundle()
    test_server_side_reshape_agrees()
    test_neuralnet_still_rejects_solve_challenge()
    print("ALL PASS")
