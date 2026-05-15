# Vector Search Challenge (GPU)

This is a **GPU challenge**. Both instance generation and solution evaluation run on CUDA. Your solver receives GPU handles and can launch custom CUDA kernels.

## Problem

Given a database of high-dimensional vectors and a set of query vectors, return one database index per query such that the **mean Euclidean distance** between each query and its assigned database vector is minimised.

Vectors live in 250-dimensional space and are sampled from a mixture of anisotropic Gaussian clusters (log-normal cluster sizes around an average of ~700 points per cluster). The database is 100× the number of queries, so a track with `n_queries=100` has 10,000 database vectors.

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_queries: u32,
    pub vector_dims: u32,                // always 250
    pub database_size: u32,              // always 100 * num_queries
    // GPU-resident, row-major. Vector i occupies [i*vector_dims .. (i+1)*vector_dims].
    pub d_database_vectors: CudaSlice<f32>,
    pub d_query_vectors: CudaSlice<f32>,
}

pub struct Solution {
    pub indexes: Vec<usize>,  // one database index per query, length == num_queries
}
```

**Vector layout.** Both `d_database_vectors` and `d_query_vectors` are flat row-major `f32` arrays of length `database_size * vector_dims` and `num_queries * vector_dims` respectively. Query `q`'s components are at `d_query_vectors[q * vector_dims .. (q+1) * vector_dims]`; same indexing for the database.

## Distance Metric

For each query `q`, with assigned database index `solution.indexes[q]`:

```
d(q) = sqrt( sum over k of (query[q][k] - db[indexes[q]][k])^2 )
```

The evaluator sums these per-query distances on GPU and returns `total / num_queries` as the mean.

## Feasibility Constraints

- `solution.indexes.len()` must equal `num_queries`.
- Every value in `solution.indexes` must be a valid database index, i.e. in `[0, database_size)`.

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

- Call `save_solution()` whenever you find an improved solution -- only the last call is kept, and the solver may be killed at any time. Write anytime algorithms that save early and improve iteratively (e.g. emit a brute-force or random-baseline solution immediately, then refine).
- Use `challenge.seed` for any randomness to keep results reproducible.
- `module` provides access to compiled CUDA kernels (both the challenge's built-in kernels and your custom kernels from `algorithm/kernels.cu`). Load a kernel with `module.load_function("kernel_name")`.
- `stream` is the CUDA stream for memory operations and kernel launches.
- `prop` contains device properties (e.g. max threads per block, shared memory size).

## GPU Data Access

All challenge data fields prefixed with `d_` are GPU-resident (`CudaSlice`). To read them on the CPU:

```rust
let queries: Vec<f32> = stream.memcpy_dtov(&challenge.d_query_vectors)?;
```

To upload CPU data to the GPU:

```rust
let d_indexes = stream.memcpy_stod(&my_indexes_vec)?;
```

For large arrays (the database can hold millions of floats), prefer launching custom kernels over copying to host.

## Custom CUDA Kernels

Agents edit **two files**:

- `algorithm/mod.rs` -- Rust solver logic.
- `algorithm/kernels.cu` -- Custom CUDA C kernels.

Any `extern "C" __global__` function defined in `algorithm/kernels.cu` is compiled and available via `module.load_function("kernel_name")`. The challenge's own kernels (from `vector_search/kernels.cu`) are also available. Libraries from the `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` image can be included (e.g. `curand_kernel.h`).

Example kernel launch from Rust:

```rust
let my_kernel = module.load_function("my_kernel")?;
let cfg = LaunchConfig {
    grid_dim: (grid_size, 1, 1),
    block_dim: (block_size, 1, 1),  // max 1024 threads per block
    shared_mem_bytes: 0,
};
unsafe {
    stream
        .launch_builder(&my_kernel)
        .arg(&challenge.num_queries)
        .arg(&challenge.vector_dims)
        .arg(&challenge.d_query_vectors)
        .arg(&challenge.d_database_vectors)
        .arg(&mut d_output)
        .launch(cfg)?;
}
stream.synchronize()?;
```

When launching kernels with multiple blocks, writes must be to non-overlapping memory locations for determinism. Hardcode `LaunchConfig` values (do not exceed 1024 for grid dimensions for compute 3.6 compatibility).

## Exact Method Signatures

### `Challenge` methods

```rust
// Validate solution and return the mean Euclidean distance across queries.
// Returns Err if any index is out of range or the solution length is wrong.
pub fn evaluate_average_distance(
    &self,
    solution: &Solution,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> Result<f32>
```

No other public helper methods are available on `Challenge`. You have direct access to all fields listed in the Types section.

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `cudarc`, `std::*` (collections, time, sync, etc.).

### Constants

```rust
pub const MAX_THREADS_PER_BLOCK: u32 = 1024;
```
