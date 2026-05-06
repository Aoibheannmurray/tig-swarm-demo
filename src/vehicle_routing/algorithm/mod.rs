use super::*;
// --- BEGIN EDITABLE REGION --- //
use anyhow::Result;
use serde_json::{Map, Value};
use std::time::{Duration, Instant};

pub fn help() {
    println!("Solomon I1 insertion + intra-route 2-opt + inter-route relocate (time-bounded)");
}

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let start = Instant::now();
    let deadline = start + Duration::from_millis(4500);

    let n = challenge.num_nodes;
    let dm = &challenge.distance_matrix;
    let st = challenge.service_time;
    let rt = &challenge.ready_times;
    let dt = &challenge.due_times;
    let demands = &challenge.demands;
    let cap = challenge.max_capacity;

    // ── Solomon I1 sequential insertion construction ────────────────
    // Seed each route with the unvisited node farthest from depot, then
    // repeatedly insert the customer with best (c1, c2) cost.
    let mut routes: Vec<Vec<usize>> = Vec::new();
    let mut nodes: Vec<usize> = (1..n).collect();
    nodes.sort_by(|&a, &b| dm[0][a].cmp(&dm[0][b]));
    let mut remaining: Vec<bool> = vec![true; n];
    remaining[0] = false;

    while let Some(seed) = nodes.pop() {
        if !remaining[seed] {
            continue;
        }
        remaining[seed] = false;
        let mut route = vec![0, seed, 0];
        let mut route_demand = demands[seed];

        loop {
            let candidates: Vec<usize> = (0..n)
                .filter(|&v| remaining[v] && route_demand + demands[v] <= cap)
                .collect();
            if candidates.is_empty() {
                break;
            }
            match find_best_insertion(&route, &candidates, dm, st, rt, dt) {
                Some((v, pos)) => {
                    remaining[v] = false;
                    route_demand += demands[v];
                    route.insert(pos, v);
                }
                None => break,
            }
        }
        routes.push(route);
    }

    save_solution(&Solution { routes: routes.clone() })?;

    // Per-route demands (needed for inter-route capacity checks).
    let mut route_demands: Vec<i32> = routes
        .iter()
        .map(|r| r.iter().map(|&v| demands[v]).sum::<i32>())
        .collect();

    // ── Local-search loop: 2-opt + relocate, time-bounded ────────────
    let mut iter_pass = 0;
    loop {
        if Instant::now() >= deadline {
            break;
        }
        let mut improved = false;

        // Intra-route 2-opt.
        for r in 0..routes.len() {
            if Instant::now() >= deadline {
                break;
            }
            if two_opt_route(&mut routes[r], dm, st, rt, dt) {
                improved = true;
            }
        }

        if Instant::now() >= deadline {
            break;
        }

        // Inter-route relocate.
        if relocate_inter(&mut routes, &mut route_demands, dm, st, rt, dt, demands, cap, deadline) {
            improved = true;
        }

        if Instant::now() >= deadline {
            break;
        }

        // Inter-route swap.
        if swap_inter(&mut routes, &mut route_demands, dm, st, rt, dt, demands, cap, deadline) {
            improved = true;
        }

        if improved {
            save_solution(&Solution { routes: routes.clone() })?;
        } else {
            break;
        }
        iter_pass += 1;
        let _ = iter_pass;
    }

    Ok(())
}

