# Hypergraph Partitioning Challenge (GPU)

This is a **GPU challenge**. Both instance generation and solution evaluation run on CUDA. Your solver receives GPU handles and can launch custom CUDA kernels.

## Problem

Hypergraph Partitioning: assign each node of a hypergraph to one of `num_parts` groups (partitions) to minimise a connectivity metric, subject to balance constraints on partition sizes.

A hypergraph generalises a graph: each *hyperedge* connects an arbitrary subset of nodes (not just two). The connectivity metric counts, for each hyperedge, the number of distinct partitions its nodes span minus one, summed over all hyperedges. Lower connectivity means fewer hyperedges are "cut" across partitions.

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_hyperedges: u32,
    pub num_nodes: u32,
    pub num_parts: u32,                      // always 64 (2^6)
    pub max_part_size: u32,                  // ceil(num_nodes / num_parts * 1.03)
    pub total_connections: u32,
    // GPU-resident data (CudaSlice) — read with stream.memcpy_dtov()
    pub d_hyperedge_sizes: CudaSlice<i32>,   // size of each hyperedge
    pub d_hyperedge_offsets: CudaSlice<i32>, // prefix-sum offsets into d_hyperedge_nodes (length num_hyperedges+1)
    pub d_hyperedge_nodes: CudaSlice<i32>,   // flat array of node indices per hyperedge
    pub d_node_degrees: CudaSlice<i32>,      // number of hyperedges each node belongs to
    pub d_node_offsets: CudaSlice<i32>,      // prefix-sum offsets into d_node_hyperedges (length num_nodes+1)
    pub d_node_hyperedges: CudaSlice<i32>,   // flat array of hyperedge indices per node
}

pub struct Solution {
    pub partition: Vec<u32>,  // partition assignment for each node (0-based, values in [0, num_parts))
}
```

**Adjacency encoding.** The hypergraph is stored in two CSR-style (compressed sparse row) representations, both GPU-resident:

- *Hyperedge-to-nodes*: for hyperedge `i`, the nodes are `d_hyperedge_nodes[d_hyperedge_offsets[i] .. d_hyperedge_offsets[i+1]]`.
- *Node-to-hyperedges*: for node `j`, the hyperedges containing it are `d_node_hyperedges[d_node_offsets[j] .. d_node_offsets[j+1]]`.

## Connectivity Metric

For each hyperedge, count the number of distinct partitions its nodes span, minus one. Sum over all hyperedges:

```
connectivity_metric = sum over all hyperedges h of (distinct_partitions(h) - 1)
```

A hyperedge whose nodes all lie in a single partition contributes 0. The goal is to **minimise** this metric.

## Feasibility Constraints

- `partition.len()` must equal `num_nodes`.
- Every value in `partition` must be in `[0, num_parts)`.
- Each partition must contain at least 1 node and at most `max_part_size` nodes.

## Scoring

Your solution's `connectivity_metric` is compared against a greedy recursive-bipartitioning baseline:

```
quality = (baseline_metric - your_metric) / baseline_metric * 1,000,000
```

Clamped to +/-10,000,000. **Higher quality is better.** Zero means matching the baseline. Positive means you beat the baseline (lower connectivity). Negative means worse than the baseline.

## Solver Interface

```rust
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    prop: &cudaDeviceProp,
) -> anyhow::Result<Option<Solution>>
```

- Call `save_solution()` whenever you find an improved solution -- only the last call is kept, and the solver may be killed at any time. Write anytime algorithms that save early and improve iteratively.
- Use `challenge.seed` for any randomness to keep results reproducible.
- `module` provides access to compiled CUDA kernels (both the challenge's built-in kernels and your custom kernels from `algorithm/kernels.cu`). Load a kernel with `module.load_function("kernel_name")`.
- `stream` is the CUDA stream for memory operations and kernel launches.
- `prop` contains device properties (e.g. max threads per block, shared memory size).

## GPU Data Access

All challenge data fields prefixed with `d_` are GPU-resident (`CudaSlice`). To read them on the CPU:

```rust
let hyperedge_sizes: Vec<i32> = stream.memcpy_dtov(&challenge.d_hyperedge_sizes)?;
```

To upload CPU data to the GPU:

```rust
let d_partition = stream.memcpy_stod(&my_partition_vec)?;
```

## Custom CUDA Kernels

Agents edit **two files**:

- `algorithm/mod.rs` -- Rust solver logic.
- `algorithm/kernels.cu` -- Custom CUDA C kernels.

Any `extern "C" __global__` function defined in `algorithm/kernels.cu` is compiled and available via `module.load_function("kernel_name")`. The challenge's own kernels (from `hypergraph/kernels.cu`) are also available. Libraries from the `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04` image can be included (e.g. `curand_kernel.h`).

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
        .arg(&challenge.num_nodes)
        .arg(&challenge.d_hyperedge_nodes)
        .arg(&mut d_output)
        .launch(cfg)?;
}
stream.synchronize()?;
```

When launching kernels with multiple blocks, writes must be to non-overlapping memory locations for determinism. Hardcode `LaunchConfig` values (do not exceed 1024 for grid dimensions for compute 3.6 compatibility).

## Exact Method Signatures

### `Challenge` methods

```rust
// Validate solution and return connectivity metric.
// Checks partition assignment validity and balance constraints.
// Returns Err if any constraint is violated.
pub fn evaluate_connectivity_metric(
    &self,
    solution: &Solution,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> Result<u32>
```

No other public helper methods are available on `Challenge`. You have direct access to all fields listed in the Types section.

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `cudarc`, `std::*` (collections, time, sync, etc.).

### Constants

```rust
pub const MAX_THREADS_PER_BLOCK: u32 = 1024;
```
