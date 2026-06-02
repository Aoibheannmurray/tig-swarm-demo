// initial_algorithms/neuralnet_optimizer/seeds/sgd.rs
//
// SEED algorithm for the neuralnet_optimizer (GPU) challenge: SGD with the
// weight update run on the GPU via the `sgd_step` kernel (see sgd.cu).
//
// The challenge fixes the training scaffold (`training_loop`) and asks you to
// fill three optimizer callbacks. `optimizer_step` must return, per parameter
// tensor, the *update to add* to the weights — the scaffold applies it with
// `apply_parameter_updates_direct` (params += update). This seed computes that
// update on the GPU: update = -learning_rate * clip(grad, -1, 1).
//
// NOTE: an earlier host-side version of this seed returned the new weights
// (w - lr*grad) instead of the update, which the scaffold then *added* to the
// existing weights — roughly doubling them each step and driving the score to
// the floor. Returning the delta (as below) is the correctness fix; doing it in
// a kernel also drops the per-step device<->host round-trip.
//
// learning_rate is read from the optional hyperparameters (default 0.001). A
// refiner's job is to add momentum / Adam / an lr schedule on top of this.
//
// Verified on an L40 (C3): compiles, runs, feasible, and actually learns —
// loss 0.119 < noise floor 0.356 (score +666570 on n_hidden=4).

use anyhow::Result;
use cudarc::{
    driver::{CudaModule, CudaSlice, CudaStream, LaunchConfig, PushKernelArg},
    runtime::sys::cudaDeviceProp,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::sync::Arc;
use crate::neuralnet_optimizer::*;

#[derive(Serialize, Deserialize)]
pub struct Hyperparameters {
    // Learning rate applied to each gradient step.
    pub learning_rate: f32,
}

pub fn help() {
    println!("Simple SGD optimizer. update = -learning_rate * clip(gradient, -1, 1).");
    println!("Hyperparameters:");
    println!("  learning_rate (f32, default 0.001)");
}

const THREADS_PER_BLOCK: u32 = 1024;
const DEFAULT_LEARNING_RATE: f32 = 0.001;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    prop: &cudaDeviceProp,
) -> Result<()> {
    let learning_rate = hyperparameters
        .as_ref()
        .and_then(|hp| hp.get("learning_rate"))
        .and_then(|v| v.as_f64())
        .map(|v| v as f32)
        .unwrap_or(DEFAULT_LEARNING_RATE);

    LEARNING_RATE.with(|lr| *lr.borrow_mut() = learning_rate);

    // boilerplate for training loop
    // recommend not modifying this function unless you have a good reason
    training_loop(
        challenge,
        save_solution,
        module,
        stream,
        prop,
        optimizer_init_state,
        optimizer_query_at_params,
        optimizer_step,
    )?;

    Ok(())
}

thread_local! {
    static LEARNING_RATE: std::cell::RefCell<f32> = std::cell::RefCell::new(DEFAULT_LEARNING_RATE);
}

#[derive(Clone)]
struct OptimizerState {
    learning_rate: f32,
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

fn optimizer_init_state(
    _seed: [u8; 32],
    _param_sizes: &[usize],
    _stream: Arc<CudaStream>,
    _module: Arc<CudaModule>,
    _prop: &cudaDeviceProp,
) -> Result<Box<dyn OptimizerStateTrait>> {
    let learning_rate = LEARNING_RATE.with(|lr| *lr.borrow());
    Ok(Box::new(OptimizerState { learning_rate }))
}

fn optimizer_query_at_params(
    _optimizer_state: &dyn OptimizerStateTrait,
    _model_params: &[CudaSlice<f32>],
    _epoch: usize,
    _train_loss: Option<f32>,
    _val_loss: Option<f32>,
    _stream: Arc<CudaStream>,
    _module: Arc<CudaModule>,
    _prop: &cudaDeviceProp,
) -> Result<Option<Vec<CudaSlice<f32>>>> {
    // Simple SGD doesn't modify parameters before the gradient calculation
    Ok(None)
}

fn optimizer_step(
    optimizer_state: &mut dyn OptimizerStateTrait,
    _model_params: &[CudaSlice<f32>],
    gradients: &[CudaSlice<f32>],
    _epoch: usize,
    _train_loss: Option<f32>,
    _val_loss: Option<f32>,
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    _prop: &cudaDeviceProp,
) -> Result<Vec<CudaSlice<f32>>> {
    let state = optimizer_state
        .as_any()
        .downcast_ref::<OptimizerState>()
        .unwrap();
    let learning_rate = state.learning_rate;

    let sgd_kernel = module.load_function("sgd_step")?;

    let mut updates = Vec::with_capacity(gradients.len());
    for grad in gradients {
        let n = grad.len();
        let mut update = stream.alloc_zeros::<f32>(n)?;
        if n > 0 {
            let n_u32 = n as u32;
            let grid_dim = (n_u32 + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK;
            let cfg = LaunchConfig {
                grid_dim: (grid_dim, 1, 1),
                block_dim: (THREADS_PER_BLOCK, 1, 1),
                shared_mem_bytes: 0,
            };
            unsafe {
                stream
                    .launch_builder(&sgd_kernel)
                    .arg(grad)
                    .arg(&n_u32)
                    .arg(&learning_rate)
                    .arg(&mut update)
                    .launch(cfg)?;
            }
        }
        updates.push(update);
    }

    Ok(updates)
}
