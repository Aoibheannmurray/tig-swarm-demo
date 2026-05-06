use super::*;
// --- BEGIN EDITABLE REGION --- //
use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};
use anyhow::{anyhow, Result};
use rand::seq::SliceRandom;
use rand::{rngs::SmallRng, Rng, SeedableRng};
use serde_json::{Map, Value};
use std::cmp::Ordering;
use std::collections::HashMap;

#[derive(Serialize, Deserialize)]
pub struct Hyperparameters {
    pub slope_factor: f64,
    pub charge_threshold: f64,
    pub discharge_threshold: f64,
}

impl Default for Hyperparameters {
    fn default() -> Self {
        Self {
            slope_factor: 2.0,
            charge_threshold: 8.0,
            discharge_threshold: 6.0,
        }
    }
}

pub fn help() {
    println!("HYPERPARAMETERS:");
    println!("  slope_factor:        float range=[1.0, 4.0]  default=2.0  # intensity slope");
    println!("  charge_threshold:    float range=[6.0, 10.0] default=8.0  # $/MWh below DA-median to charge");
    println!("  discharge_threshold: float range=[4.0, 8.0]  default=6.0  # $/MWh above DA-median to discharge");
    println!();
    println!("Refines v12 by exposing slope_factor and thresholds for finer hyperparameter search around");
    println!("v12's winning corner. End-of-episode logic (window=24, factor=0) is hardcoded.");
}

fn parse_hp(h: &Option<Map<String, Value>>) -> Hyperparameters {
    let mut out = Hyperparameters::default();
    if let Some(m) = h {
        if let Some(v) = m.get("slope_factor").and_then(|v| v.as_f64()) {
            out.slope_factor = v;
        }
        if let Some(v) = m.get("charge_threshold").and_then(|v| v.as_f64()) {
            out.charge_threshold = v;
        }
        if let Some(v) = m.get("discharge_threshold").and_then(|v| v.as_f64()) {
            out.discharge_threshold = v;
        }
    }
    out
}

const LOOKAHEAD_STEPS: usize = 72;
const END_WINDOW: usize = 24;
const END_FACTOR: f64 = 0.0;

const MAX_FLOW_ADJUST_ITERS: usize = 64;
const GLOBAL_SCALE_BSEARCH_ITERS: usize = 32;
const EPS: f64 = 1e-12;
const FLOW_EPS: f64 = 1e-6;

#[derive(Clone, Copy)]
struct Violation {
    line: usize,
    flow: f64,
    amount: f64,
}

fn compute_flows(challenge: &Challenge, state: &State, action: &[f64]) -> Vec<f64> {
    let injections = challenge.compute_total_injections(state, action);
    challenge.network.compute_flows(&injections)
}

fn most_violated_line(challenge: &Challenge, flows: &[f64]) -> Option<Violation> {
    let mut best: Option<Violation> = None;
    for (l, &flow) in flows.iter().enumerate() {
        let limit = challenge.network.flow_limits[l];
        let violation = flow.abs() - limit;
        if violation > FLOW_EPS * limit {
            let cand = Violation { line: l, flow, amount: violation };
            match best {
                Some(cur) if cand.amount <= cur.amount => {}
                _ => best = Some(cand),
            }
        }
    }
    best
}

fn is_flow_feasible(challenge: &Challenge, state: &State, action: &[f64]) -> bool {
    let flows = compute_flows(challenge, state, action);
    most_violated_line(challenge, &flows).is_none()
}

fn soften_line(challenge: &Challenge, v: Violation, action: &mut [f64]) -> bool {
    let line = v.line;
    let dir = v.flow.signum();
    if dir.abs() <= EPS {
        return false;
    }
    let mut worsening = Vec::new();
    let mut worst_mass = 0.0;
    for (i, b) in challenge.batteries.iter().enumerate() {
        let contrib = challenge.network.ptdf[line][b.node] * action[i];
        let signed = dir * contrib;
        if signed > EPS {
            worst_mass += signed;
            worsening.push(i);
        }
    }
    if worsening.is_empty() || worst_mass <= EPS {
        return false;
    }
    let keep = (1.0 - v.amount / worst_mass).clamp(0.0, 1.0);
    if (1.0 - keep).abs() <= EPS {
        return false;
    }
    for i in worsening {
        action[i] *= keep;
    }
    true
}

