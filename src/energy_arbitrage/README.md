# Energy Arbitrage

## What You Are Solving

You control a fleet of batteries placed across an electrical grid. At each 15-minute time step, your policy decides how much to charge or discharge each battery. You earn money by buying cheap energy and selling it when prices are high. Your total profit must beat a baseline policy to score positively.

The core difficulty is the combination of:
1. **Unknown future prices** — real-time prices are stochastic and revealed one step at a time.
2. **Network flow constraints** — battery actions affect line flows; violating a line limit makes the step invalid and terminates the rollout.
3. **Battery physics** — efficiency losses, SOC limits, and power limits couple decisions across time.


## Required Function Signature

```rust
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let solution = challenge.grid_optimize(&policy)?;
    save_solution(&solution)?;
    Ok(())
}

pub fn policy(challenge: &Challenge, state: &State) -> Result<Vec<f64>> {
    // TODO: implement your policy here
    Err(anyhow!("Not implemented"))
}
```

`grid_optimize` runs the full episode by calling your policy once per time step. **It may only be called once per challenge instance.** If your policy returns `Err`, the rollout terminates immediately and the solution is invalid.


## Key Types

### `Challenge` — static problem data (available throughout the episode)

```
challenge.num_steps          : usize        — total time steps H (96 or 192)
challenge.num_batteries      : usize        — number of batteries m
challenge.network            : Network      — grid topology, PTDFs, flow limits
challenge.batteries          : Vec<Battery> — battery specs (see below)
challenge.exogenous_injections: Vec<Vec<f64>> — [H][n] pre-generated nodal injections (MW)
challenge.market.day_ahead_prices: Vec<Vec<f64>> — [H][n] day-ahead prices ($/MWh), fully known
```

### `State` — dynamic information revealed each step

```
state.time_step     : usize           — current step index (0-based)
state.socs          : Vec<f64>        — current state-of-charge per battery (MWh)
state.rt_prices     : Vec<f64>        — real-time nodal prices THIS step ($/MWh)
state.exogenous_injections: Vec<f64>  — exogenous nodal injections THIS step (MW)
state.action_bounds : Vec<(f64, f64)> — (u_min, u_max) per battery (MW)
state.total_profit  : f64             — cumulative profit so far ($)
```

### `Battery`

```
battery.node              : usize  — grid node where this battery is located
battery.capacity_mwh      : f64    — energy capacity Ē_b (MWh)
battery.power_charge_mw   : f64    — max charge power P̄^c_b (MW)
battery.power_discharge_mw: f64    — max discharge power P̄^d_b (MW)
battery.efficiency_charge : f64    — η^c = 0.95
battery.efficiency_discharge: f64  — η^d = 0.95
battery.soc_min_mwh       : f64    — E^min = 0.10 × Ē_b
battery.soc_max_mwh       : f64    — E^max = 0.90 × Ē_b
battery.soc_initial_mwh   : f64    — E_0 = 0.50 × Ē_b
```


## Actions

Your policy returns `Vec<f64>` of length `num_batteries`. Each element is a **signed power** value in MW:

- **Negative** → charge (battery draws from the grid)
- **Positive** → discharge (battery injects into the grid)
- **Zero** → idle

**Critical:** every action must satisfy `action_bounds[b].0 <= action[b] <= action_bounds[b].1`. Violating this causes an error and terminates the rollout. The bounds are pre-computed for you in `state.action_bounds` based on current SOCs and battery limits — just respect them.


## Profit Formula

Per step, for each battery $b$ with action $u_b$:

```
revenue   = u_b × rt_prices[battery.node] × 0.25        ($)
tx_cost   = 0.25 × |u_b| × 0.25                          ($)   [κ_tx = 0.25 $/MWh]
deg_cost  = 1.0 × (|u_b| × 0.25 / battery.capacity_mwh)²  ($)   [κ_deg=1.0, β=2.0]
profit_b  = revenue - tx_cost - deg_cost
```

Total step profit = sum over all batteries. Idle batteries (u=0) contribute nothing. The degradation cost scales with cycle depth relative to capacity, so it is small for modest actions.


## Network Constraint — The Hard Constraint

After computing total nodal injections (exogenous + battery actions, with node 0 as slack), line flows are:

```
flow[l] = sum over k of: network.ptdf[l][k] × injection[k]
```

