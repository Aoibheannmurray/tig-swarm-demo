// initial_algorithms/energy_arbitrage/seeds/greedy.rs
//
// Simple SEED algorithm for the energy_arbitrage challenge: a self-contained,
// flow-aware greedy arbitrage policy.
//
// This is deliberately written out in full (rather than calling the built-in
// baseline) so the whole algorithm — the price-threshold trading rule AND the
// network-feasibility repair — is visible to a model that picks the seed up to
// improve. That repair is the load-bearing part: `take_step` aborts the whole
// rollout if an action overloads a transmission line OR leaves a battery's
// charge/discharge bounds, so a trading rule alone is not feasible. Here we:
//   1. charge when the current day-ahead price is below the look-ahead average
//      (and discharge when above), clamped to each battery's action bounds;
//   2. project the action back to flow-feasibility by repeatedly softening the
//      most-violated line, then (if needed) binary-searching a global scale.
//
// Natural refinements for a weaker model: better price forecasting / lookahead,
// state-of-charge planning, smarter congestion handling, or a proper economic
// dispatch instead of the per-battery threshold rule.
//
// Note: `grid_optimize` may be called only once per process — this does exactly
// that and saves the resulting schedule.

use super::*;
use super::constants::{
    EPS_BASELINE as EPS, EPS_FLOW, GLOBAL_SCALE_BSEARCH_ITERS, MAX_FLOW_ADJUST_ITERS,
};
use anyhow::{anyhow, Result};
use serde_json::{Map, Value};

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let solution = challenge.grid_optimize(&policy)?;
    save_solution(&solution)?;
    Ok(())
}

// ── Trading policy ─────────────────────────────────────────────────

fn policy(challenge: &Challenge, state: &State) -> Result<Vec<f64>> {
    let time_step = state.time_step;
    let horizon = 12; // Look ahead 3 hours

    let mut best_action = vec![0.0; challenge.num_batteries];

    let da_prices = &challenge.market.day_ahead_prices;
    let current_da = da_prices[time_step][0]; // Approximate with node 0

    // Average future day-ahead price over the look-ahead window.
    let end_step = (time_step + horizon).min(challenge.num_steps);
    let mut future_sum = 0.0;
    let mut future_count = 0.0;
    for t in time_step + 1..end_step {
        future_sum += da_prices[t][0];
        future_count += 1.0;
    }
    let future_avg = if future_count > 0.0 {
        future_sum / future_count
    } else {
        current_da
    };

    // Crude future-congestion signal: many high-net-load steps ahead suggest
    // prices will spike, so trade more aggressively around them.
    let mut future_congestion_risk = 0.0;
    for t in time_step + 1..end_step {
        let net_load: f64 = challenge.exogenous_injections[t].iter().sum();
        if net_load > 100.0 {
            future_congestion_risk += 1.0;
        }
    }

    for i in 0..challenge.num_batteries {
        let (min_bound, max_bound) = state.action_bounds[i];
        let threshold_adjust = future_congestion_risk * 2.0;
        if current_da < future_avg - 5.0 - threshold_adjust {
            best_action[i] = min_bound; // charge
        } else if current_da > future_avg + 5.0 + threshold_adjust {
            best_action[i] = max_bound; // discharge
        } else {
            best_action[i] = 0.0;
        }
    }

    enforce_flow_feasibility(challenge, state, best_action)
}

// ── Network-feasibility repair ─────────────────────────────────────

#[derive(Clone, Copy)]
struct Violation {
    line: usize,
    flow: f64,
    amount: f64,
}

fn compute_flows(challenge: &Challenge, state: &State, action: &[f64]) -> Vec<f64> {
    let injections = challenge.compute_total_injections(state, action);
    (0..challenge.network.num_lines)
        .map(|l| {
            (0..challenge.network.num_nodes)
                .map(|k| challenge.network.ptdf[l][k] * injections[k])
                .sum::<f64>()
        })
        .collect()
}

fn most_violated_line(challenge: &Challenge, flows: &[f64]) -> Option<Violation> {
    let mut best: Option<Violation> = None;
    for (l, &flow) in flows.iter().enumerate() {
        let limit = challenge.network.flow_limits[l];
        let violation = flow.abs() - limit;
        if violation > EPS_FLOW * limit {
            let candidate = Violation { line: l, flow, amount: violation };
            match best {
                Some(current) if candidate.amount <= current.amount => {}
                _ => best = Some(candidate),
            }
        }
    }
    best
}

fn is_flow_feasible(challenge: &Challenge, state: &State, action: &[f64]) -> bool {
    most_violated_line(challenge, &compute_flows(challenge, state, action)).is_none()
}

/// Scale down the battery actions that push the most-violated line further over
/// its limit, just enough to relieve the overload. Returns whether it changed.
fn soften_most_violated_line(
    challenge: &Challenge,
    violation: Violation,
    action: &mut [f64],
) -> bool {
    let line = violation.line;
    let signed_direction = violation.flow.signum();
    if signed_direction.abs() <= EPS {
        return false;
    }

    let mut worsening_indices = Vec::new();
    let mut worsening_strength = 0.0;
    for (i, battery) in challenge.batteries.iter().enumerate() {
        let contribution = challenge.network.ptdf[line][battery.node] * action[i];
        let signed_contribution = signed_direction * contribution;
        if signed_contribution > EPS {
            worsening_strength += signed_contribution;
            worsening_indices.push(i);
        }
    }

    if worsening_indices.is_empty() || worsening_strength <= EPS {
        return false;
    }

    let keep = (1.0 - violation.amount / worsening_strength).clamp(0.0, 1.0);
    if (1.0 - keep).abs() <= EPS {
        return false;
    }
    for i in worsening_indices {
        action[i] *= keep;
    }
    true
}

fn enforce_flow_feasibility(
    challenge: &Challenge,
    state: &State,
    mut action: Vec<f64>,
) -> Result<Vec<f64>> {
    for _ in 0..MAX_FLOW_ADJUST_ITERS {
        let flows = compute_flows(challenge, state, &action);
        let Some(violation) = most_violated_line(challenge, &flows) else {
            return Ok(action);
        };
        if !soften_most_violated_line(challenge, violation, &mut action) {
            break;
        }
    }

    if is_flow_feasible(challenge, state, &action) {
        return Ok(action);
    }

    // Last resort: binary-search a global scale in [0, 1] that is feasible.
    let zero = vec![0.0; action.len()];
    if !is_flow_feasible(challenge, state, &zero) {
        return Err(anyhow!(
            "grid is infeasible even with zero battery actions"
        ));
    }
    let base = action;
    let mut low = 0.0;
    let mut high = 1.0;
    for _ in 0..GLOBAL_SCALE_BSEARCH_ITERS {
        let mid = 0.5 * (low + high);
        let scaled: Vec<f64> = base.iter().map(|u| mid * u).collect();
        if is_flow_feasible(challenge, state, &scaled) {
            low = mid;
        } else {
            high = mid;
        }
    }
    Ok(base.into_iter().map(|u| low * u).collect())
}
