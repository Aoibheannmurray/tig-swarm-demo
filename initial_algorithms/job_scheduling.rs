// initial_algorithms/job_scheduling.rs
//
// Starting algorithm for the job_scheduling challenge — broadcast to every
// agent on a fresh job_scheduling trajectory. See
// initial_algorithms/vehicle_routing.rs for the broader docs; this file
// follows the same pattern with job_scheduling-specific `Challenge` /
// `Solution` types.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};

pub fn solve_challenge(
    _challenge: &Challenge,
    _save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    unimplemented!("initial job_scheduling algorithm not yet implemented for this swarm");
}
