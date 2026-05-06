use super::*;
// --- BEGIN EDITABLE REGION --- //
use anyhow::Result;
use rand::{rngs::SmallRng, Rng, SeedableRng};
use serde_json::{Map, Value};
use std::time::Instant;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let start = Instant::now();
    let time_budget_ms: u128 = 4500;

    let n = challenge.num_items;
    if n == 0 || challenge.max_weight == 0 {
        let _ = save_solution(&Solution { items: Vec::new() });
        return Ok(());
    }

    let weights = &challenge.weights;
    let values = &challenge.values;
    let inter = &challenge.interaction_values;
    let max_w = challenge.max_weight;

    let mut rng = SmallRng::from_seed(challenge.seed);

    // Greedy initial: ratio = (values[i] + sum_j inter[i][j]) / weights[i]
    let mut item_values: Vec<(usize, f32)> = (0..n)
        .map(|i| {
            let total: i32 = values[i] as i32 + inter[i].iter().sum::<i32>();
            (i, total as f32 / weights[i] as f32)
        })
        .collect();
    item_values.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut is_selected = vec![false; n];
    let mut selected: Vec<usize> = Vec::with_capacity(n);
    let mut unselected: Vec<usize> = Vec::with_capacity(n);
    let mut total_weight: u32 = 0;
    for &(item, _) in &item_values {
        if total_weight + weights[item] <= max_w {
            total_weight += weights[item];
            selected.push(item);
            is_selected[item] = true;
        } else {
            unselected.push(item);
        }
    }

    // interaction_sum_list[x] = values[x] + sum_{j in selected} inter[x][j]
    let mut interaction_sum: Vec<i32> = vec![0; n];
    for x in 0..n {
        let mut s = values[x] as i32;
        for &j in &selected {
            s += inter[x][j];
        }
        interaction_sum[x] = s;
    }

    let mut current_value: i64 = compute_total_value(challenge, &selected) as i64;
    let mut best_selected: Vec<usize> = selected.clone();
    let mut best_value: i64 = current_value;
    let _ = save_solution(&Solution {
        items: best_selected.clone(),
    });

    // ----- Phase 1: baseline-equivalent tabu search to local optimum -----
    let mut tabu = vec![0i32; n];
    run_tabu_phase(
        &mut selected,
        &mut unselected,
        &mut is_selected,
        &mut total_weight,
        &mut interaction_sum,
        &mut tabu,
        &mut current_value,
        weights,
        inter,
        max_w,
        100,
        3,
    );
    if current_value > best_value {
        best_value = current_value;
        best_selected = selected.clone();
        let _ = save_solution(&Solution {
            items: best_selected.clone(),
        });
    }

    // ----- Phase 2: ILS with perturbation + local search -----
    while start.elapsed().as_millis() < time_budget_ms {
        // Restart state from best_selected
        for x in is_selected.iter_mut() {
            *x = false;
        }
        selected.clear();
        unselected.clear();
        total_weight = 0;
        for &i in &best_selected {
            is_selected[i] = true;
            selected.push(i);
            total_weight += weights[i];
        }
        for i in 0..n {
            if !is_selected[i] {
                unselected.push(i);
            }
        }
        // Recompute interaction_sum.
        for x in 0..n {
            let mut s = values[x] as i32;
            for &j in &selected {
                s += inter[x][j];
            }
            interaction_sum[x] = s;
        }
        current_value = best_value;

        // Perturb: drop a random fraction of selected items.
        if selected.is_empty() {
            break;
        }
        let drop_count = (selected.len() / 5).max(2).min(selected.len());
        for _ in 0..drop_count {
            if selected.is_empty() {
                break;
            }
            let pos = rng.gen_range(0..selected.len());
            let it = selected[pos];
            selected.swap_remove(pos);
            unselected.push(it);
            is_selected[it] = false;
            total_weight -= weights[it];
            let row = &inter[it];
            for i in 0..n {
                interaction_sum[i] -= row[i];
            }
        }

        // Greedy refill by current interaction_sum (descending).
        unselected.sort_by(|&a, &b| interaction_sum[b].cmp(&interaction_sum[a]));
        let mut i = 0usize;
        while i < unselected.len() {
            let cand = unselected[i];
            if total_weight + weights[cand] <= max_w {
                total_weight += weights[cand];
                is_selected[cand] = true;
                selected.push(cand);
                let row = &inter[cand];
                for k in 0..n {
                    interaction_sum[k] += row[k];
                }
                unselected.swap_remove(i);
            } else {
                i += 1;
            }
        }
        current_value = compute_total_value(challenge, &selected) as i64;

        // Reset tabu list and run improving LS to convergence.
        for t in tabu.iter_mut() {
            *t = 0;
        }
        run_tabu_phase(
            &mut selected,
            &mut unselected,
            &mut is_selected,
            &mut total_weight,
            &mut interaction_sum,
            &mut tabu,
            &mut current_value,
            weights,
            inter,
            max_w,
            200,
            3,
        );
        if current_value > best_value {
            best_value = current_value;
            best_selected = selected.clone();
            let _ = save_solution(&Solution {
                items: best_selected.clone(),
            });
        }

        if start.elapsed().as_millis() >= time_budget_ms {
            break;
        }
    }

    let _ = save_solution(&Solution {
        items: best_selected,
    });
    Ok(())
}

