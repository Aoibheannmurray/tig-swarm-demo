// initial_algorithms/knapsack.rs
//
// Starting algorithm for the knapsack challenge — broadcast to every
// agent on a fresh knapsack trajectory. See initial_algorithms/vehicle_routing.rs
// for the broader docs; this file follows the same pattern with knapsack-specific
// `Challenge` / `Solution` types.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};

pub fn solve_challenge(
    _challenge: &Challenge,
    _save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    unimplemented!("initial knapsack algorithm not yet implemented for this swarm");
}