// ── Solomon I1 insertion-cost helper ────────────────────────────────
fn find_best_insertion(
    route: &[usize],
    candidates: &[usize],
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
) -> Option<(usize, usize)> {
    let alpha1: i32 = 1;
    let alpha2: i32 = 0;
    let lambda: i32 = 1;

    let mut best_c2: Option<i32> = None;
    let mut best: Option<(usize, usize)> = None;

    // Pre-compute earliest service times along the current route once;
    // this avoids the O(route_len^2) feasibility re-walk per candidate.
    let mut earliest = vec![0i32; route.len()];
    {
        let mut t = 0i32;
        let mut prev = route[0];
        for k in 1..route.len() {
            t = (t + dm[prev][route[k]]).max(rt[route[k]]);
            earliest[k] = t;
            t += st;
            prev = route[k];
        }
    }

    for &u in candidates {
        let mut best_c1: Option<i32> = None;
        let mut t = 0i32;
        let mut prev = route[0];
        for pos in 1..route.len() {
            let next = route[pos];
            // Arrival at u if inserted at position `pos`.
            let arrive_u = (t + dm[prev][u]).max(rt[u]);
            if arrive_u > dt[u] {
                t = (t + dm[prev][next]).max(rt[next]) + st;
                prev = next;
                continue;
            }
            // Forward feasibility: walk from `next` onwards with shifted time.
            let mut ok = true;
            let mut tt = arrive_u + st;
            let mut pp = u;
            for k in pos..route.len() {
                let nx = route[k];
                tt = (tt + dm[pp][nx]).max(rt[nx]);
                if tt > dt[nx] {
                    ok = false;
                    break;
                }
                tt += st;
                pp = nx;
            }
            if !ok {
                t = (t + dm[prev][next]).max(rt[next]) + st;
                prev = next;
                continue;
            }
            // Solomon c1 / c2.
            let c11 = dm[prev][u] + dm[u][next] - dm[prev][next];
            let new_arrival_next = (arrive_u + st + dm[u][next]).max(rt[next]);
            let c12 = new_arrival_next - earliest[pos];
            let c1 = -(alpha1 * c11 + alpha2 * c12);
            let c2 = lambda * dm[0][u] + c1;
            if best_c1.is_none_or(|x| c1 > x) && best_c2.is_none_or(|x| c2 > x) {
                best_c1 = Some(c1);
                best_c2 = Some(c2);
                best = Some((u, pos));
            }
            t = (t + dm[prev][next]).max(rt[next]) + st;
            prev = next;
        }
    }
    best
}

// ── Intra-route 2-opt with feasibility check ────────────────────────
fn two_opt_route(
    route: &mut Vec<usize>,
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
) -> bool {
    if route.len() < 5 {
        return false;
    }
    let mut any = false;
    loop {
        let nlen = route.len();
        let mut best_delta: i64 = 0;
        let mut best_ij: Option<(usize, usize)> = None;
        for i in 0..nlen - 3 {
            for j in i + 2..nlen - 1 {
                let a = route[i];
                let b = route[i + 1];
                let c = route[j];
                let d = route[j + 1];
                let delta =
                    (dm[a][c] as i64 + dm[b][d] as i64) - (dm[a][b] as i64 + dm[c][d] as i64);
                if delta < best_delta {
                    let mut cand = route.clone();
                    cand[i + 1..=j].reverse();
                    if route_feasible(&cand, dm, st, rt, dt) {
                        best_delta = delta;
                        best_ij = Some((i, j));
                    }
                }
            }
        }
        if let Some((i, j)) = best_ij {
            route[i + 1..=j].reverse();
            any = true;
        } else {
            break;
        }
    }
    any
}

// ── Pre-computed time bounds per route ───────────────────────────────
// earliest[k] = earliest arrival time at route[k] given a depot start
// at time 0.  latest[k] = latest arrival at route[k] consistent with
// every downstream node's due-time and a feasible depot return.
fn compute_route_times(
    route: &[usize],
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
    earliest: &mut Vec<i32>,
    latest: &mut Vec<i32>,
) {
    let l = route.len();
    earliest.clear();
    earliest.resize(l, 0);
    latest.clear();
    latest.resize(l, 0);

    let mut t = 0i32;
    earliest[0] = 0;
    for k in 1..l {
        let prev = route[k - 1];
        let nd = route[k];
        t = (t + dm[prev][nd]).max(rt[nd]);
        earliest[k] = t;
        t += st;
    }

    latest[l - 1] = dt[route[l - 1]];
    for k in (0..l - 1).rev() {
        let nx = route[k + 1];
        // From route[k], depart by latest[k+1] - dm[route[k]][nx], so arrival by
        // (latest[k+1] - dm - st), capped by dt[route[k]].
        let bound = latest[k + 1] - dm[route[k]][nx] - st;
        latest[k] = bound.min(dt[route[k]]);
    }
}

// O(1) feasibility check: is inserting v at position `pos` in this route
// time-window-feasible?  Requires earliest/latest precomputed for the
// route in its current form. Pass `pos` ∈ [1, route.len()].
fn insert_feasible(
    route: &[usize],
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
    earliest: &[i32],
    latest: &[i32],
    pos: usize,
    v: usize,
) -> bool {
    let prev = route[pos - 1];
    let depart_prev = earliest[pos - 1] + st_or_zero(prev, st);
    let arrive_v = (depart_prev + dm[prev][v]).max(rt[v]);
    if arrive_v > dt[v] {
        return false;
    }
    if pos >= route.len() {
        // Shouldn't happen — depot is the last node.
        return true;
    }
    let nx = route[pos];
    let arrive_next = (arrive_v + st + dm[v][nx]).max(rt[nx]);
    arrive_next <= latest[pos]
}

