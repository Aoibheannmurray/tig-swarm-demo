// initial_algorithms/vehicle_routing/seeds/construction.rs
//
// Simple SEED algorithm for the vehicle_routing (VRP with time windows)
// challenge: a self-contained Solomon-style sequential insertion (I1) heuristic.
//
// This is deliberately written out in full (rather than calling the built-in
// baseline) so the whole algorithm is visible to a model that picks the seed up
// to improve. It is feasible by construction: routes are grown from the depot,
// candidates are filtered by capacity, and every insertion is time-window
// checked (`is_feasible`) before being accepted, so capacity, due times, and the
// depot return-by time all hold. It also keeps the route count low enough to fit
// the fleet — a naive nearest-neighbour build does not.
//
// Natural refinements for a weaker model: intra-route 2-opt / or-opt, smarter
// seed-customer choice, the c1/c2 insertion weights (alpha1/alpha2/lambda),
// or route merging to shave distance.

use super::*;
use anyhow::Result;
use serde_json::{Map, Value};

pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    _hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    let mut routes: Vec<Vec<usize>> = Vec::new();

    // Customers ordered by distance from the depot; we seed each new route with
    // the farthest remaining customer, then fill it via cheapest feasible insert.
    let mut nodes: Vec<usize> = (1..challenge.num_nodes).collect();
    nodes.sort_by(|&a, &b| challenge.distance_matrix[0][a].cmp(&challenge.distance_matrix[0][b]));

    let mut remaining: Vec<bool> = vec![true; challenge.num_nodes];
    remaining[0] = false;

    while let Some(node) = nodes.pop() {
        if !remaining[node] {
            continue;
        }
        remaining[node] = false;
        let mut route = vec![0, node, 0];
        let mut route_demand = challenge.demands[node];

        while let Some((best_node, best_pos)) = find_best_insertion(
            &route,
            remaining
                .iter()
                .enumerate()
                .filter(|(n, &flag)| {
                    flag && route_demand + challenge.demands[*n] <= challenge.max_capacity
                })
                .map(|(n, _)| n)
                .collect(),
            &challenge.distance_matrix,
            challenge.service_time,
            &challenge.ready_times,
            &challenge.due_times,
        ) {
            remaining[best_node] = false;
            route_demand += challenge.demands[best_node];
            route.insert(best_pos, best_node);
        }

        routes.push(route);
    }

    save_solution(&Solution { routes })?;
    Ok(())
}

/// Can the (partial) `route` be served within all time windows, starting the
/// check at `start_pos` from `curr_node` / `curr_time`?
fn is_feasible(
    route: &Vec<usize>,
    distance_matrix: &Vec<Vec<i32>>,
    service_time: i32,
    ready_times: &Vec<i32>,
    due_times: &Vec<i32>,
    mut curr_node: usize,
    mut curr_time: i32,
    start_pos: usize,
) -> bool {
    for pos in start_pos..route.len() {
        let next_node = route[pos];
        curr_time += distance_matrix[curr_node][next_node];
        if curr_time > due_times[route[pos]] {
            return false;
        }
        curr_time = curr_time.max(ready_times[next_node]) + service_time;
        curr_node = next_node;
    }
    true
}

/// Solomon I1 cheapest-insertion: pick the (customer, position) maximising the
/// c1/c2 savings criteria among all time-window-feasible insertions.
fn find_best_insertion(
    route: &Vec<usize>,
    remaining_nodes: Vec<usize>,
    distance_matrix: &Vec<Vec<i32>>,
    service_time: i32,
    ready_times: &Vec<i32>,
    due_times: &Vec<i32>,
) -> Option<(usize, usize)> {
    let alpha1 = 1;
    let alpha2 = 0;
    let lambda = 1;

    let mut best_c2 = None;
    let mut best = None;
    for insert_node in remaining_nodes {
        let mut best_c1 = None;

        let mut curr_time = 0;
        let mut curr_node = 0;
        for pos in 1..route.len() {
            let next_node = route[pos];
            let new_arrival_time =
                ready_times[insert_node].max(curr_time + distance_matrix[curr_node][insert_node]);
            if new_arrival_time > due_times[insert_node] {
                continue;
            }
            let old_arrival_time =
                ready_times[next_node].max(curr_time + distance_matrix[curr_node][next_node]);

            // Distance criterion: c11 = d(i,u) + d(u,j) - mu * d(i,j)
            let c11 = distance_matrix[curr_node][insert_node]
                + distance_matrix[insert_node][next_node]
                - distance_matrix[curr_node][next_node];

            // Time criterion: c12 = b_ju - b_j (shift in arrival time at `pos`).
            let c12 = new_arrival_time - old_arrival_time;

            let c1 = -(alpha1 * c11 + alpha2 * c12);
            let c2 = lambda * distance_matrix[0][insert_node] + c1;

            if best_c1.is_none_or(|x| c1 > x)
                && best_c2.is_none_or(|x| c2 > x)
                && is_feasible(
                    route,
                    distance_matrix,
                    service_time,
                    ready_times,
                    due_times,
                    insert_node,
                    new_arrival_time + service_time,
                    pos,
                )
            {
                best_c1 = Some(c1);
                best_c2 = Some(c2);
                best = Some((insert_node, pos));
            }

            curr_time = ready_times[next_node]
                .max(curr_time + distance_matrix[curr_node][next_node])
                + service_time;
            curr_node = next_node;
        }
    }
    best
}