fn enforce_feasible(challenge: &Challenge, state: &State, mut action: Vec<f64>) -> Result<Vec<f64>> {
    for _ in 0..MAX_FLOW_ADJUST_ITERS {
        let flows = compute_flows(challenge, state, &action);
        let Some(v) = most_violated_line(challenge, &flows) else {
            return Ok(action);
        };
        if !soften_line(challenge, v, &mut action) {
            break;
        }
    }
    if is_flow_feasible(challenge, state, &action) {
        return Ok(action);
    }
    let zero = vec![0.0; action.len()];
    if !is_flow_feasible(challenge, state, &zero) {
        return Err(anyhow!("Grid infeasible even with zero battery actions"));
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

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let hp = parse_hp(hyperparameters);
    let policy_fn = move |ch: &Challenge, st: &State| -> Result<Vec<f64>> {
        policy_with_hp(ch, st, &hp)
    };
    let solution = challenge.grid_optimize(&policy_fn)?;
    save_solution(&solution)?;
    Ok(())
}

pub fn policy(challenge: &Challenge, state: &State) -> Result<Vec<f64>> {
    policy_with_hp(challenge, state, &Hyperparameters::default())
}

fn median_of(buf: &mut Vec<f64>) -> f64 {
    if buf.is_empty() {
        return 0.0;
    }
    buf.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = buf.len();
    if n % 2 == 1 {
        buf[n / 2]
    } else {
        0.5 * (buf[n / 2 - 1] + buf[n / 2])
    }
}

fn policy_with_hp(challenge: &Challenge, state: &State, hp: &Hyperparameters) -> Result<Vec<f64>> {
    let t = state.time_step;
    let end = (t + 1 + LOOKAHEAD_STEPS).min(challenge.num_steps);
    let da = &challenge.market.day_ahead_prices;

    let remaining = challenge.num_steps.saturating_sub(t);
    let in_end_window = END_WINDOW > 0 && remaining <= END_WINDOW;
    let (discharge_th, charge_th, suppress_charge) = if in_end_window {
        let progress = (remaining as f64 / END_WINDOW as f64).clamp(0.0, 1.0);
        let scale = END_FACTOR + (1.0 - END_FACTOR) * progress;
        (hp.discharge_threshold * scale, hp.charge_threshold, true)
    } else {
        (hp.discharge_threshold, hp.charge_threshold, false)
    };

    let slope = hp.slope_factor.max(0.05);

    let mut action = vec![0.0; challenge.num_batteries];
    let mut buf: Vec<f64> = Vec::with_capacity(LOOKAHEAD_STEPS);
    for (i, battery) in challenge.batteries.iter().enumerate() {
        let node = battery.node;
        buf.clear();
        for tt in (t + 1)..end {
            buf.push(da[tt][node]);
        }
        if buf.is_empty() {
            let (_, u_max) = state.action_bounds[i];
            action[i] = u_max;
            continue;
        }
        // Sort once and pick median + quartiles for an IQR-based volatility estimate.
        buf.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = buf.len();
        let reference = if n % 2 == 1 {
            buf[n / 2]
        } else {
            0.5 * (buf[n / 2 - 1] + buf[n / 2])
        };
        let q_lo = buf[((0.25 * (n - 1) as f64).round() as usize).min(n - 1)];
        let q_hi = buf[((0.75 * (n - 1) as f64).round() as usize).min(n - 1)];
        let iqr = (q_hi - q_lo).max(0.5);

        let current = state.rt_prices[node];
        let diff = current - reference;

        // SOC-aware threshold modulation: at high SOC, discharge threshold
        // shrinks (act more eagerly to discharge); at low SOC, charge
        // threshold shrinks. Linear interpolation around mid-SOC.
        let soc_range = (battery.soc_max_mwh - battery.soc_min_mwh).max(1e-6);
        let soc_norm = ((state.socs[i] - battery.soc_min_mwh) / soc_range).clamp(0.0, 1.0);
        let dt_factor = (1.5 - soc_norm).max(0.3);
        let ct_factor = (0.5 + soc_norm).max(0.3);

        // IQR-adaptive thresholds: low-volatility scenarios get smaller triggers so
        // we don't sit idle when the static threshold dwarfs the achievable spread.
        // Clamp to the configured value as an upper bound for high-vol scenarios.
        let dt_vol = (iqr * 0.40).min(discharge_th);
        let ct_vol = (iqr * 0.40).min(charge_th);

        let (u_min, u_max) = state.action_bounds[i];
        let dt = (dt_vol * dt_factor).max(0.15);
        let ct = (ct_vol * ct_factor).max(0.15);

        // Peak detection: if RT is near the lookahead window's extremes, fire at
        // full intensity regardless of the soft threshold — these are the rare
        // moments where we definitely beat the alternative.
        let max_da = buf[n - 1];
        let min_da = buf[0];
        let near_peak_high = diff > 0.0 && current >= 0.97 * max_da && current > reference;
        let near_peak_low = diff < 0.0 && current <= 1.03 * min_da && current < reference;

        if near_peak_high {
            action[i] = u_max;
        } else if near_peak_low && !suppress_charge {
            action[i] = u_min;
        } else if diff > dt {
            let span = (slope * dt).max(1e-6);
            let intensity = ((diff - dt) / span).clamp(0.0, 1.0);
            action[i] = intensity * u_max;
        } else if !suppress_charge && diff < -ct {
            let span = (slope * ct).max(1e-6);
            let intensity = ((-diff - ct) / span).clamp(0.0, 1.0);
            action[i] = intensity * u_min;
        }
    }

    enforce_feasible(challenge, state, action)
}

// --- END EDITABLE REGION --- //
