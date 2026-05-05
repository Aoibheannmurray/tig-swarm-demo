use super::*;
use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};
use anyhow::{anyhow, Result};
use rand::seq::SliceRandom;
use rand::{rngs::SmallRng, Rng, SeedableRng};
use serde_json::{Map, Value};
use std::cmp::Ordering;
use std::collections::HashMap;

const DEFAULT_EFFORT: usize = 10;
const WORK_MIN_WEIGHT: f64 = 0.3;

fn average_processing_time(operation: &HashMap<usize, u32>) -> f64 {
    if operation.is_empty() {
        return 0.0;
    }
    let sum: u32 = operation.values().sum();
    sum as f64 / operation.len() as f64
}

fn min_processing_time(operation: &HashMap<usize, u32>) -> f64 {
    operation.values().copied().min().unwrap_or(0) as f64
}

fn earliest_end_time(
    time: u32,
    machine_available_time: &[u32],
    operation: &HashMap<usize, u32>,
) -> u32 {
    let mut earliest_end = u32::MAX;
    for (&machine_id, &proc_time) in operation.iter() {
        let start = time.max(machine_available_time[machine_id]);
        let end = start + proc_time;
        if end < earliest_end {
            earliest_end = end;
        }
    }
    earliest_end
}

#[derive(Clone, Copy)]
enum DispatchRule {
    MostWorkRemaining,
    MostOpsRemaining,
    LeastFlexibility,
    ShortestProcTime,
    LongestProcTime,
}

#[derive(Clone, Copy)]
struct Candidate {
    job: usize,
    priority: f64,
    machine_end: u32,
    proc_time: u32,
    flexibility: usize,
}

struct ScheduleResult {
    job_schedule: Vec<Vec<(usize, u32)>>,
    makespan: u32,
}

struct RestartResult {
    makespan: u32,
    rule: DispatchRule,
    random_top_k: usize,
    seed: u64,
}

fn better_candidate(candidate: &Candidate, best: &Candidate, eps: f64) -> bool {
    if candidate.priority > best.priority + eps {
        return true;
    }
    if (candidate.priority - best.priority).abs() <= eps {
        if candidate.machine_end < best.machine_end {
            return true;
        }
        if candidate.machine_end == best.machine_end {
            if candidate.proc_time < best.proc_time {
                return true;
            }
            if candidate.proc_time == best.proc_time {
                if candidate.flexibility < best.flexibility {
                    return true;
                }
                if candidate.flexibility == best.flexibility && candidate.job < best.job {
                    return true;
                }
            }
        }
    }
    false
}

