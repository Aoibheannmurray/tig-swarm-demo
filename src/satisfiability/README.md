# Satisfiability Challenge

## Problem

Boolean Satisfiability (3-CNF SAT): decide whether a Boolean formula in conjunctive normal form (each clause has exactly 3 literals) can be satisfied. If it can, find a satisfying assignment.

## Types

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_variables: usize,
    pub clauses: Vec<Vec<i32>>,   // 3-CNF, 1-indexed; positive = literal, negative = negated literal
}

pub struct Solution {
    pub variables: Vec<bool>,     // length == num_variables
}
```

Instances are parameterised by `Track { n_vars, ratio }`. The number of clauses is `floor(n_vars × ratio / 1000)`. The phase-transition ratio for random 3-SAT is approximately 4267 (i.e., `clauses ~ 4.267 × variables`). Lower ratios are mostly satisfiable; higher ratios are mostly unsatisfiable.

## Instance Generation

Each clause contains 3 literals. Variables are drawn uniformly from `[1, n_vars]` and each is independently negated with probability 0.5. Clauses may contain duplicate variables.

## Feasibility Constraints

- `variables.len()` must equal `num_variables`.
- Every clause must be satisfied: at least one literal in the clause must evaluate to true under the assignment.

## Scoring

SAT is the only challenge without an algorithmic baseline — scoring is binary per instance:

```
quality = 1,000,000   if all clauses satisfied
quality = 0           otherwise
```

Per-track scores are arithmetic means of per-instance quality, then shifted geometric mean across tracks. Higher is better. Since the score is binary, feasibility is everything — partial satisfaction scores zero.

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

There are no public helper methods on `Challenge` beyond what the evaluator uses internally. You have direct access to all fields:

```rust
challenge.seed           // [u8; 32] — use for reproducible randomness
challenge.num_variables  // usize
challenge.clauses        // Vec<Vec<i32>> — each clause has 3 literals; 1-indexed, negative = negated
```

### `Solution` construction

```rust
pub struct Solution {
    pub variables: Vec<bool>,  // length must equal num_variables
}

impl Solution {
    pub fn new() -> Self  // creates empty Solution
}
```

### Clause evaluation (for your own use)

A clause is satisfied if at least one literal evaluates to true:
```rust
// literal > 0: variables[literal.abs() - 1] must be true
// literal < 0: variables[literal.abs() - 1] must be false
```

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `std::*` (collections, time, etc.).
