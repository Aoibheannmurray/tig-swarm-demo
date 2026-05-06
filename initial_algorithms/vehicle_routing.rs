// initial_algorithms/vehicle_routing.rs
//
// Starting algorithm for the vehicle_routing challenge — broadcast to every
// agent on a fresh VRP trajectory: the agent's first iteration on this
// challenge, and again whenever a trajectory reset draws the "fresh start"
// slot from the per-challenge inactive-algorithms pool.
//
// Edit this file before running `python setup.py create` to provide a
// custom starter for VRP. Left unchanged, agents start from this stub and
// must author the body themselves before they can produce a feasible
// solution.
//
// `Challenge` and `Solution` come from the VRP module via `super::*`.
// See CHALLENGE.md for the type shapes, scoring rules, and tips.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};

pub fn solve_challenge(
    _challenge: &Challenge,
    _save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    unimplemented!("initial vehicle_routing algorithm not yet implemented for this swarm");
}
