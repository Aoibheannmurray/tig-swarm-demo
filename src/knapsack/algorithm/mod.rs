use super::*;
use anyhow::Result;
use rand::{rngs::SmallRng, Rng, SeedableRng};
use serde_json::{Map, Value};
use std::time::{Duration, Instant};

enum Move {
    Swap(usize, usize), // (selected_pos, unselected_pos)
    Add(usize),         // unselected_pos
    Drop(usize),        // selected_pos
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let start = Instant::now();
    let deadline = start + Duration::from_millis(4500);

    let n = challenge.num_items;
    if n == 0 {
        save_solution(&Solution { items: vec![] })?;
        return Ok(());
    }

    let max_w = challenge.max_weight;
    let weights = &challenge.weights;
    let values = &challenge.values;
    let inter = &challenge.interaction_values;

    // Sparse adjacency: nbrs[i] = list of (j, inter[i][j]) for j with
    // non-zero interaction. The dense matrix is kept for O(1) lookups in
    // the swap eval; nbrs is for fast O(deg) gain updates instead of O(n).
    let mut nbrs: Vec<Vec<(u32, i32)>> = vec![Vec::new(); n];
    for i in 0..n {
        let row = &inter[i];
        let mut row_n: Vec<(u32, i32)> = Vec::new();
        for j in 0..n {
            if i != j {
                let v = row[j];
                if v != 0 {
                    row_n.push((j as u32, v));
                }
            }
        }
        nbrs[i] = row_n;
    }

    let mut rng = SmallRng::from_seed(challenge.seed);

    // Step 1: value-density greedy initial solution.
    let mut ratio: Vec<(usize, f64)> = (0..n)
        .map(|i| {
            let inter_sum: i64 = nbrs[i].iter().map(|&(_, v)| v as i64).sum();
            let total: i64 = values[i] as i64 + inter_sum;
            (i, total as f64 / weights[i] as f64)
        })
        .collect();
    ratio.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut is_sel = vec![false; n];
    let mut selected: Vec<usize> = Vec::with_capacity(n);
    let mut unselected: Vec<usize> = Vec::with_capacity(n);
    let mut total_weight: u32 = 0;
    for &(it, _) in &ratio {
        if total_weight + weights[it] <= max_w {
            total_weight += weights[it];
            is_sel[it] = true;
            selected.push(it);
        } else {
            unselected.push(it);
        }
    }

    // gain[x] = values[x] + sum_{y in selected} inter[x][y]
    // For x in selected, gain[x] equals x's marginal contribution to the
    // objective (since inter[x][x] = 0). The objective itself satisfies
    //   obj = (sum_{x in S} gain[x] + sum_{x in S} values[x]) / 2
    // because sum_{x in S} gain[x] = sum v + 2 * pair_sum.
    let mut gain: Vec<i64> = (0..n).map(|i| values[i] as i64).collect();
    for &y in &selected {
        for &(x, v) in &nbrs[y] {
            gain[x as usize] += v as i64;
        }
    }

    let mut current_obj: i64 = {
        let sum_gain: i64 = selected.iter().map(|&x| gain[x]).sum();
        let sum_v: i64 = selected.iter().map(|&x| values[x] as i64).sum();
        (sum_gain + sum_v) / 2
    };

    let mut best_obj = current_obj;
    let mut best_selected = selected.clone();
    let mut best_is_selected = is_sel.clone();
    let mut best_weight = total_weight;
    save_solution(&Solution { items: best_selected.clone() })?;

    // Tabu search with aspiration + ILS perturbation.
    let mut tabu = vec![0u32; n];
    let tabu_tenure: u32 = 7;
    let max_stall: u32 = 100;

    'outer: loop {
        if Instant::now() >= deadline {
            break;
        }

        // Reset tabu at the start of each ILS pass.
        for t in tabu.iter_mut() {
            *t = 0;
        }
        let mut stall: u32 = 0;

