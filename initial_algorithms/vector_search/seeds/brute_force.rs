// initial_algorithms/vector_search/seeds/brute_force.rs
//
// Simple SEED algorithm for the vector_search (GPU) approximate-nearest-
// neighbour challenge.
//
// !!! UNVERIFIED !!!  This environment has no CUDA toolkit / GPU (cudarc fails
// to build without `nvcc`), so this seed could NOT be compiled or run here.
// Validate and fix it on a CUDA box before relying on it.
//
// Strategy: exact brute-force nearest neighbour, computed on the HOST. We copy
// the database and query vectors back from device memory with `memcpy_dtov`
// (the same idiom the challenge's own evaluator uses), then for each query pick
// the database index minimising squared L2 distance. This is feasible by
// construction (one valid index per query, indexes.len() == num_queries) and
// actually returns the optimal neighbours — but it is O(num_queries *
// database_size * dims) on the CPU, so a weaker model's job is to make it FAST
// (move the search onto the GPU, add an index / clustering) rather than to
// rediscover the algorithm.

use crate::vector_search::*;
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
    stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> anyhow::Result<Option<Solution>> {
    let dims = challenge.vector_dims as usize;
    let database_size = challenge.database_size as usize;
    let num_queries = challenge.num_queries as usize;

    // Pull the GPU-resident vectors back to the host (flat row-major: row r of a
    // matrix with `dims` columns starts at r * dims).
    let database: Vec<f32> = stream.memcpy_dtov(&challenge.d_database_vectors)?;
    let queries: Vec<f32> = stream.memcpy_dtov(&challenge.d_query_vectors)?;

    let mut indexes: Vec<usize> = Vec::with_capacity(num_queries);
    for q in 0..num_queries {
        let qoff = q * dims;
        let mut best_idx = 0usize;
        let mut best_dist = f32::INFINITY;
        for d in 0..database_size {
            let doff = d * dims;
            let mut dist = 0.0f32;
            for k in 0..dims {
                let diff = queries[qoff + k] - database[doff + k];
                dist += diff * diff;
            }
            if dist < best_dist {
                best_dist = dist;
                best_idx = d;
            }
        }
        indexes.push(best_idx);
    }

    let solution = Solution { indexes };
    save_solution(&solution)?;
    Ok(Some(solution))
}