Every line must satisfy `|flow[l]| <= network.flow_limits[l]`. **If any line is violated, the step fails and the episode ends with an error.** This is the most common cause of invalid solutions.

**How to stay feasible:**
- Use `challenge.compute_total_injections(state, &action)` to get the injection vector.
- Use `challenge.network.compute_flows(&injections)` to get line flows.
- Use `challenge.network.verify_flows(&flows)` to check feasibility before returning an action.
- Scale actions down if needed — returning zeros is always feasible (exogenous injections are pre-scaled to leave headroom).


## SOC Update (for your own planning)

After an action is applied, the SOC updates as:

```
charge_amount    = max(-u_b, 0.0) × η^c × Δt      [MWh stored]
discharge_amount = max(u_b,  0.0) / η^d × Δt      [MWh consumed from store]
new_soc = clamp(soc + charge_amount - discharge_amount, E^min, E^max)
```

Use `challenge.batteries[b].apply_action_to_soc(action, soc)` to compute this.


## Look-ahead Planning with `take_step`

For planning (e.g., simulating future scenarios, dynamic programming), use:

```rust
challenge.take_step(&state, &action, NextRTPrices::Override(your_price_forecast))
```

This validates the action and returns the next `State` without touching the hidden commitment chain used by `grid_optimize`. You can call this as many times as you like for offline planning.

RT prices are **policy-independent** — the actual prices in the real rollout are fully determined by the challenge seed before the episode starts. This means you can simulate the real rollout exactly if you know the prices at future steps. However, future RT prices are not directly accessible; you can forecast them using day-ahead prices as a guide.


## Scoring

```
quality = (total_profit - baseline_profit) / (baseline_profit + 1e-6)
```

The baseline is the better of:
1. A **greedy DA policy**: charge when current DA price is below a 3-hour look-ahead average minus a threshold; discharge when above.
2. A **conservative policy**: do nothing (zero actions every step).

Quality > 0 means you beat the baseline. The score is a fixed-point integer with 6 decimal places.


## Exact Method Signatures

These are the actual Rust signatures — use the exact return types shown.

### `Challenge` methods

```rust
// Compute total nodal injections (exogenous + battery actions, slack-balanced).
pub fn compute_total_injections(&self, state: &State, action: &[f64]) -> Vec<f64>

// Compute per-step profit for given action.
pub fn compute_profit(&self, state: &State, action: &[f64]) -> f64

// Simulate one step without commitment (for look-ahead planning).
// Validates action, returns Err if any constraint is violated.
pub fn take_step(&self, state: &State, action: &[f64], next_rt_prices: NextRTPrices) -> Result<State>

// Run the full rollout. Calls policy(challenge, state) at each step.
// May only be called ONCE per challenge instance.
pub fn grid_optimize(&self, policy: &dyn Fn(&Challenge, &State) -> Result<Vec<f64>>) -> Result<Solution>
```

### `Network` methods (accessed via `challenge.network`)

```rust
// Compute line flows from nodal injections. Returns Vec of length num_lines.
// NOTE: returns Vec<f64> directly, NOT Result.
pub fn compute_flows(&self, injections: &[f64]) -> Vec<f64>

// Check all line flows are within limits. Returns Ok(()) or Err.
pub fn verify_flows(&self, flows: &[f64]) -> Result<()>
```

### `Network` fields

```rust
pub num_nodes: usize
pub num_lines: usize
pub lines: Vec<(usize, usize)>        // (from_node, to_node)
pub flow_limits: Vec<f64>              // effective limits after congestion scaling
pub ptdf: Vec<Vec<f64>>               // PTDF matrix [num_lines][num_nodes]
pub slack_bus: usize
```

### `Battery` methods (accessed via `challenge.batteries[b]`)

```rust
// Apply action to SOC, return new SOC (clamped to bounds).
pub fn apply_action_to_soc(&self, action: f64, soc: f64) -> f64
```

### `NextRTPrices` enum (for `take_step`)

```rust
pub enum NextRTPrices {
    Override(Vec<f64>),    // provide your own price forecast
    Generate([u8; 32]),    // generate from seed (not useful for innovators)
}
```

### `Market` fields (accessed via `challenge.market`)

```rust
pub params: MarketParams               // volatility, jump_probability, tail_index
pub day_ahead_prices: Vec<Vec<f64>>    // [num_steps][num_nodes]
```

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `std::*` (collections, time, etc.).
