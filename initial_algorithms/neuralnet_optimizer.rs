// TIG's UI uses the pattern `tig_challenges::<challenge_name>` to automatically detect your algorithm's challenge
use anyhow::{anyhow, Result};
use cudarc::{
    driver::{CudaModule, CudaSlice, CudaStream, LaunchConfig, PushKernelArg},
    runtime::sys::cudaDeviceProp,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use crate::neuralnet_optimizer::*;

#[derive(Serialize, Deserialize)]
pub struct Hyperparameters {
    // Optionally define hyperparameters here. Example:
    // pub param1: usize,
    // pub param2: f64,
}

pub fn help() {
    // Print help information about your algorithm here. It will be invoked with `help_algorithm` script
    println!("No help information provided.");
}

const THREADS_PER_BLOCK: u32 = 1024;

// NOTE: `solve_challenge` and the training loop are harness-owned and NOT part
// of this file — the benchmark calls a fixed `solve_challenge` that runs the
// canonical `training_loop` (enforcing the epoch budget and the train/val/test
// split) and invokes the three optimizer hooks below. Implement ONLY those
// hooks (plus `Hyperparameters`, `help`, and any helpers/kernels you need).
// The hooks MUST stay `pub fn` with these exact signatures so the harness can
// call them.

#[derive(Clone)]
struct OptimizerState {
    // define any state your optimizer needs here
}

impl OptimizerStateTrait for OptimizerState {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
        self
    }

    fn box_clone(&self) -> Box<dyn OptimizerStateTrait> {
        Box::new(self.clone())
    }
}

pub fn optimizer_init_state(
    seed: [u8; 32],
    param_sizes: &[usize],
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    prop: &cudaDeviceProp,
) -> Result<Box<dyn OptimizerStateTrait>> {
    // Ok(Box::new(OptimizerState {
    //      /* initialize state */
    // }))
    Err(anyhow!("Not implemented"))
}

pub fn optimizer_query_at_params(
    optimizer_state: &dyn OptimizerStateTrait,
    model_params: &[CudaSlice<f32>],
    epoch: usize,
    train_loss: Option<f32>,
    val_loss: Option<f32>,
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    prop: &cudaDeviceProp,
) -> Result<Option<Vec<CudaSlice<f32>>>> {
    // optionally set model parameters to specific values before gradient calculation
    Err(anyhow!("Not implemented"))
}

pub fn optimizer_step(
    optimizer_state: &mut dyn OptimizerStateTrait,
    model_params: &[CudaSlice<f32>],
    gradients: &[CudaSlice<f32>],
    epoch: usize,
    train_loss: Option<f32>,
    val_loss: Option<f32>,
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    prop: &cudaDeviceProp,
) -> Result<Vec<CudaSlice<f32>>> {
    // for each CudaSlice in gradients, calculate delta to adjust model parameters
    Err(anyhow!("Not implemented"))
}
