use super::*;
use anyhow::{anyhow, Result};
use rand::{rngs::SmallRng, seq::SliceRandom, Rng, SeedableRng};
use serde_json::{Map, Value};
use std::collections::VecDeque;
use std::time::{Duration, Instant};

const TIME_LIMIT_MS: u64 = 4500;
const WORK_MIN_WEIGHT: f64 = 0.3;

struct Problem {
    num_jobs: usize,
    num_machines: usize,
    total_ops: usize,
    op_starts: Vec<usize>,
    op_job: Vec<usize>,
    op_idx: Vec<usize>,
    op_eligible: Vec<Vec<(usize, u32)>>,
    op_avg_time: Vec<f64>,
    op_min_time: Vec<f64>,
}

impl Problem {
    fn from(challenge: &Challenge) -> Result<Self> {
        let num_jobs = challenge.num_jobs;
        let num_machines = challenge.num_machines;
        let mut job_products = Vec::with_capacity(num_jobs);
        for (product, count) in challenge.jobs_per_product.iter().enumerate() {
            for _ in 0..*count {
                job_products.push(product);
            }
        }
        if job_products.len() != num_jobs {
            return Err(anyhow!(
                "Job count mismatch: expected {} got {}",
                num_jobs,
                job_products.len()
            ));
        }
        let mut op_starts = vec![0usize];
        let mut op_job = Vec::new();
        let mut op_idx = Vec::new();
        let mut op_eligible: Vec<Vec<(usize, u32)>> = Vec::new();
        let mut op_avg_time = Vec::new();
        let mut op_min_time = Vec::new();
        for (j, &p) in job_products.iter().enumerate() {
            let ops = &challenge.product_processing_times[p];
            for (oi, op) in ops.iter().enumerate() {
                let mut eligible: Vec<(usize, u32)> =
                    op.iter().map(|(&m, &t)| (m, t)).collect();
                eligible.sort_by_key(|&(m, _)| m);
                let n = eligible.len() as f64;
                let avg = eligible.iter().map(|(_, t)| *t as f64).sum::<f64>() / n;
                let min = eligible
                    .iter()
                    .map(|(_, t)| *t as f64)
                    .fold(f64::INFINITY, f64::min);
                op_avg_time.push(avg);
                op_min_time.push(min);
                op_eligible.push(eligible);
                op_job.push(j);
                op_idx.push(oi);
            }
            op_starts.push(op_eligible.len());
        }
        Ok(Problem {
            num_jobs,
            num_machines,
            total_ops: op_eligible.len(),
            op_starts,
            op_job,
            op_idx,
            op_eligible,
            op_avg_time,
            op_min_time,
        })
    }

    fn proc_on(&self, op: usize, machine: usize) -> Option<u32> {
        for &(m, t) in &self.op_eligible[op] {
            if m == machine {
                return Some(t);
            }
        }
        None
    }
}

#[derive(Clone)]
struct Schedule {
    assigned_machine: Vec<usize>,
    proc_time: Vec<u32>,
    start: Vec<u32>,
    end: Vec<u32>,
    machine_seq: Vec<Vec<usize>>,
    pos_in_machine: Vec<usize>,
    makespan: u32,
}

impl Schedule {
    fn new(prob: &Problem) -> Self {
        Self {
            assigned_machine: vec![usize::MAX; prob.total_ops],
            proc_time: vec![0; prob.total_ops],
            start: vec![0; prob.total_ops],
            end: vec![0; prob.total_ops],
            machine_seq: vec![Vec::new(); prob.num_machines],
            pos_in_machine: vec![0; prob.total_ops],
            makespan: 0,
        }
    }

