// initial_algorithms/hypergraph/seeds/construction.rs
//
// Simple SEED algorithm for the hypergraph (GPU) partitioning challenge.
//
// !!! UNVERIFIED !!!  This environment has no CUDA toolkit / GPU (cudarc fails
// to build without `nvcc`), so this seed could NOT be compiled or run here.
// Validate and fix it on a CUDA box before relying on it. The logic below is
// deliberately host-only and uses no kernels, which minimises the surface that
// could be wrong, but the cudarc type/imports still need a real build to confirm.
//
// Strategy: a balanced round-robin assignment of nodes to partitions
// (partition[i] = i % num_parts). This needs only the two host-side scalars
// num_nodes and num_parts — not the GPU-resident hyperedge structure — and is
// the most size-balanced partition possible, so it satisfies max_part_size
// whenever any balanced partition does. The edge-cut quality is poor; it exists
// so a weaker model can refine the assignment (move nodes to reduce cut,
// multilevel coarsening, GPU greedy bipartition) instead of bootstrapping.

use crate::hypergraph::*;
use anyhow::Result;
use cudarc::{
    driver::{CudaModule, CudaStream},
    runtime::sys::cudaDeviceProp,
};
use serde_json::{Map, Value};
use std::sync::Arc;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
    _module: Arc<CudaModule>,
    _stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> anyhow::Result<Option<Solution>> {
    let num_nodes = challenge.num_nodes as usize;
    let num_parts = (challenge.num_parts as usize).max(1);

    // Balanced round-robin assignment: part sizes differ by at most one.
    let partition: Vec<u32> = (0..num_nodes)
        .map(|i| (i % num_parts) as u32)
        .collect();

    let solution = Solution { partition };
    save_solution(&solution)?;
    Ok(Some(solution))
}