// O(1) feasibility check: is replacing route[pos] with `u` time-window-
// feasible?
fn replace_feasible(
    route: &[usize],
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
    earliest: &[i32],
    latest: &[i32],
    pos: usize,
    u: usize,
) -> bool {
    let prev = route[pos - 1];
    let depart_prev = earliest[pos - 1] + st_or_zero(prev, st);
    let arrive_u = (depart_prev + dm[prev][u]).max(rt[u]);
    if arrive_u > dt[u] {
        return false;
    }
    if pos + 1 >= route.len() {
        return arrive_u <= dt[u];
    }
    let nx = route[pos + 1];
    let arrive_next = (arrive_u + st + dm[u][nx]).max(rt[nx]);
    arrive_next <= latest[pos + 1]
}

// Helper: depot has no service time in standard VRPTW; for non-depot
// nodes, service time applies after arrival.
fn st_or_zero(node: usize, st: i32) -> i32 {
    if node == 0 {
        0
    } else {
        st
    }
}

// ── Inter-route relocate with O(1) feasibility checks ────────────────
fn relocate_inter(
    routes: &mut Vec<Vec<usize>>,
    route_demands: &mut Vec<i32>,
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
    demands: &[i32],
    cap: i32,
    deadline: Instant,
) -> bool {
    let mut any = false;
    let mut local = true;
    let mut earliest_per: Vec<Vec<i32>> = Vec::new();
    let mut latest_per: Vec<Vec<i32>> = Vec::new();
    while local {
        local = false;
        if Instant::now() >= deadline {
            break;
        }
        // Recompute time bounds for every route.
        earliest_per.clear();
        latest_per.clear();
        for r in 0..routes.len() {
            let mut e: Vec<i32> = Vec::new();
            let mut l: Vec<i32> = Vec::new();
            compute_route_times(&routes[r], dm, st, rt, dt, &mut e, &mut l);
            earliest_per.push(e);
            latest_per.push(l);
        }
        for r in 0..routes.len() {
            if routes[r].len() <= 2 {
                continue;
            }
            let mut p = 1usize;
            while p < routes[r].len() - 1 {
                if Instant::now() >= deadline {
                    return any;
                }
                let v = routes[r][p];
                let prev = routes[r][p - 1];
                let next = routes[r][p + 1];
                let removal_save =
                    dm[prev][v] as i64 + dm[v][next] as i64 - dm[prev][next] as i64;
                if removal_save <= 0 {
                    p += 1;
                    continue;
                }
                let mut best_delta: i64 = 0;
                let mut best_target: Option<(usize, usize)> = None;
                for r2 in 0..routes.len() {
                    if r2 == r {
                        continue;
                    }
                    if route_demands[r2] + demands[v] > cap {
                        continue;
                    }
                    let route2 = &routes[r2];
                    for pos in 1..route2.len() {
                        let pp = route2[pos - 1];
                        let nn = route2[pos];
                        let insert_cost =
                            dm[pp][v] as i64 + dm[v][nn] as i64 - dm[pp][nn] as i64;
                        let delta = insert_cost - removal_save;
                        if delta >= best_delta {
                            continue;
                        }
                        if insert_feasible(
                            route2,
                            dm,
                            st,
                            rt,
                            dt,
                            &earliest_per[r2],
                            &latest_per[r2],
                            pos,
                            v,
                        ) {
                            best_delta = delta;
                            best_target = Some((r2, pos));
                        }
                    }
                }

                if let Some((r2, pos)) = best_target {
                    routes[r].remove(p);
                    route_demands[r] -= demands[v];
                    routes[r2].insert(pos, v);
                    route_demands[r2] += demands[v];
                    any = true;
                    local = true;
                    // Recompute earliest/latest for the two affected routes.
                    compute_route_times(
                        &routes[r],
                        dm, st, rt, dt,
                        &mut earliest_per[r], &mut latest_per[r],
                    );
                    compute_route_times(
                        &routes[r2],
                        dm, st, rt, dt,
                        &mut earliest_per[r2], &mut latest_per[r2],
                    );
                } else {
                    p += 1;
                }
            }
        }
        // Drop empty routes.
        let mut i = 0;
        while i < routes.len() {
            if routes[i].len() <= 2 {
                routes.remove(i);
                route_demands.remove(i);
                if i < earliest_per.len() {
                    earliest_per.remove(i);
                    latest_per.remove(i);
                }
            } else {
                i += 1;
            }
        }
    }
    any
}

