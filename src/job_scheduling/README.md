# Job Scheduling Challenge

## Problem

Flexible Job-Shop Scheduling: assign operations to machines and choose start times to minimise the **makespan** (finish time of the last operation across all jobs).

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_jobs: usize,
    pub num_machines: usize,
    pub num_operations: usize,               // number of distinct operation types
    pub jobs_per_product: Vec<usize>,         // jobs_per_product[p] = how many jobs use product p's route
    pub product_processing_times: Vec<Vec<HashMap<usize, u32>>>,
    // product_processing_times[product][op_index] = { machine_id -> processing_time }
    // each operation maps eligible machines to their processing time for that op
}

pub struct Solution {
    pub job_schedule: Vec<Vec<(usize, u32)>>,
    // job_schedule[job][op_index] = (machine_id, start_time)
}
```

## Job Ordering

Jobs in `job_schedule` are grouped by product in order: the first `jobs_per_product[0]` entries belong to product 0, the next `jobs_per_product[1]` to product 1, and so on. All jobs of the same product share the same operation sequence defined by `product_processing_times[product]`.

## Feasibility Constraints

1. `job_schedule.len()` must equal `num_jobs`.
2. Each job's schedule length must equal its product's number of operations.
3. Each operation must be assigned to one of its eligible machines (a key in the corresponding `HashMap`).
4. Operations within a job must respect precedence: `start_time[op] >= start_time[op-1] + processing_time[op-1]`.
5. No two operations on the same machine may overlap in time.

## Scoring

Your makespan is compared against a SOTA baseline (dispatching rules with random restarts):

```
quality = (sota_makespan − your_makespan) / sota_makespan × 1,000,000
```

Clamped to ±10,000,000. Higher is better (lower makespan = higher quality). Zero means matching the baseline.

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
// Validate solution and return makespan. Checks all constraints (operation
// precedence, machine eligibility, no overlapping operations on the same machine).
// Returns Err with a descriptive message if any constraint is violated.
pub fn evaluate_makespan(&self, solution: &Solution) -> Result<u32>
```

No other public helper methods are available on `Challenge`. You have direct access to all fields listed in the Types section. Use `product_processing_times[product][op_index]` (a `HashMap<usize, u32>`) to look up eligible machines and their processing times.

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `std::*` (collections, time, etc.).
