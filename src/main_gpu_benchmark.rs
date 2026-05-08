use anyhow::Result;
use clap::{arg, value_parser, Command};
use std::cell::Cell;
use std::time::Instant;
use tig_challenges as challenges;

fn cli() -> Command {
    Command::new("tig_gpu_benchmark")
        .about("Combined generate+solve+evaluate for GPU challenges")
        .arg(
            arg!(<CHALLENGE> "Challenge name (hypergraph, neuralnet_optimizer)")
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
            use cudarc::driver::{CudaContext, Ptx};
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

            let saved: Cell<Option<challenges::$c::Solution>> = Cell::new(None);
            let save_solution = |solution: &challenges::$c::Solution| -> Result<()> {
                saved.set(Some(solution.clone()));
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

            match saved.take() {
                Some(solution) => {
                    match instance.evaluate_solution(
                        &solution,
                        module.clone(),
                        stream.clone(),
                        &prop,
                    ) {
                        Ok(quality) => {
                            let json = serde_json::json!({
                                "score": quality,
                                "feasible": true,
                                "instance": format!("{}/{}", track_id, index),
                                "elapsed": elapsed,
                            });
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