// ── Inter-route swap with O(1) feasibility ──────────────────────────
fn swap_inter(
    routes: &mut Vec<Vec<usize>>,
    route_demands: &mut Vec<i32>,
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
    demands: &[i32],
    cap: i32,
    deadline: Instant,
) -> bool {
    let mut any = false;
    let mut local = true;
    let mut earliest_per: Vec<Vec<i32>> = Vec::new();
    let mut latest_per: Vec<Vec<i32>> = Vec::new();
    while local {
        local = false;
        if Instant::now() >= deadline {
            break;
        }
        earliest_per.clear();
        latest_per.clear();
        for r in 0..routes.len() {
            let mut e = Vec::new();
            let mut l = Vec::new();
            compute_route_times(&routes[r], dm, st, rt, dt, &mut e, &mut l);
            earliest_per.push(e);
            latest_per.push(l);
        }
        for r in 0..routes.len() {
            for r2 in (r + 1)..routes.len() {
                if Instant::now() >= deadline {
                    return any;
                }
                let mut p1 = 1usize;
                while p1 < routes[r].len().saturating_sub(1) {
                    let v = routes[r][p1];
                    let prev_v = routes[r][p1 - 1];
                    let next_v = routes[r][p1 + 1];
                    let mut p2 = 1usize;
                    let mut applied = false;
                    while p2 < routes[r2].len().saturating_sub(1) {
                        let u = routes[r2][p2];
                        if route_demands[r] - demands[v] + demands[u] > cap {
                            p2 += 1;
                            continue;
                        }
                        if route_demands[r2] - demands[u] + demands[v] > cap {
                            p2 += 1;
                            continue;
                        }
                        let prev_u = routes[r2][p2 - 1];
                        let next_u = routes[r2][p2 + 1];
                        let old_d = dm[prev_v][v] as i64
                            + dm[v][next_v] as i64
                            + dm[prev_u][u] as i64
                            + dm[u][next_u] as i64;
                        let new_d = dm[prev_v][u] as i64
                            + dm[u][next_v] as i64
                            + dm[prev_u][v] as i64
                            + dm[v][next_u] as i64;
                        let delta = new_d - old_d;
                        if delta >= 0 {
                            p2 += 1;
                            continue;
                        }
                        if !replace_feasible(
                            &routes[r],
                            dm, st, rt, dt,
                            &earliest_per[r], &latest_per[r],
                            p1, u,
                        ) {
                            p2 += 1;
                            continue;
                        }
                        if !replace_feasible(
                            &routes[r2],
                            dm, st, rt, dt,
                            &earliest_per[r2], &latest_per[r2],
                            p2, v,
                        ) {
                            p2 += 1;
                            continue;
                        }
                        routes[r][p1] = u;
                        routes[r2][p2] = v;
                        route_demands[r] = route_demands[r] - demands[v] + demands[u];
                        route_demands[r2] = route_demands[r2] - demands[u] + demands[v];
                        any = true;
                        local = true;
                        applied = true;
                        // Recompute time bounds for the two routes.
                        compute_route_times(
                            &routes[r],
                            dm, st, rt, dt,
                            &mut earliest_per[r], &mut latest_per[r],
                        );
                        compute_route_times(
                            &routes[r2],
                            dm, st, rt, dt,
                            &mut earliest_per[r2], &mut latest_per[r2],
                        );
                        break;
                    }
                    if applied {
                        continue;
                    }
                    p1 += 1;
                }
            }
        }
    }
    any
}

fn route_feasible(
    route: &[usize],
    dm: &[Vec<i32>],
    st: i32,
    rt: &[i32],
    dt: &[i32],
) -> bool {
    let mut t: i32 = 0;
    let mut prev: usize = route[0];
    for k in 1..route.len() {
        let nd = route[k];
        t = (t + dm[prev][nd]).max(rt[nd]);
        if t > dt[nd] {
            return false;
        }
        t += st;
        prev = nd;
    }
    true
}
// --- END EDITABLE REGION --- //