fn run_dispatch_rule(
    challenge: &Challenge,
    job_products: &[usize],
    product_work_times: &[Vec<f64>],
    job_ops_len: &[usize],
    job_total_work: &[f64],
    rule: DispatchRule,
    random_top_k: Option<usize>,
    rng: Option<&mut SmallRng>,
) -> Result<ScheduleResult> {
    let num_jobs = challenge.num_jobs;
    let num_machines = challenge.num_machines;

    let mut job_next_op_idx = vec![0usize; num_jobs];
    let mut job_ready_time = vec![0u32; num_jobs];
    let mut machine_available_time = vec![0u32; num_machines];
    let mut job_schedule = job_ops_len
        .iter()
        .map(|&ops_len| Vec::with_capacity(ops_len))
        .collect::<Vec<_>>();
    let mut job_remaining_work = job_total_work.to_vec();

    let mut remaining_ops = job_ops_len.iter().sum::<usize>();
    let mut time = 0u32;
    let eps = 1e-9_f64;
    let random_top_k = random_top_k.unwrap_or(0);
    let mut rng = rng;
    let use_random = random_top_k > 1 && rng.is_some();

    while remaining_ops > 0 {
        let mut available_machines = (0..num_machines)
            .filter(|&m| machine_available_time[m] <= time)
            .collect::<Vec<usize>>();
        available_machines.sort_unstable();
        if use_random {
            available_machines.shuffle(rng.as_mut().unwrap());
        }

        let mut scheduled_any = false;
        for &machine in available_machines.iter() {
            let mut best_candidate: Option<Candidate> = None;

            if use_random {
                let mut candidates: Vec<Candidate> = Vec::new();

                for job in 0..num_jobs {
                    if job_next_op_idx[job] >= job_ops_len[job] {
                        continue;
                    }
                    if job_ready_time[job] > time {
                        continue;
                    }

                    let product = job_products[job];
                    let op_idx = job_next_op_idx[job];
                    let op_times = &challenge.product_processing_times[product][op_idx];
                    let proc_time = match op_times.get(&machine) {
                        Some(&value) => value,
                        None => continue,
                    };

                    let earliest_end = earliest_end_time(time, &machine_available_time, op_times);
                    let machine_end = time.max(machine_available_time[machine]) + proc_time;
                    if machine_end != earliest_end {
                        continue;
                    }

                    let flexibility = op_times.len();
                    let priority = match rule {
                        DispatchRule::MostWorkRemaining => job_remaining_work[job],
                        DispatchRule::MostOpsRemaining => {
                            (job_ops_len[job] - job_next_op_idx[job]) as f64
                        }
                        DispatchRule::LeastFlexibility => -(flexibility as f64),
                        DispatchRule::ShortestProcTime => -(proc_time as f64),
                        DispatchRule::LongestProcTime => proc_time as f64,
                    };

                    candidates.push(Candidate {
                        job,
                        priority,
                        machine_end,
                        proc_time,
                        flexibility,
                    });
                }

                if !candidates.is_empty() {
                    candidates.sort_by(|a, b| {
                        let ord = b
                            .priority
                            .partial_cmp(&a.priority)
                            .unwrap_or(Ordering::Equal);
                        if ord != Ordering::Equal {
                            return ord;
                        }
                        let ord = a.machine_end.cmp(&b.machine_end);
                        if ord != Ordering::Equal {
                            return ord;
                        }
                        let ord = a.proc_time.cmp(&b.proc_time);
                        if ord != Ordering::Equal {
                            return ord;
                        }
                        let ord = a.flexibility.cmp(&b.flexibility);
                        if ord != Ordering::Equal {
                            return ord;
                        }
                        a.job.cmp(&b.job)
                    });
                    let k = random_top_k.min(candidates.len());
                    let pick = rng.as_mut().unwrap().gen_range(0..k);
                    best_candidate = Some(candidates[pick]);
                }
            } else {
                for job in 0..num_jobs {
                    if job_next_op_idx[job] >= job_ops_len[job] {
                        continue;
                    }
                    if job_ready_time[job] > time {
                        continue;
                    }

                    let product = job_products[job];
                    let op_idx = job_next_op_idx[job];
                    let op_times = &challenge.product_processing_times[product][op_idx];
                    let proc_time = match op_times.get(&machine) {
                        Some(&value) => value,
                        None => continue,
                    };

                    let earliest_end = earliest_end_time(time, &machine_available_time, op_times);
                    let machine_end = time.max(machine_available_time[machine]) + proc_time;
                    if machine_end != earliest_end {
                        continue;
                    }

                    let flexibility = op_times.len();
                    let priority = match rule {
                        DispatchRule::MostWorkRemaining => job_remaining_work[job],
                        DispatchRule::MostOpsRemaining => {
                            (job_ops_len[job] - job_next_op_idx[job]) as f64
                        }
                        DispatchRule::LeastFlexibility => -(flexibility as f64),
                        DispatchRule::ShortestProcTime => -(proc_time as f64),
                        DispatchRule::LongestProcTime => proc_time as f64,
                    };

                    let candidate = Candidate {
                        job,
                        priority,
                        machine_end,
                        proc_time,
                        flexibility,
                    };

                    if best_candidate
                        .as_ref()
                        .map_or(true, |best| better_candidate(&candidate, best, eps))
                    {
                        best_candidate = Some(candidate);
                    }
                }
            }

            if let Some(candidate) = best_candidate {
                let job = candidate.job;
                let product = job_products[job];
                let op_idx = job_next_op_idx[job];
                let op_times = &challenge.product_processing_times[product][op_idx];
                let proc_time = op_times[&machine];

                let start_time = time.max(machine_available_time[machine]);
                let end_time = start_time + proc_time;

                job_schedule[job].push((machine, start_time));
                job_next_op_idx[job] += 1;
                job_ready_time[job] = end_time;
                machine_available_time[machine] = end_time;
                job_remaining_work[job] -= product_work_times[product][op_idx];
                if job_remaining_work[job] < 0.0 {
                    job_remaining_work[job] = 0.0;
                }

                remaining_ops -= 1;
                scheduled_any = true;
            }
        }

        if remaining_ops == 0 {
            break;
        }

        // Compute next event time (either machine becoming available or job becoming ready)
        let mut next_time: Option<u32> = None;
        for &t in machine_available_time.iter() {
            if t > time {
                next_time = Some(next_time.map_or(t, |best| best.min(t)));
            }
        }
        for job in 0..num_jobs {
            if job_next_op_idx[job] < job_ops_len[job] && job_ready_time[job] > time {
                let t = job_ready_time[job];
                next_time = Some(next_time.map_or(t, |best| best.min(t)));
            }
        }

        // Advance time to next event
        time = next_time.ok_or_else(|| {
            if scheduled_any {
                anyhow!("No next event time found while operations remain unscheduled")
            } else {
                anyhow!("No schedulable operations remain; dispatching rules stalled")
            }
        })?;
    }

    let makespan = job_ready_time.iter().copied().max().unwrap_or(0);
    Ok(ScheduleResult {
        job_schedule,
        makespan,
    })
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    solve_challenge_with_effort(challenge, save_solution, DEFAULT_EFFORT)
}

