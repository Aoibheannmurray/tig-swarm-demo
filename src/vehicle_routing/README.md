# Vehicle Routing Challenge

## Problem

Vehicle Routing Problem with Time Windows (VRPTW): design routes for a fleet of capacity-limited vehicles to serve every customer within their time window, minimising **total travel distance**.

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_nodes: usize,                  // including depot (node 0)
    pub demands: Vec<i32>,                 // demands[0] = 0 (depot); customers in [1, 35]
    pub node_positions: Vec<(i32, i32)>,   // on a 1000×1000 grid; depot at (500, 500)
    pub distance_matrix: Vec<Vec<i32>>,    // Euclidean distances, rounded to integer
    pub max_capacity: i32,                 // always 200
    pub fleet_size: usize,                 // max number of routes allowed
    pub service_time: i32,                 // always 10; time spent at each customer after arrival
    pub ready_times: Vec<i32>,             // earliest allowed arrival per node
    pub due_times: Vec<i32>,               // latest allowed arrival per node
}

pub struct Solution {
    pub routes: Vec<Vec<usize>>,
    // each route is a sequence of node indices, must start and end with 0 (depot)
    // e.g. [0, 5, 12, 3, 0]
}
```

## Time Model

Travel time equals distance (1 unit distance = 1 unit time). For each route, time starts at 0 and advances as follows:

1. Travel to next node: `time += distance_matrix[current][next]`
2. If `time < ready_times[next]`, wait: `time = ready_times[next]`
3. Arrival must satisfy: `time <= due_times[next]`
4. Service: `time += service_time`
5. After the last customer, return to depot: `time += distance_matrix[last][0]`, must satisfy `time <= due_times[0]`

## Feasibility Constraints

1. `routes.len() <= fleet_size`.
2. Each route starts and ends at node 0 (depot) and visits at least one customer.
3. Every non-depot node is visited exactly once across all routes.
4. Total demand per route `<= max_capacity`.
5. Time window respected at every node (see time model above).
6. Vehicle returns to depot before `due_times[0]`.

## Scoring

Your total distance is compared against a Solomon I1 insertion heuristic baseline:

```
quality = (baseline_distance − your_distance) / baseline_distance × 1,000,000
```

Clamped to ±10,000,000. Higher is better (shorter distance = higher quality). Zero means matching the baseline.

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
// Validate solution and return total distance. Checks all constraints (capacity,
// time windows, fleet size, node coverage). Returns Err with a descriptive
// message if any constraint is violated.
pub fn evaluate_total_distance(&self, solution: &Solution) -> Result<i32>
```

No other public helper methods are available on `Challenge`. You have direct access to all fields listed in the Types section — use them to build and evaluate routes yourself.

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `std::*` (collections, time, etc.).