        // Inner: local search.
        while stall < max_stall {
            if Instant::now() >= deadline {
                break 'outer;
            }

            // Sort unselected by gain desc and selected by gain asc so that
            // the swap loops can early-terminate via the non-negative-interaction
            // upper bound.  Interactions are jaccard*1000 in [0, 1000], so:
            //   delta <= g_a - gain[d]  (since inter[a][d] >= 0)
            //   delta <= g_a - min_sel_gain  (over all feasible d)
            // Once g_a falls below the cutoff, no later candidate can improve.
            unselected.sort_unstable_by(|&i, &j| gain[j].cmp(&gain[i]));
            selected.sort_unstable_by(|&i, &j| gain[i].cmp(&gain[j]));

            let min_sel_gain = if selected.is_empty() { 0 } else { gain[selected[0]] };

            // Best-improvement neighborhood: 1-1 swap, add-only, drop-only.
            let mut best_delta: i64 = 0;
            let mut best_move: Option<Move> = None;

            // 1-1 swap (early-terminating).
            for ai in 0..unselected.len() {
                let a = unselected[ai];
                let g_a = gain[a];
                if g_a - min_sel_gain <= best_delta {
                    break;
                }
                let a_tabu = tabu[a] > 0;
                let need_extra = weights[a] as i64 + total_weight as i64 - max_w as i64;
                for di in 0..selected.len() {
                    let d = selected[di];
                    let g_d = gain[d];
                    if g_a - g_d <= best_delta {
                        // selected sorted asc by gain — all later d are worse.
                        break;
                    }
                    if need_extra > 0 && (weights[d] as i64) < need_extra {
                        continue;
                    }
                    let delta = g_a - inter[a][d] as i64 - g_d;
                    if delta <= best_delta {
                        continue;
                    }
                    let is_tabu = a_tabu || tabu[d] > 0;
                    if is_tabu && current_obj + delta <= best_obj {
                        continue;
                    }
                    best_delta = delta;
                    best_move = Some(Move::Swap(di, ai));
                }
            }

            // Add-only (uses any spare capacity).
            if total_weight < max_w {
                let slack = max_w - total_weight;
                for ai in 0..unselected.len() {
                    let a = unselected[ai];
                    if weights[a] > slack {
                        continue;
                    }
                    let g_a = gain[a];
                    if g_a <= best_delta {
                        continue;
                    }
                    if tabu[a] > 0 && current_obj + g_a <= best_obj {
                        continue;
                    }
                    best_delta = g_a;
                    best_move = Some(Move::Add(ai));
                }
            }

            // Drop-only (only useful if some item has negative marginal contribution).
            for di in 0..selected.len() {
                let d = selected[di];
                let delta = -gain[d];
                if delta <= best_delta {
                    continue;
                }
                if tabu[d] > 0 && current_obj + delta <= best_obj {
                    continue;
                }
                best_delta = delta;
                best_move = Some(Move::Drop(di));
            }

            // No improving move? Take the best non-tabu *worsening* move
            // among the top-K candidates from the already-sorted lists
            // (highest-gain unselected paired with lowest-gain selected).
            // Considers Swap, Add, and Drop — Add/Drop give extra escape
            // routes when capacity slack or low-gain selected items exist.
            // The tabu list prevents immediate reversal.
            if best_move.is_none() {
                let k_u = unselected.len().min(64);
                let k_s = selected.len().min(64);
                let mut bd_w: i64 = i64::MIN;
                let mut bm_w: Option<Move> = None;
                // Swap.
                for ai in 0..k_u {
                    let a = unselected[ai];
                    if tabu[a] > 0 {
                        continue;
                    }
                    let g_a = gain[a];
                    let need_extra = weights[a] as i64 + total_weight as i64 - max_w as i64;
                    for di in 0..k_s {
                        let d = selected[di];
                        if tabu[d] > 0 {
                            continue;
                        }
                        if need_extra > 0 && (weights[d] as i64) < need_extra {
                            continue;
                        }
                        let delta = g_a - inter[a][d] as i64 - gain[d];
                        if delta > bd_w {
                            bd_w = delta;
                            bm_w = Some(Move::Swap(di, ai));
                        }
                    }
                }
                // Add (worsening — only if slack capacity).
                if total_weight < max_w {
                    let slack = max_w - total_weight;
                    for ai in 0..k_u {
                        let a = unselected[ai];
                        if tabu[a] > 0 || weights[a] > slack {
                            continue;
                        }
                        let delta = gain[a];
                        if delta > bd_w {
                            bd_w = delta;
                            bm_w = Some(Move::Add(ai));
                        }
                    }
                }
                // Drop.
                for di in 0..k_s {
                    let d = selected[di];
                    if tabu[d] > 0 {
                        continue;
                    }
                    let delta = -gain[d];
                    if delta > bd_w {
                        bd_w = delta;
                        bm_w = Some(Move::Drop(di));
                    }
                }
                if let Some(mv) = bm_w {
                    best_delta = bd_w;
                    best_move = Some(mv);
                }
            }

            match best_move {
                Some(Move::Swap(di, ai)) => {
                    let d = selected[di];
                    let a = unselected[ai];
                    selected.swap_remove(di);
                    unselected.swap_remove(ai);
                    selected.push(a);
                    unselected.push(d);
                    is_sel[d] = false;
                    is_sel[a] = true;
                    total_weight = total_weight + weights[a] - weights[d];
                    current_obj += best_delta;
                    for &(x, v) in &nbrs[a] {
                        gain[x as usize] += v as i64;
                    }
                    for &(x, v) in &nbrs[d] {
                        gain[x as usize] -= v as i64;
                    }
                    tabu[d] = tabu_tenure;
                    tabu[a] = tabu_tenure;
                }
                Some(Move::Add(ai)) => {
                    let a = unselected[ai];
                    unselected.swap_remove(ai);
                    selected.push(a);
                    is_sel[a] = true;
                    total_weight += weights[a];
                    current_obj += best_delta;
                    for &(x, v) in &nbrs[a] {
                        gain[x as usize] += v as i64;
                    }
                    tabu[a] = tabu_tenure;
                }
                Some(Move::Drop(di)) => {
                    let d = selected[di];
                    selected.swap_remove(di);
                    unselected.push(d);
                    is_sel[d] = false;
                    total_weight -= weights[d];
                    current_obj += best_delta;
                    for &(x, v) in &nbrs[d] {
                        gain[x as usize] -= v as i64;
                    }
                    tabu[d] = tabu_tenure;
                }
                None => break,
            }

            // Decrement tabu counters.
            for t in tabu.iter_mut() {
                *t = t.saturating_sub(1);
            }

            if current_obj > best_obj {
                best_obj = current_obj;
                best_selected = selected.clone();
                best_is_selected = is_sel.clone();
                best_weight = total_weight;
                save_solution(&Solution { items: best_selected.clone() })?;
                stall = 0;
            } else {
                stall += 1;
            }
        }