pub fn solve_challenge_with_effort(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    effort: usize,
) -> Result<()> {
    let (random_restarts, top_k) = if effort == 0 {
        (10usize, 0usize)
    } else if effort == 1 {
        (200usize, 2usize)
    } else {
        let random_restarts = 200usize.saturating_add(50usize.saturating_mul(effort));
        let top_k = 2usize.saturating_mul(effort);
        (random_restarts, top_k)
    };
    let local_search_tries = 1usize.saturating_add(3usize.saturating_mul(effort));
    solve_challenge_with_params(
        challenge,
        save_solution,
        random_restarts,
        top_k,
        local_search_tries,
    )
}

fn solve_challenge_with_params(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    random_restarts: usize,
    top_k: usize,
    local_search_tries: usize,
) -> Result<()> {
    let save_best = |best: &ScheduleResult| -> Result<()> {
        save_solution(&Solution {
            job_schedule: best.job_schedule.clone(),
        })
    };
    let num_jobs = challenge.num_jobs;

    let mut job_products = Vec::with_capacity(num_jobs);
    for (product, count) in challenge.jobs_per_product.iter().enumerate() {
        for _ in 0..*count {
            job_products.push(product);
        }
    }
    if job_products.len() != num_jobs {
        return Err(anyhow!(
            "Job count mismatch. Expected {}, got {}",
            num_jobs,
            job_products.len()
        ));
    }

    let mut product_work_times = Vec::with_capacity(challenge.product_processing_times.len());
    for product_ops in challenge.product_processing_times.iter() {
        let mut work_ops = Vec::with_capacity(product_ops.len());
        for op in product_ops.iter() {
            let avg = average_processing_time(op);
            let min = min_processing_time(op);
            let work = avg * (1.0 - WORK_MIN_WEIGHT) + min * WORK_MIN_WEIGHT;
            work_ops.push(work);
        }
        product_work_times.push(work_ops);
    }

    let mut job_ops_len = Vec::with_capacity(num_jobs);
    let mut job_total_work: Vec<f64> = Vec::with_capacity(num_jobs);
    for &product in job_products.iter() {
        let work_ops = &product_work_times[product];
        job_ops_len.push(work_ops.len());
        job_total_work.push(work_ops.iter().sum());
    }

    let rules = [
        DispatchRule::MostWorkRemaining,
        DispatchRule::MostOpsRemaining,
        DispatchRule::LeastFlexibility,
        DispatchRule::ShortestProcTime,
        DispatchRule::LongestProcTime,
    ];

    let mut best_result: Option<ScheduleResult> = None;
    for rule in rules.iter().copied() {
        let result = run_dispatch_rule(
            challenge,
            &job_products,
            &product_work_times,
            &job_ops_len,
            &job_total_work,
            rule,
            None,
            None,
        )?;
        let is_better = best_result
            .as_ref()
            .map_or(true, |best| result.makespan < best.makespan);
        if is_better {
            best_result = Some(result);
        }
    }

    let mut best_result = best_result.ok_or_else(|| anyhow!("No valid schedule produced"))?;
    save_best(&best_result)?;

    let mut top_restarts: Vec<RestartResult> = Vec::new();

    if random_restarts > 0 {
        let mut rng = SmallRng::from_seed(challenge.seed);
        for _ in 1..=random_restarts {
            let seed = rng.r#gen::<u64>();
            let rule = rules[rng.gen_range(0..rules.len())];
            let random_top_k = rng.gen_range(2..=5);
            let mut local_rng = SmallRng::seed_from_u64(seed);
            let result = run_dispatch_rule(
                challenge,
                &job_products,
                &product_work_times,
                &job_ops_len,
                &job_total_work,
                rule,
                Some(random_top_k),
                Some(&mut local_rng),
            )?;
            let makespan = result.makespan;
            let is_better = makespan < best_result.makespan;
            if is_better {
                best_result = result;
                save_best(&best_result)?;
            }

            if top_k > 0 {
                top_restarts.push(RestartResult {
                    makespan,
                    rule,
                    random_top_k,
                    seed,
                });
                top_restarts.sort_by(|a, b| a.makespan.cmp(&b.makespan));
                if top_restarts.len() > top_k {
                    top_restarts.pop();
                }
            }
        }
    }

    if !top_restarts.is_empty() {
        for restart in top_restarts.iter() {
            for attempt in 0..local_search_tries {
                let local_seed = restart.seed.wrapping_add(attempt as u64 + 1);
                let mut local_rng = SmallRng::seed_from_u64(local_seed);
                let local_k = match attempt % 3 {
                    0 => restart.random_top_k,
                    1 => restart.random_top_k.saturating_sub(1),
                    _ => restart.random_top_k.saturating_add(1),
                }
                .max(2);
                let result = run_dispatch_rule(
                    challenge,
                    &job_products,
                    &product_work_times,
                    &job_ops_len,
                    &job_total_work,
                    restart.rule,
                    Some(local_k),
                    Some(&mut local_rng),
                )?;
                if result.makespan < best_result.makespan {
                    best_result = result;
                    save_best(&best_result)?;
                }
            }
        }
    }

    // Machine-swap and machine-reassign local search alternated until
    // neither produces improvement. Each pass strictly reduces makespan.
    loop {
        let mut any = false;
        if let Some(improved) = improve_via_machine_swap(
            challenge,
            &job_products,
            &job_ops_len,
            &best_result,
        ) {
            if improved.makespan < best_result.makespan {
                best_result = improved;
                save_best(&best_result)?;
                any = true;
            }
        }
        if let Some(improved) = improve_via_machine_reassign(
            challenge,
            &job_products,
            &job_ops_len,
            &best_result,
        ) {
            if improved.makespan < best_result.makespan {
                best_result = improved;
                save_best(&best_result)?;
                any = true;
            }
        }
        if !any {
            break;
        }
    }

    save_solution(&Solution {
        job_schedule: best_result.job_schedule,
    })?;
    Ok(())
}

