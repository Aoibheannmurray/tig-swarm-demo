use super::*;
use anyhow::{anyhow, Result};
use serde_json::{Map, Value};
use std::cell::RefCell;

const LOOKAHEAD_STEPS: usize = 12; // 3-hour DA window
const FLOW_MARGIN: f64 = 0.95; // 5 % head-room

// Gradient–ascent params
const GRAD_ITERS: usize = 20;
const STEP_U: f64 = 0.5;
const STEP_LAMBDA: f64 = 0.5;
const LAMBDA_MAX: f64 = 10.0;
const EPS: f64 = 1e-9;

// Cost / physics
const DELTA_T: f64 = 0.25;
const KAPPA_TX: f64 = 0.25;
const KAPPA_DEG: f64 = 1.0;
const BETA_DEG: f64 = 2.0;

// targeted relief
const RELIEF_ITERS: usize = 20;

thread_local! {
    static PERSISTENT_LAMBDAS: RefCell<Option<Vec<f64>>> = RefCell::new(None);
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    PERSISTENT_LAMBDAS.with(|cell| *cell.borrow_mut() = None);
    let solution = challenge.grid_optimize(&|c, s| policy(c, s))?;
    save_solution(&solution)?;
    Ok(())
}

pub fn policy(challenge: &Challenge, state: &State) -> Result<Vec<f64>> {
    let m = challenge.num_batteries;
    let h = challenge.num_steps;
    let t = state.time_step;
    let slack = challenge.network.slack_bus;
    let num_lines = challenge.network.num_lines;

    // --------------------------------------------------
    // 1. Marginal-profit gradient and bounds
    // --------------------------------------------------
    let mut profit_grad = vec![0.0; m];
    let mut u_min = vec![0.0; m];
    let mut u_max = vec![0.0; m];

    for (b_idx, battery) in challenge.batteries.iter().enumerate() {
        let node = battery.node;
        let p_now = state.rt_prices[node];

        let end = (t + LOOKAHEAD_STEPS + 1).min(h);
        let mut sum_da = 0.0;
        for tt in (t + 1)..end {
            sum_da += challenge.market.day_ahead_prices[tt][node];
        }
        let cnt = end.saturating_sub(t + 1);
        let p_future_avg = if cnt > 0 {
            sum_da / cnt as f64
        } else {
            p_now
        };

        let (bound_min, bound_max) = state.action_bounds[b_idx];
        u_min[b_idx] = bound_min;
        u_max[b_idx] = bound_max;

        let eta_tot = battery.efficiency_charge * battery.efficiency_discharge;
        let tx_term = KAPPA_TX * DELTA_T;
        let cap = battery.capacity_mwh;
        let u_nom = 0.5 * bound_min.abs().max(bound_max.abs());
        let deg_term = if cap > EPS {
            BETA_DEG * KAPPA_DEG * (u_nom * DELTA_T) / (cap * cap)
        } else {
            0.0
        };

        let delta_discharge = p_now - eta_tot * p_future_avg - tx_term - deg_term;
        let delta_charge = -delta_discharge;

        if delta_discharge > EPS {
            profit_grad[b_idx] = delta_discharge;
        } else if delta_charge > EPS {
            profit_grad[b_idx] = -delta_charge;
        } else {
            profit_grad[b_idx] = 0.0;
        }
    }

    if profit_grad.iter().all(|g| g.abs() < EPS) {
        return Ok(vec![0.0; m]);
    }

    // --------------------------------------------------
    // 2. PTDF contribution matrix Δ_{l,b}
    // --------------------------------------------------
    let mut delta = vec![vec![0.0; m]; num_lines];
    for (b_idx, battery) in challenge.batteries.iter().enumerate() {
        let node = battery.node;
        for l in 0..num_lines {
            delta[l][b_idx] =
                challenge.network.ptdf[l][node] - challenge.network.ptdf[l][slack];
        }
    }

    // base flows (no battery action)
    let inj_zero = challenge.compute_total_injections(state, &vec![0.0; m]);
    let base_flows = challenge.network.compute_flows(&inj_zero);
    let limits: Vec<f64> = challenge
        .network
        .flow_limits
        .iter()
        .map(|&lim| FLOW_MARGIN * lim)
        .collect();

    // --------------------------------------------------
    // 3. Dual projected gradient ascent
    // --------------------------------------------------
    let mut lambda: Vec<f64> = PERSISTENT_LAMBDAS.with(|cell| {
        let mut opt = cell.borrow_mut();
        match &*opt {
            Some(v) if v.len() == num_lines => v.clone(),
            _ => {
                *opt = Some(vec![0.0; num_lines]);
                vec![0.0; num_lines]
            }
        }
    });

    let mut u = vec![0.0; m];

    for _ in 0..GRAD_ITERS {
        // flows
        let mut flows = base_flows.clone();
        for l in 0..num_lines {
            let mut add = 0.0;
            for b in 0..m {
                add += delta[l][b] * u[b];
            }
            flows[l] += add;
        }

        // overflows
        let mut overflow_sign = vec![0.0; num_lines];
        let mut overflow_mag = vec![0.0; num_lines];
        for l in 0..num_lines {
            let f = flows[l];
            let lim = limits[l];
            if f > lim + EPS {
                overflow_sign[l] = 1.0;
                overflow_mag[l] = f - lim;
            } else if f < -lim - EPS {
                overflow_sign[l] = -1.0;
                overflow_mag[l] = -lim - f;
            }
        }

        // gradient wrt u
        let mut grad_u = profit_grad.clone();
        for b in 0..m {
            let mut penalty = 0.0;
            for l in 0..num_lines {
                if overflow_sign[l] != 0.0 {
                    penalty += lambda[l] * overflow_sign[l] * delta[l][b];
                }
            }
            grad_u[b] -= penalty;
        }

        // update u
        for b in 0..m {
            u[b] = (u[b] + STEP_U * grad_u[b]).clamp(u_min[b], u_max[b]);
        }

        // update duals
        for l in 0..num_lines {
            lambda[l] = (lambda[l] + STEP_LAMBDA * overflow_mag[l]).clamp(0.0, LAMBDA_MAX);
        }
    }

    // --------------------------------------------------
    // 4. Targeted per-battery flow-relief scaling
    // --------------------------------------------------
    for _ in 0..RELIEF_ITERS {
        // compute current flows
        let mut flows = base_flows.clone();
        for l in 0..num_lines {
            let mut add = 0.0;
            for b in 0..m {
                add += delta[l][b] * u[b];
            }
            flows[l] += add;
        }

        // find worst violation
        let mut worst_excess = 0.0;
        let mut worst_line: Option<usize> = None;
        let mut worst_sign = 0.0;
        for l in 0..num_lines {
            let f = flows[l];
            let lim = limits[l];
            if f > lim + EPS {
                let excess = f - lim;
                if excess > worst_excess {
                    worst_excess = excess;
                    worst_line = Some(l);
                    worst_sign = 1.0;
                }
            } else if f < -lim - EPS {
                let excess = -lim - f;
                if excess > worst_excess {
                    worst_excess = excess;
                    worst_line = Some(l);
                    worst_sign = -1.0;
                }
            }
        }

        // done if no violation
        if worst_line.is_none() {
            break;
        }

        let l = worst_line.unwrap();
        let sign = worst_sign; // direction of overflow
        let overflow = worst_excess;

        // total positive contribution towards the overflow
        let mut pos_contrib_total = 0.0;
        let mut contributors: Vec<usize> = Vec::new();
        for b in 0..m {
            let contrib = delta[l][b] * u[b] * sign;
            if contrib > EPS {
                pos_contrib_total += contrib;
                contributors.push(b);
            }
        }

        // If nothing contributes (shouldn't happen), exit
        if pos_contrib_total < EPS {
            break;
        }

        let k = (overflow / pos_contrib_total).clamp(0.0, 1.0); // uniform reduction share
        let factor = 1.0 - k;

        for &b in &contributors {
            u[b] *= factor;
            // keep inside bounds
            u[b] = u[b].clamp(u_min[b], u_max[b]);
        }
    }

    // --------------------------------------------------
    // 5. Final safety check
    // --------------------------------------------------
    let inj_final = challenge.compute_total_injections(state, &u);
    let flow_final = challenge.network.compute_flows(&inj_final);
    if challenge.network.verify_flows(&flow_final).is_err() {
        // fallback to conservative global scaling
        let mut flows = base_flows.clone();
        let mut delta_sum = vec![0.0; num_lines];
        for l in 0..num_lines {
            for b in 0..m {
                delta_sum[l] += delta[l][b] * u[b];
            }
            flows[l] += delta_sum[l];
        }

        let mut scale = 1.0;
        for l in 0..num_lines {
            let f = flows[l];
            let lim = limits[l];
            if f.abs() <= lim + EPS {
                continue;
            }
            let base = base_flows[l];
            let d = delta_sum[l];
            if d.abs() < EPS {
                continue;
            }
            let cand = if f > lim {
                (lim - base) / d
            } else {
                (-lim - base) / d
            };
            if cand < scale {
                scale = cand;
            }
        }
        if scale < 1.0 - 1e-6 && scale > 0.0 {
            for b in 0..m {
                u[b] = (u[b] * scale).clamp(u_min[b], u_max[b]);
            }
        } else {
            // ultimate safe-guard: zero vector
            return Ok(vec![0.0; m]);
        }

        // verify again
        let inj2 = challenge.compute_total_injections(state, &u);
        let flow2 = challenge.network.compute_flows(&inj2);
        if challenge.network.verify_flows(&flow2).is_err() {
            return Ok(vec![0.0; m]);
        }
    }

    // store duals
    PERSISTENT_LAMBDAS.with(|cell| {
        *cell.borrow_mut() = Some(lambda);
    });

    Ok(u)
}