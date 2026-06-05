// initial_algorithms/vector_search/seeds/brute_force.rs
//
// SEED algorithm for the vector_search (GPU) approximate-nearest-neighbour
// challenge: exact brute-force search run on the GPU.
//
// Strategy: launch the `nearest_neighbor_search` kernel (see brute_force.cu)
// with one thread per query; each thread scans the whole database and returns
// the index minimising squared L2 distance. This is feasible by construction
// (one valid index per query, indexes.len() == num_queries) and returns the
// exact optimal neighbours. It is O(num_queries * database_size * dims) of GPU
// work, so a weaker model's job is to make it FAST (tiling / shared memory,
// early-exit pruning on the running min, an index / clustering) rather than to
// rediscover the algorithm.
//
// An earlier version of this seed did the scan on the HOST and copied the
// vectors back with `memcpy_dtov` as its first device op — which crashed with
// CUDA_ERROR_INVALID_CONTEXT because the harness runs solvers on a spawned
// thread and `memcpy_dtoh` does not bind the context. Doing the work in a kernel
// avoids that: the first device op below is `alloc_zeros`, which binds the
// context to this thread.
//
// Verified on an L40 (C3): compiles, runs, feasible, and beats the baseline
// (score +44520 on n_queries=10).

use crate::vector_search::*;
use anyhow::Result;
use cudarc::{
    driver::{CudaModule, CudaStream, LaunchConfig, PushKernelArg},
    runtime::sys::cudaDeviceProp,
};
use serde_json::{Map, Value};
use std::sync::Arc;

const THREADS_PER_BLOCK: u32 = 128;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> Result<()> {
    let num_queries = challenge.num_queries as usize;
    let num_queries_i = challenge.num_queries as i32;
    let database_size_i = challenge.database_size as i32;
    let vector_dims_i = challenge.vector_dims as i32;

    // First device op — binds the CUDA context to this (spawned) solver thread.
    let mut d_results = stream.alloc_zeros::<i32>(num_queries)?;

    let kernel = module.load_function("nearest_neighbor_search")?;
    let cfg = LaunchConfig {
        grid_dim: (
            (challenge.num_queries + THREADS_PER_BLOCK - 1) / THREADS_PER_BLOCK,
            1,
            1,
        ),
        block_dim: (THREADS_PER_BLOCK, 1, 1),
        shared_mem_bytes: 0,
    };
    unsafe {
        stream
            .launch_builder(&kernel)
            .arg(&challenge.d_query_vectors)
            .arg(&challenge.d_database_vectors)
            .arg(&mut d_results)
            .arg(&num_queries_i)
            .arg(&database_size_i)
            .arg(&vector_dims_i)
            .launch(cfg)?;
    }
    stream.synchronize()?;

    let result_indices: Vec<i32> = stream.memcpy_dtov(&d_results)?;
    let indexes: Vec<usize> = result_indices
        .iter()
        .map(|&idx| {
            if idx < 0 || idx >= challenge.database_size as i32 {
                0
            } else {
                idx as usize
            }
        })
        .collect();

    save_solution(&Solution { indexes })?;
    Ok(())
}