// ── Machine-swap local search ──────────────────────────────────────
// From an existing schedule, try swapping each consecutive pair of
// operations on the same machine and re-simulate. Returns the best
// improved result (or None if simulation fails / no improvement).
fn improve_via_machine_swap(
    challenge: &Challenge,
    job_products: &[usize],
    job_ops_len: &[usize],
    seed_result: &ScheduleResult,
) -> Option<ScheduleResult> {
    let num_machines = challenge.num_machines;

    // Build machine_order: per-machine list of (job, op_idx) sorted by start.
    let mut machine_order: Vec<Vec<(usize, usize)>> = (0..num_machines)
        .map(|m| {
            let mut v: Vec<(u32, usize, usize)> = seed_result
                .job_schedule
                .iter()
                .enumerate()
                .flat_map(|(j, ops)| {
                    ops.iter()
                        .enumerate()
                        .filter(move |(_, &(mm, _))| mm == m)
                        .map(move |(op_idx, &(_, start))| (start, j, op_idx))
                })
                .collect();
            v.sort_unstable();
            v.into_iter().map(|(_, j, o)| (j, o)).collect()
        })
        .collect();

    let mut best_makespan = seed_result.makespan;
    let mut best_schedule: Vec<Vec<(usize, u32)>> = seed_result.job_schedule.clone();

    let mut local_improved = true;
    let mut iter_count = 0usize;
    while local_improved && iter_count < 200 {
        local_improved = false;
        iter_count += 1;
        for m in 0..num_machines {
            if machine_order[m].len() < 2 {
                continue;
            }
            let mut i = 0;
            while i + 1 < machine_order[m].len() {
                machine_order[m].swap(i, i + 1);
                if let Some((sched, ms)) = simulate_from_order(
                    challenge,
                    job_products,
                    job_ops_len,
                    &machine_order,
                ) {
                    if ms < best_makespan {
                        best_makespan = ms;
                        best_schedule = sched;
                        local_improved = true;
                        // Keep the swap; advance past it.
                        i += 1;
                        continue;
                    } else {
                        machine_order[m].swap(i, i + 1); // revert
                    }
                } else {
                    machine_order[m].swap(i, i + 1); // revert
                }
                i += 1;
            }
        }
    }

    if best_makespan < seed_result.makespan {
        Some(ScheduleResult {
            job_schedule: best_schedule,
            makespan: best_makespan,
        })
    } else {
        None
    }
}

