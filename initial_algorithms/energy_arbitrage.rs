// initial_algorithms/energy_arbitrage.rs
//
// Starting algorithm for the energy_arbitrage challenge — broadcast to every
// agent on a fresh energy_arbitrage trajectory. See
// initial_algorithms/vehicle_routing.rs for the broader docs; this file
// follows the same pattern with energy_arbitrage-specific `Challenge` /
// `Solution` types.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};

pub fn solve_challenge(
    _challenge: &Challenge,
    _save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    unimplemented!("initial energy_arbitrage algorithm not yet implemented for this swarm");
}
