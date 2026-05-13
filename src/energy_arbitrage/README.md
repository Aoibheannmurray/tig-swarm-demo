# Energy Arbitrage

## What You Are Solving

You control a fleet of batteries placed across an electrical grid. At each 15-minute time step, your policy decides how much to charge or discharge each battery. You earn money by buying cheap energy and selling it when prices are high. The objective is to **maximise total profit** over the episode.

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
    // Zero actions are always feasible (exogenous injections leave headroom).
    // Replace this with your strategy.
    Ok(vec![0.0; challenge.num_batteries])
}
```

`grid_optimize` runs the full episode by calling your policy once per time step. **It may only be called once per challenge instance.** If your policy returns `Err`, the rollout terminates immediately and the solution is invalid.


## Types

Static problem data (`Challenge`) is fixed for the episode. `State` is revealed one step at a time.

```rust
pub struct Challenge {
    pub seed: [u8; 32],
    pub num_steps: usize,                    // total time steps H (varies by track)
    pub num_batteries: usize,                // m
    pub network: Network,
    pub batteries: Vec<Battery>,
    pub exogenous_injections: Vec<Vec<f64>>, // [num_steps][num_nodes] MW
    pub market: Market,
}

pub struct State {
    pub time_step: usize,                    // 0-based
    pub socs: Vec<f64>,                      // state-of-charge per battery (MWh)
    pub rt_prices: Vec<f64>,                 // real-time nodal prices THIS step ($/MWh)
    pub exogenous_injections: Vec<f64>,      // nodal injections THIS step (MW)
    pub action_bounds: Vec<(f64, f64)>,      // (u_min, u_max) per battery (MW)
                                             // SOC + nameplate power only — NOT network-aware
    pub total_profit: f64,                   // cumulative profit so far ($)
}

pub struct Battery {
    pub node: usize,
    pub capacity_mwh: f64,                   // Ē_b — varies per instance
    pub power_charge_mw: f64,                // P̄^c_b
    pub power_discharge_mw: f64,             // P̄^d_b
    pub efficiency_charge: f64,              // η^c = 0.95 (fixed)
    pub efficiency_discharge: f64,           // η^d = 0.95 (fixed)
    pub soc_min_mwh: f64,                    // E^min = 0.10 × Ē_b
    pub soc_max_mwh: f64,                    // E^max = 0.90 × Ē_b
    pub soc_initial_mwh: f64,                // E_0   = 0.50 × Ē_b
}

pub struct Network {
    pub num_nodes: usize,                    // n
    pub num_lines: usize,                    // L
    pub lines: Vec<(usize, usize)>,          // (from_node, to_node)
    pub flow_limits: Vec<f64>,               // effective limits after congestion scaling (MW)
    pub ptdf: Vec<Vec<f64>>,                 // [num_lines][num_nodes]
    pub slack_bus: usize,
}

pub struct Market {
    pub params: MarketParams,                // volatility, jump_probability, tail_index
    pub day_ahead_prices: Vec<Vec<f64>>,     // [num_steps][num_nodes] $/MWh — fully known up front
}

pub struct Solution {
    pub schedule: Vec<Vec<f64>>,             // [num_steps][num_batteries] — produced by grid_optimize
}
```


## Actions

Your policy returns `Vec<f64>` of length `num_batteries`. Each element is a **signed power** value in MW:

- **Negative** → charge (battery draws from the grid)
- **Positive** → discharge (battery injects into the grid)
- **Zero** → idle

**Critical:** every action must satisfy `action_bounds[b].0 <= action[b] <= action_bounds[b].1`. Violating this causes an error and terminates the rollout. The bounds are pre-computed for you in `state.action_bounds` based on current SOCs and battery limits — just respect them.


## Profit Formula

Constants (fixed across all tracks):

- `Δt    = 0.25 h`     — step duration (15-minute slots)
- `κ_tx  = 0.25 $/MWh` — transaction cost
- `κ_deg = 1.0  $`     — degradation scale
- `β     = 2.0`        — degradation exponent

Per step, for each battery `b` with action `u_b` (MW) at price `λ_b = rt_prices[battery.node]` ($/MWh):

```
revenue   = u_b × λ_b × Δt
tx_cost   = κ_tx × |u_b| × Δt
deg_cost  = κ_deg × (|u_b| × Δt / capacity_mwh) ^ β
profit_b  = revenue − tx_cost − deg_cost
```

Total step profit = sum over all batteries. Idle batteries (u=0) contribute nothing. Degradation grows quadratically with cycle depth relative to capacity, so small/moderate actions pay almost nothing.


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


## Methods

```rust
// On Challenge
pub fn compute_total_injections(&self, state: &State, action: &[f64]) -> Vec<f64>
pub fn compute_profit(&self, state: &State, action: &[f64]) -> f64
// Simulate one step without commitment (look-ahead). Validates action; Err on violation.
pub fn take_step(&self, state: &State, action: &[f64], next_rt_prices: NextRTPrices) -> Result<State>
// Run the full rollout. MAY ONLY BE CALLED ONCE per challenge instance.
pub fn grid_optimize(&self, policy: &dyn Fn(&Challenge, &State) -> Result<Vec<f64>>) -> Result<Solution>

// On Network (challenge.network)
pub fn compute_flows(&self, injections: &[f64]) -> Vec<f64>   // returns Vec, NOT Result
pub fn verify_flows(&self, flows: &[f64]) -> Result<()>

// On Battery (challenge.batteries[b])
pub fn apply_action_to_soc(&self, action: f64, soc: f64) -> f64

pub enum NextRTPrices {
    Override(Vec<f64>),    // your own forecast for the next step (length = num_nodes)
    Generate([u8; 32]),    // seed-based; the real rollout uses a hidden seed, so this cannot reproduce its prices
}
```

### Available crates

`anyhow`, `serde`, `serde_json`, `rand` (SmallRng, SeedableRng, Rng), `rand_distr`, `ndarray`, `statrs`, `std::*` (collections, time, etc.).
