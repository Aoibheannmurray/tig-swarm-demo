// Hypergraph partitioning: GPU-FIRST pipeline.
//
// Rationale: fuel is counted on CPU instructions, so the previous CPU-side
// multilevel partitioner burned its entire budget on coarsening/refinement.
// This version pushes all heavy computation onto the GPU (fuel-cheap) and
// leaves the CPU only trivial O(n) bookkeeping per round:
//
//   1. Construction (best-of-two): (a) the challenge's own `greedy_bipartition`
//      kernel from the root - 6 levels of greedy recursive bisection -> 64
//      balanced parts; (b) seeded cluster growth - 64 highest-degree nodes
//      seed one part each, then GPU rounds let every unassigned node bid for
//      the part its incident hyperedges already touch most, admitting per
//      part the highest-affinity cohort that provably fits capacity (same
//      histogram/threshold machinery as refinement). Both are evaluated on
//      the GPU and refinement starts from the better one.
//   2. Refinement: FULLY GPU-RESIDENT greedy parallel moves. Per round,
//      kernels compute every node's best-gain move (restricted to one
//      direction, alternating q>p / q<p between rounds to kill pairwise
//      oscillation), build per-part gain histograms, derive a per-part gain
//      acceptance threshold that provably fits the remaining capacity, and
//      apply all accepted moves in parallel on the GPU. The CPU per round
//      only launches kernels and reads two scalars (moved count + exact
//      metric), so fuel per round is near-constant instead of O(n log n)
//      host sorting. The partition is downloaded only when the metric
//      improves, to save it.
//   3. Stop: two consecutive rounds with zero applied moves (both
//      directions dry), or PATIENCE rounds without improving the best
//      metric (search-state based, no wall clock).
//
// Determinism: every custom kernel writes only thread-private output slots
// or uses commutative integer atomics (sums are order-independent);
// acceptance is all-or-nothing per (part, gain-level), so no ticket races.
// num_parts is always 64, so a hyperedge's touched-parts set fits one u64.

use anyhow::Result;
use cudarc::{
    driver::{CudaModule, CudaSlice, CudaStream, LaunchConfig, PushKernelArg},
    runtime::sys::cudaDeviceProp,
};
#[allow(unused_imports)]
use rand::{rngs::SmallRng, seq::SliceRandom, SeedableRng};
use serde_json::{Map, Value};
#[allow(unused_imports)]
use std::collections::HashMap;
use std::sync::Arc;
use tig_challenges::hypergraph::*;

#[allow(dead_code)]
const THREADS_PER_BLOCK: u32 = 256;

