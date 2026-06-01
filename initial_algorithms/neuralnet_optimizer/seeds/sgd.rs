// initial_algorithms/neuralnet_optimizer/seeds/sgd.rs
//
// Simple SEED algorithm for the neuralnet_optimizer (GPU) challenge: plain SGD.
//
// !!! UNVERIFIED !!!  This environment has no CUDA toolkit / GPU (cudarc fails
// to build without `nvcc`), so this seed could NOT be compiled or run here.
// Validate and fix it on a CUDA box before relying on it.
//
// The challenge fixes the training scaffold (`training_loop`) and asks you to
// fill three optimizer callbacks. This seed implements vanilla SGD with a
// constant learning rate: for every parameter tensor, new = w - lr * grad.
//
// To avoid needing a custom CUDA kernel (the shipped .cu has none), the update
// is done on the HOST: copy params + grads back with `memcpy_dtov`, compute the
// step in Rust, and upload the result with `memcpy_stod`. Correct but slow (a
// device<->host round-trip every step) — a weaker model's job is to make the
// update run on the GPU (a fused axpy kernel) and to add momentum / Adam, not
// to rediscover gradient descent.

use anyhow::Result;
use cudarc::{
    driver::{CudaModule, CudaSlice, CudaStream},
    runtime::sys::cudaDeviceProp,
};
use serde_json::{Map, Value};
use std::sync::Arc;
use crate::neuralnet_optimizer::*;

const LEARNING_RATE: f32 = 0.01;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    prop: &cudaDeviceProp,
) -> Result<()> {
    // Standard training scaffold — drives our three optimizer callbacks.
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

#[derive(Clone)]
struct OptimizerState;

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
    // Vanilla SGD is stateless.
    Ok(Box::new(OptimizerState))
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
    // Evaluate the gradient at the current parameters — no override.
    Ok(None)
}

fn optimizer_step(
    _optimizer_state: &mut dyn OptimizerStateTrait,
    model_params: &[CudaSlice<f32>],
    gradients: &[CudaSlice<f32>],
    _epoch: usize,
    _train_loss: Option<f32>,
    _val_loss: Option<f32>,
    stream: Arc<CudaStream>,
    _module: Arc<CudaModule>,
    _prop: &cudaDeviceProp,
) -> Result<Vec<CudaSlice<f32>>> {
    let mut updated: Vec<CudaSlice<f32>> = Vec::with_capacity(model_params.len());
    for (param, grad) in model_params.iter().zip(gradients.iter()) {
        let w: Vec<f32> = stream.memcpy_dtov(param)?;
        let g: Vec<f32> = stream.memcpy_dtov(grad)?;
        let next: Vec<f32> = w
            .iter()
            .zip(g.iter())
            .map(|(&wi, &gi)| wi - LEARNING_RATE * gi)
            .collect();
        updated.push(stream.memcpy_stod(&next)?);
    }
    Ok(updated)
}
