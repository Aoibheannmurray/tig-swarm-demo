// initial_algorithms/job_scheduling/seeds/greedy.rs
//
// Simple SEED algorithm for the job_scheduling (flexible job-shop) challenge.
// A complete, feasible starting point using active list scheduling: at each
// step, among the operations that are *ready* (their job's previous operation
// is done), schedule the one that can FINISH earliest, on its earliest-finish
// eligible machine. This greedy "earliest completion" dispatch balances load
// across machines far better than scheduling jobs one-at-a-time.
//
// Feasibility is guaranteed by construction:
//   * exactly one schedule entry per job, one (machine, start) per operation,
//     emitted in operation order;
//   * each chosen machine is eligible for that operation;
//   * an operation starts no earlier than the previous op of the same job
//     finishes (job ready time);
//   * a machine never overlaps — each op on a machine starts at/after that
//     machine's running availability time, which then advances.
// A weaker model can refine the dispatch rule (e.g. most-work-remaining,
// bottleneck-aware machine choice, local search) from here.

use super::*;
use anyhow::{anyhow, Result};
use serde_json::{Map, Value};

struct JobState {
    product: usize,
    next_op: usize,
    ready: u32, // time the previous operation of this job completes
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    // Flatten jobs in the exact order the evaluator expects: product by
    // product, `jobs_per_product[p]` jobs each.
    let mut jobs: Vec<JobState> = Vec::with_capacity(challenge.num_jobs);
    let mut job_schedule: Vec<Vec<(usize, u32)>> = Vec::with_capacity(challenge.num_jobs);
    let mut total_ops: usize = 0;
    for (product, &count) in challenge.jobs_per_product.iter().enumerate() {
        let n_ops = challenge.product_processing_times[product].len();
        for _ in 0..count {
            jobs.push(JobState { product, next_op: 0, ready: 0 });
            job_schedule.push(Vec::with_capacity(n_ops));
            total_ops += n_ops;
        }
    }

    let mut machine_avail: std::collections::HashMap<usize, u32> =
        std::collections::HashMap::new();

    let mut scheduled = 0usize;
    while scheduled < total_ops {
        // Find the globally best (earliest-finishing) ready operation.
        let mut best: Option<(usize, usize, u32, u32)> = None; // (job, machine, start, dur)
        let mut best_finish = u32::MAX;
        let mut best_start = u32::MAX;
        for (ji, j) in jobs.iter().enumerate() {
            let ops = &challenge.product_processing_times[j.product];
            if j.next_op >= ops.len() {
                continue;
            }
            let op = &ops[j.next_op];
            for (&machine, &dur) in op.iter() {
                let avail = machine_avail.get(&machine).copied().unwrap_or(0);
                let start = j.ready.max(avail);
                let finish = start.saturating_add(dur);
                // Prefer earliest finish; break ties by earliest start.
                if finish < best_finish || (finish == best_finish && start < best_start) {
                    best_finish = finish;
                    best_start = start;
                    best = Some((ji, machine, start, dur));
                }
            }
        }
        let (ji, machine, start, dur) =
            best.ok_or_else(|| anyhow!("no ready operation could be scheduled"))?;
        job_schedule[ji].push((machine, start));
        machine_avail.insert(machine, start.saturating_add(dur));
        jobs[ji].ready = start.saturating_add(dur);
        jobs[ji].next_op += 1;
        scheduled += 1;
    }

    save_solution(&Solution { job_schedule })?;
    Ok(())
}