        if Instant::now() >= deadline {
            break;
        }

        // Perturbation: restart from best, kick random items, refill greedily.
        selected = best_selected.clone();
        is_sel = best_is_selected.clone();
        total_weight = best_weight;
        unselected = (0..n).filter(|i| !is_sel[*i]).collect();
        for x in 0..n {
            gain[x] = values[x] as i64;
        }
        for &y in &selected {
            for &(x, v) in &nbrs[y] {
                gain[x as usize] += v as i64;
            }
        }
        current_obj = best_obj;

        let kick = ((selected.len() / 5).max(3)).min(selected.len());
        for _ in 0..kick {
            if selected.is_empty() {
                break;
            }
            let idx = rng.gen_range(0..selected.len());
            let d = selected.swap_remove(idx);
            is_sel[d] = false;
            total_weight -= weights[d];
            unselected.push(d);
            current_obj -= gain[d];
            for &(x, v) in &nbrs[d] {
                gain[x as usize] -= v as i64;
            }
        }

        // Greedy refill on current gain[].
        let mut cands: Vec<(usize, f64)> = unselected
            .iter()
            .map(|&i| (i, gain[i] as f64 / weights[i] as f64))
            .collect();
        cands.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        for (item, _) in cands {
            if is_sel[item] {
                continue;
            }
            if total_weight + weights[item] <= max_w {
                selected.push(item);
                is_sel[item] = true;
                total_weight += weights[item];
                current_obj += gain[item];
                for &(x, v) in &nbrs[item] {
                    gain[x as usize] += v as i64;
                }
            }
        }
        unselected = (0..n).filter(|i| !is_sel[*i]).collect();
    }

    Ok(())
}
