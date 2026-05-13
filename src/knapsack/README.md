# Knapsack Challenge

## Problem

Quadratic Knapsack: select a subset of items that maximises total value (individual values + pairwise interaction values) without exceeding a weight limit.

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_items: usize,
    pub weights: Vec<u32>,              // per-item weight, each in [1, 10]
    pub values: Vec<u32>,               // per-item value (currently all 0 — objective is interaction-only)
    pub interaction_values: Vec<Vec<i32>>, // symmetric; interaction_values[i][j] in [0, 1000] (Jaccard similarity × 1000)
    pub max_weight: u32,                // budget% of total weight (budget is a track parameter)
}

pub struct Solution {
    pub items: Vec<usize>,  // indices of selected items (0-based, no duplicates)
}
```

## Value Calculation

```rust
total_value = selected.iter().map(|&i| values[i]).sum()
            + pairs(selected).map(|(i, j)| interaction_values[i][j]).sum()  // i < j
```


## Feasibility Constraints

- All item indices must be valid (`< num_items`) and unique.
- `selected.iter().map(|&i| weights[i]).sum() <= max_weight`

## Solver Interface

```rust
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()>
```

- Call `save_solution()` whenever you find an improved solution — only the last call is kept, and the solver may be killed at any time.
- Use `challenge.seed` for any randomness to keep results reproducible.
- Single-threaded only (no `std::thread`, `rayon`, etc.).

## Exact Method Signatures

These are the actual Rust signatures available on the types above.

### `Challenge` methods

```rust
// Validate solution and return total value (individual + pairwise interaction values)
// Checks weight constraint and item validity.
// Returns Err if any constraint is violated.
pub fn evaluate_total_value(&self, solution: &Solution) -> Result<u32>
```

No other public helper methods are available on `Challenge`. You have direct access to all fields listed in the Types section — `weights`, `values`, `interaction_values`, `max_weight`, `num_items`.

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `std::*` (collections, time, etc.).