/// Improving-only tabu local search (1-1 swap neighbourhood). Mirrors the
/// baseline's structure: outer loop over unselected, inner over selected,
/// with weight + value pruning.
fn run_tabu_phase(
    selected: &mut Vec<usize>,
    unselected: &mut Vec<usize>,
    is_selected: &mut [bool],
    total_weight: &mut u32,
    interaction_sum: &mut [i32],
    tabu: &mut [i32],
    current_value: &mut i64,
    weights: &[u32],
    inter: &[Vec<i32>],
    max_w: u32,
    max_iterations: u32,
    tabu_tenure: i32,
) {
    let n = is_selected.len();

    let mut min_selected_iv = i32::MAX;
    for x in 0..n {
        if is_selected[x] {
            min_selected_iv = min_selected_iv.min(interaction_sum[x]);
        }
    }

    for _ in 0..max_iterations {
        let mut best_improvement: i32 = 0;
        let mut best_swap: Option<(usize, usize)> = None; // (unsel_idx, sel_idx)

        for i in 0..unselected.len() {
            let new_item = unselected[i];
            if tabu[new_item] > 0 {
                continue;
            }
            let new_iv = interaction_sum[new_item];
            if new_iv < best_improvement + min_selected_iv {
                continue;
            }
            let min_weight =
                weights[new_item] as i32 - (max_w as i32 - *total_weight as i32);
            for j in 0..selected.len() {
                let rem_item = selected[j];
                if tabu[rem_item] > 0 {
                    continue;
                }
                if min_weight > 0 {
                    let rem_w = weights[rem_item] as i32;
                    if rem_w < min_weight {
                        continue;
                    }
                }
                let rem_iv = interaction_sum[rem_item];
                let delta = new_iv - rem_iv - inter[new_item][rem_item];
                if delta > best_improvement {
                    best_improvement = delta;
                    best_swap = Some((i, j));
                }
            }
        }

        if let Some((u_idx, s_idx)) = best_swap {
            let new_item = unselected[u_idx];
            let rem_item = selected[s_idx];

            selected.swap_remove(s_idx);
            unselected.swap_remove(u_idx);
            selected.push(new_item);
            unselected.push(rem_item);

            is_selected[new_item] = true;
            is_selected[rem_item] = false;
            *total_weight = *total_weight + weights[new_item] - weights[rem_item];
            *current_value += best_improvement as i64;

            let new_row = &inter[new_item];
            let rem_row = &inter[rem_item];
            min_selected_iv = i32::MAX;
            for x in 0..n {
                interaction_sum[x] += new_row[x] - rem_row[x];
                if is_selected[x] {
                    min_selected_iv = min_selected_iv.min(interaction_sum[x]);
                }
            }

            tabu[new_item] = tabu_tenure;
            tabu[rem_item] = tabu_tenure;
        } else {
            break;
        }

        for t in tabu.iter_mut() {
            if *t > 0 {
                *t -= 1;
            }
        }
    }
}

fn compute_total_value(challenge: &Challenge, selected: &[usize]) -> u32 {
    let mut total: i64 = 0;
    for &i in selected {
        total += challenge.values[i] as i64;
    }
    for i in 0..selected.len() {
        let row = &challenge.interaction_values[selected[i]];
        for j in (i + 1)..selected.len() {
            total += row[selected[j]] as i64;
        }
    }
    if total < 0 {
        0
    } else {
        total.min(u32::MAX as i64) as u32
    }
}
// --- END EDITABLE REGION --- //
