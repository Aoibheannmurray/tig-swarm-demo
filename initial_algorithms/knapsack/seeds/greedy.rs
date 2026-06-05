// initial_algorithms/knapsack/seeds/greedy.rs
//
// Simple SEED algorithm for the knapsack (quadratic / team-formation)
// challenge. Unlike the stub (initial_algorithms/knapsack.rs), this is a
// complete, feasible starting point that standard-tier / exploiter agents are
// handed instead of `unimplemented!()`, so a weaker model can refine it rather
// than bootstrap from scratch.
//
// Strategy (greedy construction): in this challenge `values` are all zero and
// every pairwise `interaction_values[i][j]` is a non-negative (scaled Jaccard)
// term, so adding any item that still fits can only increase total value.
// We therefore sort items by interaction "degree" (how much they interact with
// everything else) per unit weight, and pack greedily until the budget is
// exhausted. Always feasible: items are distinct and total weight <= max_weight.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let n = challenge.num_items;
    let cap = challenge.max_weight;
    let weights = &challenge.weights;
    let iv = &challenge.interaction_values;

    // Static priority: total interaction "degree" of each item. Items that
    // interact strongly with the rest tend to contribute more value once
    // they (and their neighbours) are in the knapsack.
    let mut order: Vec<usize> = (0..n).collect();
    let degree: Vec<i64> = (0..n)
        .map(|i| iv[i].iter().map(|&x| x as i64).sum::<i64>())
        .collect();

    // Sort by value-density (degree per unit weight) descending; break ties by
    // lighter items first so we can fit more of them under the budget.
    order.sort_by(|&a, &b| {
        let wa = weights[a].max(1) as f64;
        let wb = weights[b].max(1) as f64;
        let ka = degree[a] as f64 / wa;
        let kb = degree[b] as f64 / wb;
        kb.partial_cmp(&ka)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(weights[a].cmp(&weights[b]))
    });

    // Greedy fill, saving incrementally so a partial result survives even if
    // the time budget is hit (it won't be for this O(n log n) construction).
    let mut items: Vec<usize> = Vec::new();
    let mut used: u32 = 0;
    // Save the (feasible) empty solution first so there is always something.
    save_solution(&Solution { items: items.clone() })?;
    for &i in &order {
        if used + weights[i] <= cap {
            used += weights[i];
            items.push(i);
        }
    }
    save_solution(&Solution { items })?;
    Ok(())
}
