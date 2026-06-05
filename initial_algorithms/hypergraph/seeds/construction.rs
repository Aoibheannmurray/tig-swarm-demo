// initial_algorithms/hypergraph/seeds/construction.rs
//
// SEED algorithm for the hypergraph (GPU) partitioning challenge: a balanced
// round-robin assignment computed on the GPU.
//
// Strategy: launch the `round_robin_partition` kernel (see construction.cu)
// with one thread per node, assigning partition[i] = i % num_parts. This is the
// most size-balanced partition possible (part sizes differ by at most one), so
// it satisfies max_part_size whenever any balanced partition does. The edge-cut
// quality is poor; it exists so a weaker model can refine the assignment (move
// nodes to reduce cut, greedy bipartition, multilevel coarsening) instead of
// bootstrapping a partitioner from scratch.
//
// Verified on an L40 (C3): compiles, runs, and produces a feasible, balanced
// partition (score −658834 on n_h_edges=10000 — poor cut by design).

use crate::hypergraph::*;
use anyhow::Result;
use cudarc::{
    driver::{CudaModule, CudaStream, LaunchConfig, PushKernelArg},
    runtime::sys::cudaDeviceProp,
};
use serde_json::{Map, Value};
use std::sync::Arc;

const THREADS_PER_BLOCK: u32 = 256;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> Result<()> {
    let num_nodes = challenge.num_nodes as usize;
    let num_nodes_i = challenge.num_nodes as i32;
    let num_parts_i = challenge.num_parts as i32;

    // First device op — binds the CUDA context to this (spawned) solver thread.
    let mut d_partition = stream.alloc_zeros::<i32>(num_nodes)?;

    let kernel = module.load_function("round_robin_partition")?;
    let cfg = LaunchConfig {
        grid_dim: (
            (challenge.num_nodes + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK,
            1,
            1,
        ),
        block_dim: (THREADS_PER_BLOCK, 1, 1),
        shared_mem_bytes: 0,
    };
    unsafe {
        stream
            .launch_builder(&kernel)
            .arg(&mut d_partition)
            .arg(&num_nodes_i)
            .arg(&num_parts_i)
            .launch(cfg)?;
    }
    stream.synchronize()?;

    let partition: Vec<i32> = stream.memcpy_dtov(&d_partition)?;
    let partition_u32: Vec<u32> = partition.iter().map(|&x| x as u32).collect();

    save_solution(&Solution { partition: partition_u32 })?;
    Ok(())
}
