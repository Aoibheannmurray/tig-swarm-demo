use anyhow::Result;
use clap::{arg, value_parser, Command};
use std::sync::Mutex;
use std::time::Instant;
use tig_challenges as challenges;

fn cli() -> Command {
    Command::new("tig_gpu_benchmark")
        .about("Combined generate+solve+evaluate for GPU challenges")
        .arg(
            arg!(<CHALLENGE> "Challenge name (hypergraph, neuralnet_optimizer, vector_search)")
                .value_parser(value_parser!(String)),
        )
        .arg(
            arg!(<TRACK> "Track specification (key=value format)")
                .value_parser(value_parser!(String)),
        )
        .arg(
            arg!(--seed <SEED> "Random seed string")
                .value_parser(value_parser!(String)),
        )
        .arg(
            arg!(--index <INDEX> "Instance index")
                .value_parser(value_parser!(usize)),
        )
        .arg(
            arg!(--timeout <TIMEOUT> "Solver timeout in seconds")
                .default_value("30")
                .value_parser(value_parser!(u64)),
        )
        .arg(
            arg!(--ptx <PTX> "Path to compiled .ptx file")
                .value_parser(value_parser!(String)),
        )
}

macro_rules! append_viz_data {
    ($json:expr, $challenge_name:expr, $instance:expr, $solution:expr,
     $module:expr, $stream:expr, $prop:expr, $quality:expr) => {
        match $challenge_name {
            #[cfg(feature = "hypergraph")]
            "hypergraph" => {
                let inst: &challenges::hypergraph::Challenge = &$instance;
                let sol: &challenges::hypergraph::Solution = &$solution;
                let num_parts = inst.num_parts as usize;
                let mut partition_sizes = vec![0u32; num_parts];
                for &p in &sol.partition {
                    if (p as usize) < num_parts {
                        partition_sizes[p as usize] += 1;
                    }
                }
                let connectivity_metric = inst.evaluate_connectivity_metric(
                    sol, $module.clone(), $stream.clone(), $prop,
                ).ok();
                let mut hg = serde_json::json!({
                    "num_nodes": inst.num_nodes,
                    "num_parts": inst.num_parts,
                    "max_part_size": inst.max_part_size,
                    "partition_sizes": partition_sizes,
                    "connectivity_metric": connectivity_metric,
                    "baseline_connectivity_metric": inst.greedy_baseline_connectivity_metric,
                });
                // The dashboard panel draws nothing without `galaxy_view`; add
                // it (plus `cuts_between` for the CUTS stat) when we can build
                // the sampled layout from the solved partition.
                if let Some((galaxy, cuts)) =
                    build_hypergraph_viz(inst, sol, $stream.clone())
                {
                    hg["galaxy_view"] = galaxy;
                    hg["cuts_between"] = cuts;
                }
                $json["hypergraph_data"] = hg;
            }
            #[cfg(feature = "neuralnet_optimizer")]
            "neuralnet_optimizer" => {
                let inst: &challenges::neuralnet_optimizer::Challenge = &$instance;
                let sol: &challenges::neuralnet_optimizer::Solution = &$solution;
                let total_params: usize = sol.weights.iter()
                    .map(|layer| layer.iter().map(|row| row.len()).sum::<usize>())
                    .sum::<usize>()
                    + sol.biases.iter().map(|b| b.len()).sum::<usize>();

                // Compute noise floor from dataset (no model needed)
                let noise_floor: Option<f64> = (|| -> Option<f64> {
                    let y_h = $stream.memcpy_dtov(&inst.dataset.test_targets_noisy()).ok()?;
                    let f_h = $stream.memcpy_dtov(&inst.dataset.test_targets_true_f()).ok()?;
                    let _ = $stream.synchronize();
                    let sum_sq: f32 = y_h.iter().zip(f_h.iter())
                        .map(|(y, f)| (*y - *f).powi(2))
                        .sum();
                    Some((4.0f64 / inst.dataset.test_size as f64) * sum_sq as f64)
                })();

                // Derive model test loss from quality and noise floor
                let model_loss: Option<f64> = noise_floor.map(|nf| {
                    let quality_ratio = $quality as f64 / 1_000_000.0;
                    nf * (1.0 - quality_ratio)
                });

                $json["neuralnet_data"] = serde_json::json!({
                    "epochs_used": sol.epochs_used,
                    "max_epochs": inst.max_epochs,
                    "num_hidden_layers": inst.num_hidden_layers,
                    "total_params": total_params,
                    "noise_floor": noise_floor,
                    "model_loss": model_loss,
                });
            }
            #[cfg(feature = "vector_search")]
            "vector_search" => {
                let inst: &challenges::vector_search::Challenge = &$instance;
                let sol: &challenges::vector_search::Solution = &$solution;
                let avg_distance = inst.evaluate_average_distance(
                    sol, $module.clone(), $stream.clone(), $prop,
                ).ok();
                $json["vector_search_data"] = serde_json::json!({
                    "num_queries": inst.num_queries,
                    "vector_dims": inst.vector_dims,
                    "database_size": inst.database_size,
                    "avg_distance": avg_distance,
                });
            }
            _ => {}
        }
    };
}

