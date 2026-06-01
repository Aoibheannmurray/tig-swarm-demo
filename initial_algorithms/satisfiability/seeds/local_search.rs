// initial_algorithms/satisfiability/seeds/local_search.rs
//
// Simple SEED algorithm for the satisfiability (3-SAT) challenge. Unlike the
// stub, this is a complete, feasible starting point: it always produces an
// assignment of the correct length, and uses a bounded greedy local search
// (GSAT-style) to satisfy as many clauses as it can.
//
// Scoring is all-or-nothing (full QUALITY_PRECISION only if EVERY clause is
// satisfied, else 0), so this won't always score — but it gives a weaker model
// a working, structurally-correct algorithm to refine (better flip heuristics,
// random restarts, WalkSAT noise) rather than bootstrapping from a stub.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};
use std::time::Instant;

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let n = challenge.num_variables;
    let clauses = &challenge.clauses;

    // Start from an all-true assignment of the correct length (always feasible).
    let mut assign = vec![true; n];
    // Save immediately so there is always a valid-length solution on record.
    save_solution(&Solution { variables: assign.clone() })?;
    if n == 0 || clauses.is_empty() {
        return Ok(());
    }

    // var -> clauses it appears in (by index), so a flip only re-checks the
    // clauses it can affect.
    let mut var_clauses: Vec<Vec<usize>> = vec![Vec::new(); n];
    for (ci, clause) in clauses.iter().enumerate() {
        for &lit in clause {
            let v = (lit.abs() as usize).saturating_sub(1);
            if v < n {
                var_clauses[v].push(ci);
            }
        }
    }

    let lit_true = |lit: i32, a: &[bool]| -> bool {
        let v = (lit.abs() as usize) - 1;
        (lit > 0 && a[v]) || (lit < 0 && !a[v])
    };

    // sat_count[c] = number of satisfied literals in clause c.
    let mut sat_count: Vec<u32> = clauses
        .iter()
        .map(|cl| cl.iter().filter(|&&l| lit_true(l, &assign)).count() as u32)
        .collect();
    let mut unsatisfied: usize = sat_count.iter().filter(|&&c| c == 0).count();

    let mut best_assign = assign.clone();
    let mut best_unsat = unsatisfied;

    // Net change in #satisfied clauses if we flip variable v.
    let net_gain = |v: usize, a: &[bool], sc: &[u32]| -> i64 {
        let mut gain: i64 = 0;
        for &ci in &var_clauses[v] {
            // does this clause's count gain or lose a satisfied literal when v flips?
            let mut delta: i64 = 0;
            for &lit in &clauses[ci] {
                if (lit.abs() as usize) - 1 == v {
                    // this literal's truth toggles
                    delta += if lit_true(lit, a) { -1 } else { 1 };
                }
            }
            let before = sc[ci];
            let after = (before as i64 + delta).max(0) as u32;
            if before == 0 && after > 0 {
                gain += 1;
            } else if before > 0 && after == 0 {
                gain -= 1;
            }
        }
        gain
    };

    let deadline = Instant::now();
    let max_flips = 20 * clauses.len().max(n) + 1000;
    for _ in 0..max_flips {
        if unsatisfied == 0 {
            break;
        }
        // Time guard: bail out well before the per-instance budget.
        if deadline.elapsed().as_secs_f64() > 5.0 {
            break;
        }
        // Pick the first unsatisfied clause and flip its best variable.
        let Some(ci) = sat_count.iter().position(|&c| c == 0) else { break };
        let mut best_v = (clauses[ci][0].abs() as usize) - 1;
        let mut best_g = i64::MIN;
        for &lit in &clauses[ci] {
            let v = (lit.abs() as usize) - 1;
            let g = net_gain(v, &assign, &sat_count);
            if g > best_g {
                best_g = g;
                best_v = v;
            }
        }
        // Apply the flip and update affected clause counts.
        assign[best_v] = !assign[best_v];
        for &c in &var_clauses[best_v] {
            let new_count = clauses[c]
                .iter()
                .filter(|&&l| lit_true(l, &assign))
                .count() as u32;
            if sat_count[c] == 0 && new_count > 0 {
                unsatisfied -= 1;
            } else if sat_count[c] > 0 && new_count == 0 {
                unsatisfied += 1;
            }
            sat_count[c] = new_count;
        }
        if unsatisfied < best_unsat {
            best_unsat = unsatisfied;
            best_assign = assign.clone();
        }
    }

    save_solution(&Solution { variables: best_assign })?;
    Ok(())
}
