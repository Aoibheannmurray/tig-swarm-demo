# Neural Network Optimizer Challenge (GPU)

## Problem

Optimize a neural network's weights to minimize test loss on a regression task. You implement the **optimizer** (e.g. SGD, Adam, or something novel) — the training loop, model architecture, forward/backward passes, and loss computation are all provided. All computation runs on GPU via the `cudarc` crate.

This is a **GPU challenge**. Your solver receives CUDA handles (`CudaModule`, `CudaStream`, `cudaDeviceProp`) and all parameter/gradient tensors are `CudaSlice<f32>` living in device memory. You edit two files: `algorithm/mod.rs` (Rust) and `algorithm/kernels.cu` (CUDA C).

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_hidden_layers: usize,    // number of hidden layers in the MLP
    pub hidden_layers_dims: usize,   // width of each hidden layer (256)
    pub batch_size: usize,           // mini-batch size (128)
    pub max_epochs: usize,           // maximum training epochs (1000)
    pub patience: usize,             // early-stopping patience (50 epochs without improvement)
    pub min_loss_delta: f32,         // minimum validation loss improvement to count (1e-7)
    pub num_frozen_layers: usize,    // number of layers (from output end) whose gradients are zeroed
    pub dataset: Dataset,
}

pub struct Dataset {
    pub inputs: CudaSlice<f32>,          // all inputs (train + validation + test), on GPU
    pub targets_noisy: CudaSlice<f32>,   // noisy targets
    pub targets_true_f: CudaSlice<f32>,  // true function values (no noise)
    pub train_size: usize,               // 1000
    pub validation_size: usize,          // 200
    pub test_size: usize,                // 250
    pub input_dims: usize,               // 1
    pub output_dims: usize,              // 2
}

pub struct Solution {
    pub weights: Vec<Vec<Vec<f32>>>,     // per-layer weight matrices
    pub biases: Vec<Vec<f32>>,           // per-layer bias vectors
    pub epochs_used: usize,              // how many epochs were trained
    pub bn_weights: Vec<Vec<f32>>,       // BatchNorm scale parameters
    pub bn_biases: Vec<Vec<f32>>,        // BatchNorm shift parameters
    pub bn_running_means: Vec<Vec<f32>>, // BatchNorm running means
    pub bn_running_vars: Vec<Vec<f32>>,  // BatchNorm running variances
}
```

## MLP Architecture

The model is a fully-connected MLP with the following layer structure:

```
Input (1) -> [Linear -> ReLU -> BatchNorm1d] x n_hidden -> Linear (2) -> Output
```

- Each hidden layer has 256 units.
- Hidden layers use: Linear (cuBLAS GEMM) -> ReLU activation -> BatchNorm1d (cuDNN).
- The final layer is a plain Linear (no activation, no batch norm).
- Weights are initialized via Kaiming/He initialization on GPU.
- The last `num_frozen_layers` layers (counted from the output) have `requires_grad = false` and receive zero gradients.

## What You Implement

You implement three optimizer callbacks that the provided `training_loop` calls:

### `optimizer_init_state`

```rust
fn optimizer_init_state(
    seed: [u8; 32],
    param_sizes: &[usize],
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    prop: &cudaDeviceProp,
) -> Result<Box<dyn OptimizerStateTrait>>
```

Called once before training. Allocate any optimizer state you need (momentum buffers, second-moment estimates, learning rate schedules, etc.) and return it as a boxed trait object. `param_sizes` gives the length of each parameter tensor in the model.

### `optimizer_query_at_params`

```rust
fn optimizer_query_at_params(
    optimizer_state: &dyn OptimizerStateTrait,
    model_params: &[CudaSlice<f32>],
    epoch: usize,
    train_loss: Option<f32>,
    val_loss: Option<f32>,
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    prop: &cudaDeviceProp,
) -> Result<Option<Vec<CudaSlice<f32>>>>
```

Called before each forward pass. Optionally return modified parameters to use for gradient computation (e.g. for lookahead or parameter prediction). Return `Ok(None)` to use the current model parameters as-is. If you return `Some(modified_params)`, the training loop computes gradients at those parameters but then restores the originals before calling `optimizer_step`.

### `optimizer_step`

```rust
fn optimizer_step(
    optimizer_state: &mut dyn OptimizerStateTrait,
    model_params: &[CudaSlice<f32>],
    gradients: &[CudaSlice<f32>],
    epoch: usize,
    train_loss: Option<f32>,
    val_loss: Option<f32>,
    stream: Arc<CudaStream>,
    module: Arc<CudaModule>,
    prop: &cudaDeviceProp,
) -> Result<Vec<CudaSlice<f32>>>
```

Called after each backward pass. Given the current parameters and their gradients (both as `CudaSlice<f32>` on GPU), compute and return parameter **updates** (deltas). The training loop applies these updates additively: `param += update`. For standard SGD, you would return `-lr * gradient` for each parameter tensor.

The parameter/gradient layout is:
1. For each Linear layer: weight, bias
2. For each BatchNorm layer: weight, bias, running_mean, running_var

Note: frozen layers receive zero gradients, and BatchNorm running_mean/running_var always receive zero gradients.

## Training Loop (Provided)

The `training_loop` function handles:
- Model construction and weight initialization (seeded).
- Epoch loop with shuffled mini-batches.
- Forward pass (Linear via cuBLAS, BatchNorm via cuDNN, ReLU activation).
- MSE loss computation and backward pass.
- Calling your three optimizer callbacks at the right times.
- Validation loss computation each epoch.
- Early stopping: saves the solution whenever validation loss improves by at least `min_loss_delta`, stops after `patience` epochs without improvement.
- `save_solution()` is called automatically when validation loss improves.

## Feasibility Constraints

- The solution must contain valid weights/biases matching the MLP architecture dimensions.
- The solution must be loadable into the MLP and produce a finite test loss.

## Solver Interface

```rust
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    prop: &cudaDeviceProp,
) -> Result<()>
```

- The default implementation calls `training_loop(...)` with your three optimizer callbacks. You generally should not need to modify `solve_challenge` itself.
- `module` is the compiled CUDA module containing kernels from both `neuralnet_optimizer/kernels.cu` (challenge kernels) and `algorithm/kernels.cu` (your custom kernels).
- `stream` is the CUDA stream for all GPU operations.
- `prop` contains device properties (compute capability, shared memory size, etc.).

## Files You Edit

- **`algorithm/mod.rs`** — Rust code: define `OptimizerState`, implement `optimizer_init_state`, `optimizer_query_at_params`, and `optimizer_step`.
- **`algorithm/kernels.cu`** — CUDA C code: define any custom GPU kernels your optimizer needs (e.g. fused update kernels, custom reductions). Kernels from both this file and the challenge's `kernels.cu` are available via `module.load_function("kernel_name")`. You can use any library available in `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` (e.g. `curand_kernel.h`, `math.h`).

## Available Crates

`anyhow`, `serde`, `serde_json`, `rand` (StdRng, SeedableRng), `cudarc` (CudaSlice, CudaStream, CudaModule, CudaBlas, Cudnn, LaunchConfig, PushKernelArg), `std::*`.