// ── Machine-reassign local search ──────────────────────────────────
// For each scheduled op, try moving it to each other eligible machine
// (appended at end of that machine's queue) and re-simulate. Accept the
// best improving reassignment per op, iterate until no improvement.
fn improve_via_machine_reassign(
    challenge: &Challenge,
    job_products: &[usize],
    job_ops_len: &[usize],
    seed_result: &ScheduleResult,
) -> Option<ScheduleResult> {
    let num_machines = challenge.num_machines;

    // machine_order[m] = ordered (job, op_idx) on machine m.
    let mut machine_order: Vec<Vec<(usize, usize)>> = (0..num_machines)
        .map(|m| {
            let mut v: Vec<(u32, usize, usize)> = seed_result
                .job_schedule
                .iter()
                .enumerate()
                .flat_map(|(j, ops)| {
                    ops.iter()
                        .enumerate()
                        .filter(move |(_, &(mm, _))| mm == m)
                        .map(move |(op_idx, &(_, start))| (start, j, op_idx))
                })
                .collect();
            v.sort_unstable();
            v.into_iter().map(|(_, j, o)| (j, o)).collect()
        })
        .collect();

    let mut best_makespan = seed_result.makespan;
    let mut best_schedule: Vec<Vec<(usize, u32)>> = seed_result.job_schedule.clone();

    let mut improved_any = true;
    let mut iter_count = 0usize;
    while improved_any && iter_count < 50 {
        improved_any = false;
        iter_count += 1;

        // Iterate over current ops per machine.  Snapshot first so the
        // moving target doesn't trip us up.
        let snapshot: Vec<Vec<(usize, usize)>> = machine_order.clone();
        for m in 0..num_machines {
            for &(j, op_idx) in &snapshot[m] {
                let product = job_products[j];
                let op_times = &challenge.product_processing_times[product][op_idx];
                if op_times.len() < 2 {
                    continue;
                }
                // Try moving this op to each other eligible machine.
                let cur_pos = machine_order[m].iter().position(|&(jj, oo)| jj == j && oo == op_idx);
                let cur_pos = match cur_pos {
                    Some(p) => p,
                    None => continue,
                };
                let mut best_target: Option<(usize, u32)> = None;
                for (&m2, _) in op_times.iter() {
                    if m2 == m {
                        continue;
                    }
                    // Remove from m, append at end of m2.
                    machine_order[m].remove(cur_pos);
                    machine_order[m2].push((j, op_idx));
                    if let Some((_, ms)) = simulate_from_order(
                        challenge,
                        job_products,
                        job_ops_len,
                        &machine_order,
                    ) {
                        if ms < best_makespan
                            && best_target.map_or(true, |(_, prev)| ms < prev)
                        {
                            best_target = Some((m2, ms));
                        }
                    }
                    // Revert: pop the appended op, re-insert at cur_pos in m.
                    machine_order[m2].pop();
                    machine_order[m].insert(cur_pos, (j, op_idx));
                }
                if let Some((m2, ms)) = best_target {
                    machine_order[m].remove(cur_pos);
                    machine_order[m2].push((j, op_idx));
                    if let Some((sched, ms2)) = simulate_from_order(
                        challenge,
                        job_products,
                        job_ops_len,
                        &machine_order,
                    ) {
                        if ms2 < best_makespan {
                            best_makespan = ms2;
                            best_schedule = sched;
                            improved_any = true;
                        } else {
                            machine_order[m2].pop();
                            machine_order[m].insert(cur_pos, (j, op_idx));
                        }
                        let _ = ms;
                    } else {
                        machine_order[m2].pop();
                        machine_order[m].insert(cur_pos, (j, op_idx));
                    }
                }
            }
        }
    }

    if best_makespan < seed_result.makespan {
        Some(ScheduleResult {
            job_schedule: best_schedule,
            makespan: best_makespan,
        })
    } else {
        None
    }
}