pub fn help() {
    println!("GPU greedy recursive bisection + GPU-parallel FM/label-propagation refinement.");
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    _prop: &cudaDeviceProp,
) -> Result<()> {
    const K: usize = 64;
    const DEPTH: i32 = 6; // 2^6 = 64 parts
    const MAX_ROUNDS: usize = 2000;
    const PATIENCE: usize = 24; // rounds alternate direction; 24 = 12 full sweeps
    const GROW_MAX_ROUNDS: usize = 512;

    let n = challenge.num_nodes as usize;
    let m = challenge.num_hyperedges as usize;
    let k = challenge.num_parts as usize;
    let max_size = challenge.max_part_size as i64;

    if n == 0 || k == 0 {
        save_solution(&Solution { partition: vec![] })?;
        return Ok(());
    }
    // The u64 part-mask machinery requires exactly 64 parts (spec guarantee);
    // anything else gets a plain balanced round-robin and we're done.
    if k != K || n < K {
        let partition: Vec<u32> = (0..n as u32).map(|j| j % (k as u32)).collect();
        save_solution(&Solution { partition })?;
        return Ok(());
    }

    let save = |part: &[u8]| -> Result<()> {
        save_solution(&Solution {
            partition: part.iter().map(|&p| p as u32).collect(),
        })
    };

    // Anytime guarantee: feasible round-robin before any GPU work.
    let mut part: Vec<u8> = (0..n).map(|j| (j % K) as u8).collect();
    save(&part)?;
    if m == 0 {
        return Ok(());
    }

    let greedy_bipartition = module.load_function("greedy_bipartition")?;
    let finalize_bipartition = module.load_function("finalize_bipartition")?;
    let calc_metric = module.load_function("calc_connectivity_metric")?;
    let edge_part_info = module.load_function("edge_part_info")?;
    let node_best_move = module.load_function("node_best_move")?;
    let gain_histogram = module.load_function("gain_histogram")?;
    let compute_accept = module.load_function("compute_accept")?;
    let apply_moves = module.load_function("apply_moves")?;
    let node_affinity = module.load_function("node_affinity")?;
    let assign_histogram = module.load_function("assign_histogram")?;
    let apply_assign = module.load_function("apply_assign")?;

    let n_i32 = n as i32;
    let m_i32 = m as i32;
    let block = 256u32;
    let grid_m = ((m as u32 + block - 1) / block).clamp(1, 1024);
    let grid_n = ((n as u32 + block - 1) / block).clamp(1, 1024);
    let cfg_m = LaunchConfig {
        grid_dim: (grid_m, 1, 1),
        block_dim: (block, 1, 1),
        shared_mem_bytes: 0,
    };
    let cfg_n = LaunchConfig {
        grid_dim: (grid_n, 1, 1),
        block_dim: (block, 1, 1),
        shared_mem_bytes: 0,
    };

    // ---- Phase 1: greedy recursive bisection on the GPU (root -> 64 leaves).
    // Same kernel the generator uses to build the baseline partition; we run
    // it from level 0 so the top split is also connectivity-greedy.
    let node_degrees: Vec<i32> = stream.memcpy_dtov(&challenge.d_node_degrees)?;
    let mut order: Vec<i32> = (0..n_i32).collect();
    order.sort_unstable_by(|&a, &b| {
        node_degrees[b as usize]
            .cmp(&node_degrees[a as usize])
            .then(a.cmp(&b))
    });
    let d_sorted_nodes = stream.memcpy_stod(&order)?;

    let mut d_partition = stream.alloc_zeros::<i32>(n)?;
    let mut d_curr = stream.alloc_zeros::<i32>(n)?;
    let words = (m + 63) / 64;

    for level in 0..DEPTH {
        let parts_this_level = 1usize << level;
        let mut d_left = stream.alloc_zeros::<u64>(words * parts_this_level)?;
        let mut d_right = stream.alloc_zeros::<u64>(words * parts_this_level)?;
        stream.memcpy_dtod(&d_partition, &mut d_curr)?;
        unsafe {
            stream
                .launch_builder(&greedy_bipartition)
                .arg(&level)
                .arg(&n_i32)
                .arg(&m_i32)
                .arg(&challenge.d_node_hyperedges)
                .arg(&challenge.d_node_offsets)
                .arg(&d_sorted_nodes)
                .arg(&challenge.d_node_degrees)
                .arg(&d_curr)
                .arg(&mut d_partition)
                .arg(&mut d_left)
                .arg(&mut d_right)
                .launch(LaunchConfig {
                    grid_dim: (parts_this_level as u32, 1, 1),
                    block_dim: (1024, 1, 1),
                    shared_mem_bytes: 400,
                })?;
        }
        stream.synchronize()?;
    }
    let k_i32 = K as i32;
    unsafe {
        stream
            .launch_builder(&finalize_bipartition)
            .arg(&n_i32)
            .arg(&k_i32)
            .arg(&mut d_partition)
            .launch(LaunchConfig {
                grid_dim: (1, 1, 1),
                block_dim: (1024, 1, 1),
                shared_mem_bytes: 0,
            })?;
    }
    stream.synchronize()?;

    // Repair a host-side partition in place - fix any out-of-range, oversize,
    // or empty parts so it is strictly feasible - and return the part sizes.
    let repair = |gp: &mut Vec<i32>| -> [i64; K] {
        for (j, v) in gp.iter_mut().enumerate() {
            if *v < 0 || *v >= K as i32 {
                *v = (j % K) as i32;
            }
        }
        let mut sizes = [0i64; K];
        for &p in gp.iter() {
            sizes[p as usize] += 1;
        }
        for v in gp.iter_mut() {
            let p = *v as usize;
            if sizes[p] > max_size {
                let q = (0..K).min_by_key(|&q| sizes[q]).unwrap();
                sizes[p] -= 1;
                sizes[q] += 1;
                *v = q as i32;
            }
        }
        for q in 0..K {
            while sizes[q] == 0 {
                let donor = (0..K).max_by_key(|&d| sizes[d]).unwrap();
                let j = gp.iter().position(|&p| p as usize == donor).unwrap();
                gp[j] = q as i32;
                sizes[donor] -= 1;
                sizes[q] += 1;
            }
        }
        sizes
    };

    let eval = |d_part: &CudaSlice<i32>| -> Result<u32> {
        let mut d_metric = stream.alloc_zeros::<u32>(1)?;
        unsafe {
            stream
                .launch_builder(&calc_metric)
                .arg(&m_i32)
                .arg(&challenge.d_hyperedge_offsets)
                .arg(&challenge.d_hyperedge_nodes)
                .arg(d_part)
                .arg(&mut d_metric)
                .launch(cfg_m)?;
        }
        stream.synchronize()?;
        Ok(stream.memcpy_dtov(&d_metric)?[0])
    };

    let mut gp_bis: Vec<i32> = stream.memcpy_dtov(&d_partition)?;
    let sizes_bis = repair(&mut gp_bis);
    stream.memcpy_htod(&gp_bis, &mut d_partition)?;
    let metric_bis = eval(&d_partition)?;
    part = gp_bis.iter().map(|&p| p as u8).collect();
    save(&part)?;

    // Scratch shared by the growth construction and the refinement rounds.
    let mut d_edge_mask = stream.alloc_zeros::<u64>(m)?;
    let mut d_edge_solo = stream.alloc_zeros::<u64>(m)?;
    let mut d_gain = stream.alloc_zeros::<i32>(n)?;
    let mut d_target = stream.alloc_zeros::<i32>(n)?;
    let max_size_i32 = challenge.max_part_size as i32;
    let cfg_parts = LaunchConfig {
        grid_dim: (1, 1, 1),
        block_dim: (K as u32, 1, 1),
        shared_mem_bytes: 0,
    };

    // ---- Phase 1b: alternative construction - seeded cluster growth.
    // The 64 highest-degree nodes seed one part each; every round, each
    // unassigned node bids for the part its incident hyperedges already touch
    // most, and per part the highest-affinity cohort that provably fits
    // capacity is admitted (all-or-nothing per affinity level, so the result
    // is deterministic). CPU work per round is one scalar readback.
    let mut host_grow: Vec<i32> = vec![-1; n];
    for (i, &nd) in order.iter().take(K).enumerate() {
        host_grow[nd as usize] = i as i32;
    }
    let mut d_grow = stream.memcpy_stod(&host_grow)?;
    let ones = vec![1i32; K];
    let mut d_sizes_grow = stream.memcpy_stod(&ones)?;
    let mut assigned_total = K;
    for _ in 0..GROW_MAX_ROUNDS {
        if assigned_total >= n {
            break;
        }
        let mut d_hist = stream.alloc_zeros::<u32>(K * 32)?;
        let d_out_count = stream.alloc_zeros::<u32>(K)?; // stays zero: growth has no out-moves
        let mut d_thresh = stream.alloc_zeros::<i32>(K)?;
        let mut d_out_block = stream.alloc_zeros::<i32>(K)?;
        let mut d_assigned = stream.alloc_zeros::<u32>(1)?;

        unsafe {
            stream
                .launch_builder(&edge_part_info)
                .arg(&m_i32)
                .arg(&challenge.d_hyperedge_offsets)
                .arg(&challenge.d_hyperedge_nodes)
                .arg(&d_grow)
                .arg(&mut d_edge_mask)
                .arg(&mut d_edge_solo)
                .launch(cfg_m)?;
        }
        unsafe {
            stream
                .launch_builder(&node_affinity)
                .arg(&n_i32)
                .arg(&challenge.d_node_offsets)
                .arg(&challenge.d_node_hyperedges)
                .arg(&d_grow)
                .arg(&d_edge_mask)
                .arg(&mut d_gain)
                .arg(&mut d_target)
                .launch(cfg_n)?;
        }
        unsafe {
            stream
                .launch_builder(&assign_histogram)
                .arg(&n_i32)
                .arg(&d_gain)
                .arg(&d_target)
                .arg(&mut d_hist)
                .launch(cfg_n)?;
        }
        unsafe {
            stream
                .launch_builder(&compute_accept)
                .arg(&max_size_i32)
                .arg(&d_sizes_grow)
                .arg(&d_hist)
                .arg(&d_out_count)
                .arg(&mut d_thresh)
                .arg(&mut d_out_block)
                .launch(cfg_parts)?;
        }
        unsafe {
            stream
                .launch_builder(&apply_assign)
                .arg(&n_i32)
                .arg(&mut d_grow)
                .arg(&d_gain)
                .arg(&d_target)
                .arg(&d_thresh)
                .arg(&mut d_sizes_grow)
                .arg(&mut d_assigned)
                .launch(cfg_n)?;
        }
        stream.synchronize()?;

        let a = stream.memcpy_dtov(&d_assigned)?[0] as usize;
        if a == 0 {
            // Capacity gating starved every remaining candidate; leftovers
            // are placed host-side below.
            break;
        }
        assigned_total += a;
    }
    let mut gp_grow: Vec<i32> = stream.memcpy_dtov(&d_grow)?;
    // Leftover unassigned nodes go to the least-loaded part before repair.
    let mut sg = [0i64; K];
    for &p in &gp_grow {
        if p >= 0 && p < K as i32 {
            sg[p as usize] += 1;
        }
    }
    for v in gp_grow.iter_mut() {
        if *v < 0 || *v >= K as i32 {
            let q = (0..K).min_by_key(|&q| sg[q]).unwrap();
            sg[q] += 1;
            *v = q as i32;
        }
    }
    let sizes_grow = repair(&mut gp_grow);
    stream.memcpy_htod(&gp_grow, &mut d_grow)?;
    let metric_grow = eval(&d_grow)?;

    // Refinement starts from whichever construction scored better.
    let (sizes, mut best_metric) = if metric_grow < metric_bis {
        stream.memcpy_htod(&gp_grow, &mut d_partition)?;
        part = gp_grow.iter().map(|&p| p as u8).collect();
        save(&part)?;
        (sizes_grow, metric_grow)
    } else {
        (sizes_bis, metric_bis)
    };

    // ---- Phase 2: fully GPU-resident refinement rounds.
    let sizes_i32: Vec<i32> = sizes.iter().map(|&s| s as i32).collect();
    let mut d_sizes = stream.memcpy_stod(&sizes_i32)?;
    let mut since_improve = 0usize;
    let mut dry_rounds = 0usize;

    for round in 0..MAX_ROUNDS {
        let dir: i32 = (round & 1) as i32;
        // Small per-round scratch buffers; kernels require them zeroed.
        let mut d_hist = stream.alloc_zeros::<u32>(K * 32)?;
        let mut d_out_count = stream.alloc_zeros::<u32>(K)?;
        let mut d_thresh = stream.alloc_zeros::<i32>(K)?;
        let mut d_out_block = stream.alloc_zeros::<i32>(K)?;
        let mut d_moved = stream.alloc_zeros::<u32>(1)?;

        unsafe {
            stream
                .launch_builder(&edge_part_info)
                .arg(&m_i32)
                .arg(&challenge.d_hyperedge_offsets)
                .arg(&challenge.d_hyperedge_nodes)
                .arg(&d_partition)
                .arg(&mut d_edge_mask)
                .arg(&mut d_edge_solo)
                .launch(cfg_m)?;
        }
        unsafe {
            stream
                .launch_builder(&node_best_move)
                .arg(&n_i32)
                .arg(&dir)
                .arg(&challenge.d_node_offsets)
                .arg(&challenge.d_node_hyperedges)
                .arg(&d_partition)
                .arg(&d_edge_mask)
                .arg(&d_edge_solo)
                .arg(&mut d_gain)
                .arg(&mut d_target)
                .launch(cfg_n)?;
        }
        unsafe {
            stream
                .launch_builder(&gain_histogram)
                .arg(&n_i32)
                .arg(&d_partition)
                .arg(&d_gain)
                .arg(&d_target)
                .arg(&mut d_hist)
                .arg(&mut d_out_count)
                .launch(cfg_n)?;
        }
        unsafe {
            stream
                .launch_builder(&compute_accept)
                .arg(&max_size_i32)
                .arg(&d_sizes)
                .arg(&d_hist)
                .arg(&d_out_count)
                .arg(&mut d_thresh)
                .arg(&mut d_out_block)
                .launch(cfg_parts)?;
        }
        unsafe {
            stream
                .launch_builder(&apply_moves)
                .arg(&n_i32)
                .arg(&mut d_partition)
                .arg(&d_gain)
                .arg(&d_target)
                .arg(&d_thresh)
                .arg(&d_out_block)
                .arg(&mut d_sizes)
                .arg(&mut d_moved)
                .launch(cfg_n)?;
        }
        stream.synchronize()?;

        let moved = stream.memcpy_dtov(&d_moved)?[0];
        if moved == 0 {
            dry_rounds += 1;
            // Both directions must run dry before we call it converged.
            if dry_rounds >= 2 {
                break;
            }
            continue;
        }
        dry_rounds = 0;

        let metric = eval(&d_partition)?;
        if metric < best_metric {
            best_metric = metric;
            let cur: Vec<i32> = stream.memcpy_dtov(&d_partition)?;
            part = cur.iter().map(|&p| p as u8).collect();
            save(&part)?;
            since_improve = 0;
        } else {
            since_improve += 1;
            if since_improve >= PATIENCE {
                break;
            }
        }
    }

    Ok(())
}