    fn rebuild_times(&mut self, prob: &Problem) -> bool {
        let n = prob.total_ops;
        let mut in_deg = vec![0u8; n];
        for op in 0..n {
            if prob.op_idx[op] > 0 {
                in_deg[op] += 1;
            }
            if self.pos_in_machine[op] > 0 {
                in_deg[op] += 1;
            }
        }
        for v in self.start.iter_mut() {
            *v = 0;
        }
        for v in self.end.iter_mut() {
            *v = 0;
        }
        let mut queue: VecDeque<usize> = VecDeque::with_capacity(n);
        for op in 0..n {
            if in_deg[op] == 0 {
                self.end[op] = self.proc_time[op];
                queue.push_back(op);
            }
        }
        let mut processed = 0usize;
        let mut max_end = 0u32;
        while let Some(op) = queue.pop_front() {
            processed += 1;
            if self.end[op] > max_end {
                max_end = self.end[op];
            }
            let prev_end = self.end[op];
            let j = prob.op_job[op];
            let next_op = op + 1;
            if next_op < prob.op_starts[j + 1] {
                if prev_end > self.start[next_op] {
                    self.start[next_op] = prev_end;
                }
                in_deg[next_op] -= 1;
                if in_deg[next_op] == 0 {
                    self.end[next_op] = self.start[next_op] + self.proc_time[next_op];
                    queue.push_back(next_op);
                }
            }
            let m = self.assigned_machine[op];
            let pos = self.pos_in_machine[op];
            if pos + 1 < self.machine_seq[m].len() {
                let next_m_op = self.machine_seq[m][pos + 1];
                if prev_end > self.start[next_m_op] {
                    self.start[next_m_op] = prev_end;
                }
                in_deg[next_m_op] -= 1;
                if in_deg[next_m_op] == 0 {
                    self.end[next_m_op] = self.start[next_m_op] + self.proc_time[next_m_op];
                    queue.push_back(next_m_op);
                }
            }
        }
        self.makespan = max_end;
        processed == n
    }

    fn to_solution(&self, prob: &Problem) -> Solution {
        let mut job_schedule = Vec::with_capacity(prob.num_jobs);
        for j in 0..prob.num_jobs {
            let mut sched = Vec::new();
            for op in prob.op_starts[j]..prob.op_starts[j + 1] {
                sched.push((self.assigned_machine[op], self.start[op]));
            }
            job_schedule.push(sched);
        }
        Solution { job_schedule }
    }
}

#[derive(Clone, Copy)]
enum Rule {
    MWR, // Most Work Remaining
    MOR, // Most Ops Remaining
    LFJ, // Least Flexibility (fewest eligible machines)
    SPT, // Shortest Processing Time
    LPT, // Longest Processing Time
}

const ALL_RULES: [Rule; 5] = [
    Rule::MWR,
    Rule::MOR,
    Rule::LFJ,
    Rule::SPT,
    Rule::LPT,
];