// Re-simulate the schedule given a fixed (op → machine) assignment and a
// fixed per-machine sequence. Returns (per-job (machine, start) list,
// makespan), or None if the ordering is deadlocked or infeasible.
fn simulate_from_order(
    challenge: &Challenge,
    job_products: &[usize],
    job_ops_len: &[usize],
    machine_order: &[Vec<(usize, usize)>],
) -> Option<(Vec<Vec<(usize, u32)>>, u32)> {
    let num_jobs = challenge.num_jobs;
    let num_machines = challenge.num_machines;

    let mut job_op_idx = vec![0usize; num_jobs];
    let mut machine_idx = vec![0usize; num_machines];
    let mut job_ready = vec![0u32; num_jobs];
    let mut machine_ready = vec![0u32; num_machines];
    let mut schedule: Vec<Vec<(usize, u32)>> = job_ops_len
        .iter()
        .map(|&l| Vec::with_capacity(l))
        .collect();
    let mut remaining: usize = job_ops_len.iter().sum();

    while remaining > 0 {
        let mut progress = false;
        for m in 0..num_machines {
            if machine_idx[m] >= machine_order[m].len() {
                continue;
            }
            let (j, op_idx) = machine_order[m][machine_idx[m]];
            if job_op_idx[j] != op_idx {
                continue;
            }
            let product = job_products[j];
            let op_times = &challenge.product_processing_times[product][op_idx];
            let &proc_time = op_times.get(&m)?;
            let start = job_ready[j].max(machine_ready[m]);
            let end = start + proc_time;
            schedule[j].push((m, start));
            job_ready[j] = end;
            machine_ready[m] = end;
            job_op_idx[j] += 1;
            machine_idx[m] += 1;
            remaining -= 1;
            progress = true;
        }
        if !progress {
            return None;
        }
    }

    let makespan = job_ready.iter().copied().max().unwrap_or(0);
    Some((schedule, makespan))
}