/// Build the hypergraph dashboard's `galaxy_view` payload (and the
/// `cuts_between` partition matrix that drives the CUTS stat) from a solved
/// instance.
///
/// The dashboard renders nothing unless it receives a `galaxy_view`: a sampled
/// spatial layout of up to 8 partition clusters (the palette has 8 colours),
/// ~2k nodes scattered around their cluster centroid, and a sample of cut
/// hyperedges drawn as ribbons between them. The full graph (up to 200k nodes)
/// is far too large to ship or draw, so we down-sample deterministically.
///
/// Pure host-side: we copy the hyperedge CSR arrays back from the device once
/// (this runs a single time per instance, after solving) and lay everything
/// out on the CPU — no new kernels.
#[cfg(feature = "hypergraph")]
fn build_hypergraph_viz(
    inst: &challenges::hypergraph::Challenge,
    sol: &challenges::hypergraph::Solution,
    stream: std::sync::Arc<cudarc::driver::CudaStream>,
) -> Option<(serde_json::Value, serde_json::Value)> {
    use std::f64::consts::PI;

    // Round coordinates to 1 decimal: sub-pixel precision is invisible at the
    // panel's scale, and trimming the digits keeps the published payload small
    // (a prior proxy/body-size limit silently dropped oversized solution_data).
    let r1 = |v: f64| (v * 10.0).round() / 10.0;

    const MAX_SHOWN_PARTS: usize = 8; // matches the dashboard PALETTE length
    const MAX_NODES: usize = 2000;
    const MAX_CUT_EDGES: usize = 300;
    const MAX_EDGE_MEMBERS: usize = 8;
    const WIDTH: f64 = 600.0;
    const HEIGHT: f64 = 410.0;

    let num_nodes = inst.num_nodes as usize;
    let num_parts = inst.num_parts as usize;
    let partition = &sol.partition;
    if num_nodes == 0 || num_parts == 0 || partition.len() != num_nodes {
        return None;
    }

    // Partition sizes, then pick the largest few to show (palette-capped).
    let mut sizes = vec![0u32; num_parts];
    for &p in partition {
        if (p as usize) < num_parts {
            sizes[p as usize] += 1;
        }
    }
    let mut order: Vec<usize> = (0..num_parts).filter(|&p| sizes[p] > 0).collect();
    order.sort_by(|&a, &b| sizes[b].cmp(&sizes[a]).then(a.cmp(&b)));
    let shown: Vec<usize> = order.into_iter().take(MAX_SHOWN_PARTS).collect();
    if shown.is_empty() {
        return None;
    }
    let k = shown.len();

    let mut part_to_slot = vec![-1i32; num_parts];
    for (slot, &p) in shown.iter().enumerate() {
        part_to_slot[p] = slot as i32;
    }
    let shown_partitions: Vec<u32> = shown.iter().map(|&p| p as u32).collect();
    let shown_sizes: Vec<u32> = shown.iter().map(|&p| sizes[p]).collect();
    let over_cap: Vec<bool> = shown_sizes.iter().map(|&sz| sz > inst.max_part_size).collect();

    // Cluster centroids on a ring; one cluster sits dead-centre.
    let (cx0, cy0) = (WIDTH / 2.0, HEIGHT / 2.0);
    let ring_r = WIDTH.min(HEIGHT) * 0.33;
    let cluster_r = if k <= 1 {
        WIDTH.min(HEIGHT) * 0.30
    } else {
        (ring_r * (PI / k as f64).sin() * 0.9).min(HEIGHT * 0.22)
    };
    let centroids: Vec<[f64; 2]> = (0..k)
        .map(|s| {
            if k == 1 {
                [cx0, cy0]
            } else {
                let ang = 2.0 * PI * (s as f64) / (k as f64) - PI / 2.0;
                [r1(cx0 + ring_r * ang.cos()), r1(cy0 + ring_r * ang.sin())]
            }
        })
        .collect();

    // Down-sample nodes that belong to a shown partition, then place each one
    // at a deterministic point inside its cluster disk (hash-jittered so the
    // layout is stable across re-renders but not a rigid grid).
    let candidates: Vec<usize> = (0..num_nodes)
        .filter(|&n| part_to_slot[partition[n] as usize] >= 0)
        .collect();
    if candidates.is_empty() {
        return None;
    }
    let stride = (candidates.len() + MAX_NODES - 1) / MAX_NODES;
    let sampled: Vec<usize> = candidates.iter().step_by(stride).copied().collect();
    let mut id_to_idx = vec![-1i32; num_nodes];
    for (i, &nid) in sampled.iter().enumerate() {
        id_to_idx[nid] = i as i32;
    }

    let mut nodes_json: Vec<[f64; 3]> = Vec::with_capacity(sampled.len());
    for &nid in &sampled {
        let slot = part_to_slot[partition[nid] as usize];
        let [cx, cy] = centroids[slot as usize];
        // splitmix64-style hash → two uniforms → uniform point in a disk.
        let mut h = (nid as u64)
            .wrapping_mul(0x9E37_79B9_7F4A_7C15)
            .wrapping_add(0xD1B5_4A32_D192_ED03);
        h = (h ^ (h >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        h = (h ^ (h >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        h ^= h >> 31;
        let u1 = ((h & 0xFF_FFFF) as f64) / (0x100_0000 as f64);
        let u2 = (((h >> 24) & 0xFF_FFFF) as f64) / (0x100_0000 as f64);
        let r = cluster_r * u1.sqrt();
        let theta = 2.0 * PI * u2;
        nodes_json.push([slot as f64, r1(cx + r * theta.cos()), r1(cy + r * theta.sin())]);
    }

    // Copy the hyperedge CSR arrays back from the device for cut analysis.
    let offsets: Vec<i32> = stream.memcpy_dtov(&inst.d_hyperedge_offsets).ok()?;
    let hnodes: Vec<i32> = stream.memcpy_dtov(&inst.d_hyperedge_nodes).ok()?;
    let _ = stream.synchronize();
    let num_he = inst.num_hyperedges as usize;
    if offsets.len() < num_he + 1 {
        return None;
    }

    // cuts_between[i][j]: number of hyperedges that touch both partition i and
    // partition j (full graph, all partitions). The dashboard sums the upper
    // triangle for the CUTS stat. cut_edges: a sample of cut hyperedges among
    // the *shown* partitions, as indices into nodes_json, for the ribbons.
    let mut cuts_between = vec![vec![0i64; num_parts]; num_parts];
    let mut cut_edges: Vec<Vec<i32>> = Vec::new();
    let mut parts_buf: Vec<usize> = Vec::with_capacity(MAX_EDGE_MEMBERS);
    for e in 0..num_he {
        let start = offsets[e] as usize;
        let end = offsets[e + 1] as usize;
        if end <= start || end > hnodes.len() {
            continue;
        }

        // Distinct partitions this hyperedge spans → pairwise cut counts.
        parts_buf.clear();
        for &raw in &hnodes[start..end] {
            let nid = raw as usize;
            if nid >= num_nodes {
                continue;
            }
            let p = partition[nid] as usize;
            if p < num_parts && !parts_buf.contains(&p) {
                parts_buf.push(p);
            }
        }
        for i in 0..parts_buf.len() {
            for j in (i + 1)..parts_buf.len() {
                let (a, b) = (parts_buf[i], parts_buf[j]);
                cuts_between[a][b] += 1;
                cuts_between[b][a] += 1;
            }
        }

        // Ribbon sample: only sampled members, only if the edge spans ≥2 shown
        // partitions (so the ribbon visibly crosses clusters).
        if cut_edges.len() < MAX_CUT_EDGES {
            let mut members: Vec<i32> = Vec::new();
            let mut slot_mask = 0u16;
            for &raw in &hnodes[start..end] {
                let nid = raw as usize;
                if nid >= num_nodes {
                    continue;
                }
                let idx = id_to_idx[nid];
                if idx < 0 {
                    continue;
                }
                slot_mask |= 1u16 << part_to_slot[partition[nid] as usize] as u16;
                if members.len() < MAX_EDGE_MEMBERS {
                    members.push(idx);
                }
            }
            if members.len() >= 2 && slot_mask.count_ones() >= 2 {
                cut_edges.push(members);
            }
        }
    }

    let galaxy = serde_json::json!({
        "shown_partitions": shown_partitions,
        "shown_sizes": shown_sizes,
        "over_cap": over_cap,
        "centroids": centroids,
        "cluster_r": r1(cluster_r),
        "nodes": nodes_json,
        "cut_edges": cut_edges,
        "width": WIDTH,
        "height": HEIGHT,
    });
    Some((galaxy, serde_json::json!(cuts_between)))
}

fn run_instance(
    challenge_name: &str,
    track_id: &str,
    seed: &str,
    index: usize,
    timeout_secs: u64,
    ptx_path: &str,
) -> Result<()> {
    let instance_seed = blake3::hash(
        format!("{}-{}-{}-{}", challenge_name, track_id, seed, index).as_bytes(),
    );

    let ptx_src = std::fs::read_to_string(ptx_path)?;

    macro_rules! dispatch_gpu {
        ($c:ident) => {{
            use cudarc::driver::CudaContext;
            use cudarc::nvrtc::Ptx;
            use cudarc::runtime::result::device::get_device_prop;

            let ctx = CudaContext::new(0)?;
            ctx.set_blocking_synchronize()?;
            let module = ctx.load_module(Ptx::from_src(ptx_src))?;
            let stream = ctx.default_stream();
            let prop = get_device_prop(0)?;

            let track_str = if track_id.starts_with('"') && track_id.ends_with('"') {
                track_id.to_string()
            } else {
                format!(r#""{}""#, track_id)
            };
            let track = serde_json::from_str::<challenges::$c::Track>(&track_str)?;

            let instance = challenges::$c::Challenge::generate_instance(
                instance_seed.as_bytes(),
                &track,
                module.clone(),
                stream.clone(),
                &prop,
            )?;

            let saved: Mutex<Option<challenges::$c::Solution>> = Mutex::new(None);
            let save_solution = |solution: &challenges::$c::Solution| -> Result<()> {
                *saved.lock().unwrap() = Some(solution.clone());
                Ok(())
            };

            let start = Instant::now();
            let solver_result = std::thread::scope(|s| {
                let handle = s.spawn(|| {
                    challenges::$c::algorithm::solve_challenge(
                        &instance,
                        &save_solution,
                        &None,
                        module.clone(),
                        stream.clone(),
                        &prop,
                    )
                });
                loop {
                    if handle.is_finished() {
                        return handle.join().unwrap();
                    }
                    if start.elapsed().as_secs() >= timeout_secs {
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                Err(anyhow::anyhow!("Solver timeout"))
            });

            let elapsed = start.elapsed().as_secs_f64();

            let saved_solution = saved.lock().unwrap().take();
            match saved_solution {
                Some(solution) => {
                    match instance.evaluate_solution(
                        &solution,
                        module.clone(),
                        stream.clone(),
                        &prop,
                    ) {
                        Ok(quality) => {
                            let mut json = serde_json::json!({
                                "score": quality,
                                "feasible": true,
                                "instance": format!("{}/{}", track_id, index),
                                "elapsed": elapsed,
                            });
                            append_viz_data!(json, challenge_name, instance, solution,
                                            module, stream, &prop, quality);
                            println!("{}", json);
                        }
                        Err(e) => {
                            let json = serde_json::json!({
                                "score": 0,
                                "feasible": false,
                                "instance": format!("{}/{}", track_id, index),
                                "elapsed": elapsed,
                                "error": format!("Evaluation failed: {}", e),
                            });
                            println!("{}", json);
                        }
                    }
                }
                None => {
                    let json = serde_json::json!({
                        "score": 0,
                        "feasible": false,
                        "instance": format!("{}/{}", track_id, index),
                        "elapsed": elapsed,
                        "error": if solver_result.is_err() {
                            format!("{}", solver_result.unwrap_err())
                        } else {
                            "No solution saved".to_string()
                        },
                    });
                    println!("{}", json);
                }
            }
        }};
    }

    challenges::enabled_gpu_challenge_arms!(challenge_name, dispatch_gpu);
    Ok(())
}

fn main() -> Result<()> {
    let matches = cli().get_matches();
    let challenge = matches.get_one::<String>("CHALLENGE").unwrap();
    let track = matches.get_one::<String>("TRACK").unwrap();
    let seed = matches.get_one::<String>("seed").unwrap();
    let index = *matches.get_one::<usize>("index").unwrap();
    let timeout = *matches.get_one::<u64>("timeout").unwrap();
    let ptx = matches.get_one::<String>("ptx").unwrap();
    run_instance(challenge, track, seed, index, timeout, ptx)
}
