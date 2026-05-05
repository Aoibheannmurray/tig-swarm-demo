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

Instances are parameterised by `Track { n: usize, s: Scenario }`.

- `num_machines = n / 2 + 5`
- `num_operations = n / 2 + 5` (distinct op types; individual jobs may have more steps due to reentrance)
- Base processing times per operation type are drawn from `[1, 200]`, then scaled per machine by a speed factor in `[0.8, 1.2]`.

## Job Ordering

Jobs in `job_schedule` are grouped by product in order: the first `jobs_per_product[0]` entries belong to product 0, the next `jobs_per_product[1]` to product 1, and so on. All jobs of the same product share the same operation sequence defined by `product_processing_times[product]`.

## Scenarios

Each scenario controls operation flexibility (how many machines each op is eligible for), reentrance (an operation type appearing multiple times in a route), flow structure, and product diversity.

| Scenario | Flexibility | Reentrance | Flow structure | Product mix |
|---|---|---|---|---|
| `FLOW_SHOP` | 1 machine/op | moderate | sequential | few products |
| `HYBRID_FLOW_SHOP` | ~3 machines/op | moderate | sequential | few products |
| `JOB_SHOP` | 1 machine/op | none | random permutation | many products |
| `FJSP_MEDIUM` | ~3 machines/op | moderate | mixed | many products |
| `FJSP_HIGH` | ~all machines/op | none | fully random | many products |

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
