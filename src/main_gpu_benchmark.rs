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
                $json["hypergraph_data"] = serde_json::json!({
                    "num_nodes": inst.num_nodes,
                    "num_parts": inst.num_parts,
                    "max_part_size": inst.max_part_size,
                    "partition_sizes": partition_sizes,
                    "connectivity_metric": connectivity_metric,
                    "baseline_connectivity_metric": inst.greedy_baseline_connectivity_metric,
                });
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