fn construct(
    prob: &Problem,
    rule: Rule,
    top_k: usize,
    rng: &mut SmallRng,
) -> Option<Schedule> {
    let mut sched = Schedule::new(prob);
    let n_jobs = prob.num_jobs;
    let n_machines = prob.num_machines;
    let mut job_next_op = vec![0usize; n_jobs];
    let mut job_ready = vec![0u32; n_jobs];
    let mut machine_avail = vec![0u32; n_machines];
    let mut job_ops_count = vec![0usize; n_jobs];
    let mut job_remaining = vec![0.0f64; n_jobs];
    for j in 0..n_jobs {
        job_ops_count[j] = prob.op_starts[j + 1] - prob.op_starts[j];
        for op in prob.op_starts[j]..prob.op_starts[j + 1] {
            job_remaining[j] += prob.op_avg_time[op] * (1.0 - WORK_MIN_WEIGHT)
                + prob.op_min_time[op] * WORK_MIN_WEIGHT;
        }
    }
    let mut remaining_ops = prob.total_ops;
    let mut time = 0u32;
    let use_random = top_k > 1;

    while remaining_ops > 0 {
        let mut avail: Vec<usize> = (0..n_machines)
            .filter(|&m| machine_avail[m] <= time)
            .collect();
        if use_random {
            avail.shuffle(rng);
        }

        let mut scheduled_any = false;
        let mut candidates: Vec<(usize, f64, u32, u32, usize)> = Vec::new();
        for &m in &avail {
            candidates.clear();
            for j in 0..n_jobs {
                if job_next_op[j] >= job_ops_count[j] {
                    continue;
                }
                if job_ready[j] > time {
                    continue;
                }
                let op = prob.op_starts[j] + job_next_op[j];
                let proc = match prob.proc_on(op, m) {
                    Some(t) => t,
                    None => continue,
                };
                let machine_end = time.max(machine_avail[m]) + proc;
                let mut earliest_end = u32::MAX;
                for &(mm, t) in &prob.op_eligible[op] {
                    let end = time.max(machine_avail[mm]) + t;
                    if end < earliest_end {
                        earliest_end = end;
                    }
                }
                if machine_end != earliest_end {
                    continue;
                }
                let flexibility = prob.op_eligible[op].len();
                let priority = match rule {
                    Rule::MWR => job_remaining[j],
                    Rule::MOR => (job_ops_count[j] - job_next_op[j]) as f64,
                    Rule::LFJ => -(flexibility as f64),
                    Rule::SPT => -(proc as f64),
                    Rule::LPT => proc as f64,
                };
                candidates.push((op, priority, machine_end, proc, flexibility));
            }
            if candidates.is_empty() {
                continue;
            }
            candidates.sort_by(|a, b| {
                b.1.partial_cmp(&a.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(a.2.cmp(&b.2))
                    .then(a.3.cmp(&b.3))
                    .then(a.4.cmp(&b.4))
                    .then(a.0.cmp(&b.0))
            });
            let (op, proc) = if use_random {
                let k = top_k.min(candidates.len());
                let pick = rng.gen_range(0..k);
                (candidates[pick].0, candidates[pick].3)
            } else {
                (candidates[0].0, candidates[0].3)
            };
            let j = prob.op_job[op];
            let start_time = time.max(machine_avail[m]);
            let end_time = start_time + proc;
            sched.assigned_machine[op] = m;
            sched.proc_time[op] = proc;
            sched.start[op] = start_time;
            sched.end[op] = end_time;
            sched.pos_in_machine[op] = sched.machine_seq[m].len();
            sched.machine_seq[m].push(op);
            job_next_op[j] += 1;
            job_ready[j] = end_time;
            machine_avail[m] = end_time;
            let unit = prob.op_avg_time[op] * (1.0 - WORK_MIN_WEIGHT)
                + prob.op_min_time[op] * WORK_MIN_WEIGHT;
            job_remaining[j] -= unit;
            if job_remaining[j] < 0.0 {
                job_remaining[j] = 0.0;
            }
            remaining_ops -= 1;
            scheduled_any = true;
        }
        if remaining_ops == 0 {
            break;
        }
        let mut next_time: Option<u32> = None;
        for &t in machine_avail.iter() {
            if t > time {
                next_time = Some(next_time.map_or(t, |b: u32| b.min(t)));
            }
        }
        for j in 0..n_jobs {
            if job_next_op[j] < job_ops_count[j] && job_ready[j] > time {
                let t = job_ready[j];
                next_time = Some(next_time.map_or(t, |b: u32| b.min(t)));
            }
        }
        match next_time {
            Some(t) => time = t,
            None => {
                if !scheduled_any {
                    return None;
                }
                break;
            }
        }
    }
    sched.makespan = sched.end.iter().copied().max().unwrap_or(0);
    Some(sched)
}

fn critical_path(prob: &Problem, sched: &Schedule) -> Vec<usize> {
    let mut end_op = 0usize;
    let mut max_end = 0u32;
    for op in 0..prob.total_ops {
        if sched.end[op] > max_end {
            max_end = sched.end[op];
            end_op = op;
        }
    }
    let mut cp = Vec::new();
    if max_end == 0 {
        return cp;
    }
    let mut current = end_op;
    loop {
        cp.push(current);
        let s = sched.start[current];
        let mut prev: Option<usize> = None;
        if prob.op_idx[current] > 0 {
            let job_prev = current - 1;
            if sched.end[job_prev] == s {
                prev = Some(job_prev);
            }
        }
        if prev.is_none() {
            let m = sched.assigned_machine[current];
            let pos = sched.pos_in_machine[current];
            if pos > 0 {
                let mach_prev = sched.machine_seq[m][pos - 1];
                if sched.end[mach_prev] == s {
                    prev = Some(mach_prev);
                }
            }
        }
        match prev {
            Some(p) => current = p,
            None => break,
        }
    }
    cp.reverse();
    cp
}

fn try_swap(
    prob: &Problem,
    sched: &mut Schedule,
    a: usize,
    b: usize,
) -> bool {
    if prob.op_job[a] == prob.op_job[b] {
        return false;
    }
    let m = sched.assigned_machine[a];
    if sched.assigned_machine[b] != m {
        return false;
    }
    let pa = sched.pos_in_machine[a];
    let pb = sched.pos_in_machine[b];
    sched.machine_seq[m].swap(pa, pb);
    sched.pos_in_machine[a] = pb;
    sched.pos_in_machine[b] = pa;
    let old_makespan = sched.makespan;
    let ok = sched.rebuild_times(prob);
    if ok && sched.makespan < old_makespan {
        return true;
    }
    sched.machine_seq[m].swap(pa, pb);
    sched.pos_in_machine[a] = pa;
    sched.pos_in_machine[b] = pb;
    sched.rebuild_times(prob);
    false
}

fn try_reassign(
    prob: &Problem,
    sched: &mut Schedule,
    op: usize,
    new_machine: usize,
    new_pos: usize,
) -> bool {
    let old_m = sched.assigned_machine[op];
    if old_m == new_machine {
        return false;
    }
    let new_proc = match prob.proc_on(op, new_machine) {
        Some(t) => t,
        None => return false,
    };
    let old_pos = sched.pos_in_machine[op];
    let old_proc = sched.proc_time[op];
    let old_makespan = sched.makespan;

    sched.machine_seq[old_m].remove(old_pos);
    for i in old_pos..sched.machine_seq[old_m].len() {
        let oo = sched.machine_seq[old_m][i];
        sched.pos_in_machine[oo] = i;
    }
    let insert_at = new_pos.min(sched.machine_seq[new_machine].len());
    sched.machine_seq[new_machine].insert(insert_at, op);
    for i in insert_at..sched.machine_seq[new_machine].len() {
        let oo = sched.machine_seq[new_machine][i];
        sched.pos_in_machine[oo] = i;
    }
    sched.assigned_machine[op] = new_machine;
    sched.proc_time[op] = new_proc;

    let ok = sched.rebuild_times(prob);
    if ok && sched.makespan < old_makespan {
        return true;
    }

    // Revert
    sched.machine_seq[new_machine].remove(insert_at);
    for i in insert_at..sched.machine_seq[new_machine].len() {
        let oo = sched.machine_seq[new_machine][i];
        sched.pos_in_machine[oo] = i;
    }
    sched.machine_seq[old_m].insert(old_pos, op);
    for i in old_pos..sched.machine_seq[old_m].len() {
        let oo = sched.machine_seq[old_m][i];
        sched.pos_in_machine[oo] = i;
    }
    sched.assigned_machine[op] = old_m;
    sched.proc_time[op] = old_proc;
    sched.rebuild_times(prob);
    false
}

fn try_intra_move(prob: &Problem, sched: &mut Schedule, op: usize, new_pos: usize) -> bool {
    let m = sched.assigned_machine[op];
    let old_pos = sched.pos_in_machine[op];
    let n = sched.machine_seq[m].len();
    if new_pos >= n || new_pos == old_pos {
        return false;
    }
    let old_makespan = sched.makespan;

    sched.machine_seq[m].remove(old_pos);
    let actual_new_pos = if new_pos > old_pos { new_pos - 1 } else { new_pos };
    sched.machine_seq[m].insert(actual_new_pos, op);
    let lo = old_pos.min(actual_new_pos);
    for i in lo..sched.machine_seq[m].len() {
        let oo = sched.machine_seq[m][i];
        sched.pos_in_machine[oo] = i;
    }

    let ok = sched.rebuild_times(prob);
    if ok && sched.makespan < old_makespan {
        return true;
    }

    // Revert.
    sched.machine_seq[m].remove(actual_new_pos);
    sched.machine_seq[m].insert(old_pos, op);
    let lo = old_pos.min(actual_new_pos);
    for i in lo..sched.machine_seq[m].len() {
        let oo = sched.machine_seq[m][i];
        sched.pos_in_machine[oo] = i;
    }
    sched.rebuild_times(prob);
    false
}

fn good_insert_pos(prob: &Problem, sched: &Schedule, op: usize, m: usize) -> usize {
    let job_prev_end = if prob.op_idx[op] == 0 {
        0
    } else {
        sched.end[op - 1]
    };
    let mut pos = 0usize;
    for (i, &other) in sched.machine_seq[m].iter().enumerate() {
        if other == op {
            continue;
        }
        if sched.end[other] <= job_prev_end {
            pos = i + 1;
        } else {
            break;
        }
    }
    pos
}

fn perturb(prob: &Problem, sched: &mut Schedule, rng: &mut SmallRng, n_kicks: usize) {
    for _ in 0..n_kicks {
        let cp = critical_path(prob, sched);
        if cp.len() < 2 {
            return;
        }
        // Try up to 12 random adjacent CP pairs to find a valid swap.
        let mut applied = false;
        for _ in 0..12 {
            let i = rng.gen_range(0..(cp.len() - 1));
            let a = cp[i];
            let b = cp[i + 1];
            if prob.op_job[a] == prob.op_job[b] {
                continue;
            }
            let m = sched.assigned_machine[a];
            if sched.assigned_machine[b] != m {
                continue;
            }
            let pa = sched.pos_in_machine[a];
            let pb = sched.pos_in_machine[b];
            sched.machine_seq[m].swap(pa, pb);
            sched.pos_in_machine[a] = pb;
            sched.pos_in_machine[b] = pa;
            if !sched.rebuild_times(prob) {
                // Cycle — revert.
                sched.machine_seq[m].swap(pa, pb);
                sched.pos_in_machine[a] = pa;
                sched.pos_in_machine[b] = pb;
                sched.rebuild_times(prob);
                continue;
            }
            applied = true;
            break;
        }
        if !applied {
            return;
        }
    }
}

/// LNS-style destroy-and-recreate perturbation: pull `n_destroy` random ops out of the
/// schedule and re-insert each at a randomly chosen eligible machine and position.
/// Returns false (and reverts) if the resulting graph has a cycle.
fn perturb_lns(
    prob: &Problem,
    sched: &mut Schedule,
    rng: &mut SmallRng,
    n_destroy: usize,
) -> bool {
    if n_destroy == 0 || n_destroy >= prob.total_ops {
        return false;
    }
    let backup = sched.clone();

    // Pick n_destroy distinct random ops.
    let mut indices: Vec<usize> = (0..prob.total_ops).collect();
    indices.shuffle(rng);
    indices.truncate(n_destroy);

    // Remove from machines (descending position to avoid index shifts).
    let mut removed = indices.clone();
    removed.sort_by(|a, b| {
        let pa = (sched.assigned_machine[*a], sched.pos_in_machine[*a]);
        let pb = (sched.assigned_machine[*b], sched.pos_in_machine[*b]);
        pb.cmp(&pa)
    });
    for &op in &removed {
        let m = sched.assigned_machine[op];
        let pos = sched.pos_in_machine[op];
        sched.machine_seq[m].remove(pos);
        for i in pos..sched.machine_seq[m].len() {
            let oo = sched.machine_seq[m][i];
            sched.pos_in_machine[oo] = i;
        }
    }

    // Reinsert in random order: random eligible machine, random position.
    let mut order = indices;
    order.shuffle(rng);
    for &op in &order {
        let eligible = &prob.op_eligible[op];
        let &(m, pt) = &eligible[rng.gen_range(0..eligible.len())];
        let n = sched.machine_seq[m].len();
        let pos = if n == 0 { 0 } else { rng.gen_range(0..=n) };
        sched.assigned_machine[op] = m;
        sched.proc_time[op] = pt;
        sched.machine_seq[m].insert(pos, op);
        for i in pos..sched.machine_seq[m].len() {
            let oo = sched.machine_seq[m][i];
            sched.pos_in_machine[oo] = i;
        }
    }

    if !sched.rebuild_times(prob) {
        *sched = backup;
        sched.rebuild_times(prob);
        return false;
    }
    true
}

fn local_search(
    prob: &Problem,
    sched: &mut Schedule,
    deadline: Instant,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    best_makespan: &mut u32,
) -> Result<()> {
    loop {
        if Instant::now() >= deadline {
            return Ok(());
        }
        let cp = critical_path(prob, sched);
        if cp.len() < 2 {
            return Ok(());
        }
        let mut blocks: Vec<(usize, usize)> = Vec::new();
        let mut start = 0usize;
        let mut cur_machine = sched.assigned_machine[cp[0]];
        for i in 1..cp.len() {
            let m = sched.assigned_machine[cp[i]];
            if m != cur_machine {
                if i - start >= 2 {
                    blocks.push((start, i));
                }
                start = i;
                cur_machine = m;
            }
        }
        if cp.len() - start >= 2 {
            blocks.push((start, cp.len()));
        }

        let mut improved = false;
        // Try swaps first (cheaper). N5 adjacent + N7 head/tail with non-adjacent.
        'outer: for &(b_start, b_end) in &blocks {
            // N5: adjacent swaps in block.
            for i in b_start..(b_end - 1) {
                if Instant::now() >= deadline {
                    return Ok(());
                }
                if try_swap(prob, sched, cp[i], cp[i + 1]) {
                    improved = true;
                    break 'outer;
                }
            }
        }

        // Then try machine reassignments for ops on critical path.
        if !improved {
            'reassign: for &op in &cp {
                if prob.op_eligible[op].len() <= 1 {
                    continue;
                }
                let cur_m = sched.assigned_machine[op];
                for &(m, _) in &prob.op_eligible[op] {
                    if m == cur_m {
                        continue;
                    }
                    if Instant::now() >= deadline {
                        return Ok(());
                    }
                    let pos = good_insert_pos(prob, sched, op, m);
                    if try_reassign(prob, sched, op, m, pos) {
                        improved = true;
                        break 'reassign;
                    }
                    // Also try at end of new machine
                    let end_pos = sched.machine_seq[m].len();
                    if end_pos != pos && try_reassign(prob, sched, op, m, end_pos) {
                        improved = true;
                        break 'reassign;
                    }
                }
            }
        }

        if !improved {
            return Ok(());
        }

        if sched.makespan < *best_makespan {
            *best_makespan = sched.makespan;
            save_solution(&sched.to_solution(prob))?;
        }
    }
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let start = Instant::now();
    let deadline = start + Duration::from_millis(TIME_LIMIT_MS);
    let prob = Problem::from(challenge)?;
    let mut rng = SmallRng::from_seed(challenge.seed);

    let mut best_makespan = u32::MAX;
    // top_pool keeps the K best initial schedules (lowest makespan first).
    const POOL_SIZE: usize = 4;
    let mut top_pool: Vec<Schedule> = Vec::with_capacity(POOL_SIZE + 1);

    let mut consider = |s: Schedule,
                        best_makespan: &mut u32,
                        top_pool: &mut Vec<Schedule>,
                        save: &dyn Fn(&Solution) -> Result<()>|
     -> Result<()> {
        if s.makespan < *best_makespan {
            *best_makespan = s.makespan;
            save(&s.to_solution(&prob))?;
        }
        // Insert into top pool sorted by makespan ascending
        let pos = top_pool
            .iter()
            .position(|x| x.makespan > s.makespan)
            .unwrap_or(top_pool.len());
        if pos < POOL_SIZE {
            top_pool.insert(pos, s);
            if top_pool.len() > POOL_SIZE {
                top_pool.pop();
            }
        }
        Ok(())
    };

    // Phase 1: deterministic dispatch rules
    for &rule in &ALL_RULES {
        if let Some(s) = construct(&prob, rule, 1, &mut rng) {
            consider(s, &mut best_makespan, &mut top_pool, save_solution)?;
        }
    }
    if top_pool.is_empty() {
        return Err(anyhow!("No valid initial schedule produced"));
    }

    // Phase 2: random restarts (~55% of total budget on construction).
    let phase2_deadline = start + Duration::from_millis(TIME_LIMIT_MS * 55 / 100);
    let mut restart_iter = 0;
    while Instant::now() < phase2_deadline {
        restart_iter += 1;
        if restart_iter > 500 {
            break;
        }
        let rule = ALL_RULES[rng.gen_range(0..ALL_RULES.len())];
        let top_k = rng.gen_range(2..=5);
        if let Some(s) = construct(&prob, rule, top_k, &mut rng) {
            consider(s, &mut best_makespan, &mut top_pool, save_solution)?;
        }
    }

    // Phase 3a: local search (swap + reassign) on each pooled schedule, best first.
    let mut best_sched: Option<Schedule> = None;
    for mut current in top_pool.into_iter() {
        if Instant::now() >= deadline {
            break;
        }
        local_search(&prob, &mut current, deadline, save_solution, &mut best_makespan)?;
        if best_sched
            .as_ref()
            .map_or(true, |b: &Schedule| current.makespan < b.makespan)
        {
            best_sched = Some(current);
        }
    }

    // Phase 3b: ILS — kick (swap or LNS destroy-recreate) + LS, accept if better.
    if let Some(mut current) = best_sched {
        if current.makespan < best_makespan {
            best_makespan = current.makespan;
            save_solution(&current.to_solution(&prob))?;
        }
        let mut snapshot = current.clone();
        let mut consec_no_improve = 0usize;
        const RESTART_THRESHOLD: usize = 25;
        while Instant::now() < deadline {
            if consec_no_improve >= RESTART_THRESHOLD {
                consec_no_improve = 0;
                let rule = ALL_RULES[rng.gen_range(0..ALL_RULES.len())];
                let top_k = rng.gen_range(2..=5);
                if let Some(mut fresh) = construct(&prob, rule, top_k, &mut rng) {
                    local_search(
                        &prob,
                        &mut fresh,
                        deadline,
                        save_solution,
                        &mut best_makespan,
                    )?;
                    if fresh.makespan <= snapshot.makespan {
                        snapshot = fresh.clone();
                        current = fresh;
                        continue;
                    }
                }
                current = snapshot.clone();
            }
            // Pick a kick type. LNS destroy-recreate every 4th iter for diversity.
            let do_lns = consec_no_improve >= 3 && rng.gen::<u32>() % 4 == 0;
            if do_lns {
                let n_destroy = (prob.total_ops / 12).max(4).min(20);
                if !perturb_lns(&prob, &mut current, &mut rng, n_destroy) {
                    // LNS hit a cycle — revert to snapshot and skip.
                    current = snapshot.clone();
                    continue;
                }
            } else {
                let n_kicks = if consec_no_improve < 5 { 2 } else { 4 };
                perturb(&prob, &mut current, &mut rng, n_kicks);
            }
            local_search(&prob, &mut current, deadline, save_solution, &mut best_makespan)?;
            if current.makespan < snapshot.makespan {
                snapshot = current.clone();
                consec_no_improve = 0;
            } else {
                consec_no_improve += 1;
                current = snapshot.clone();
            }
        }
    }

    Ok(())
}
