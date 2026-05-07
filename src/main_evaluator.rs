use anyhow::Result;
use clap::{arg, value_parser, Command};
use serde_json::json;
use std::fs;
use std::path::{Path, PathBuf};
use tig_challenges as challenges;

fn cli() -> Command {
    Command::new("tig-challenges-evaluator")
        .about("TIG challenge evaluation")
        .arg(
            arg!(<CHALLENGE> "Challenge name (satisfiability, vehicle_routing, knapsack, job_scheduling, energy_arbitrage)")
                .value_parser(value_parser!(String)),
        )
        .arg(arg!(<INSTANCE_FILE> "Path to the instance file").value_parser(value_parser!(PathBuf)))
        .arg(arg!(<SOLUTION_FILE> "Path to the solution file").value_parser(value_parser!(PathBuf)))
}

fn run_evaluate(challenge: &str, instance_file: &Path, solution_file: &Path) -> Result<()> {
    anyhow::ensure!(
        instance_file.exists(),
        "Instance file does not exist: {}",
        instance_file.display()
    );
    anyhow::ensure!(
        solution_file.exists(),
        "Solution file does not exist: {}",
        solution_file.display()
    );
    let instance_content = fs::read_to_string(instance_file)?;
    let solution_content = fs::read_to_string(solution_file)?;

    // All challenges return Result<i32> (baseline-relative quality).
    // Cast to f64 for uniform JSON output.
    macro_rules! dispatch_evaluate {
        ($c:ident) => {{
            let instance = challenges::$c::Challenge::from_txt(&instance_content)?;
            let solution = challenges::$c::Solution::from_txt(&solution_content)?;
            instance.evaluate_solution(&solution)? as f64
        }};
    }

    let out: f64 = challenges::enabled_challenge_arms!(challenge, dispatch_evaluate);
    // The dispatch returns each challenge's native score type; print it
    // as a generic `score` field so downstream tooling can stay challenge-
    // agnostic. (We keep the legacy "distance" key as a fallback for
    // existing benchmark.py parsing of vehicle_routing output.)
    let score_value = serde_json::to_value(&out).unwrap_or(serde_json::Value::Null);
    println!(
        "{}",
        json!({
            "score": score_value.clone(),
            "distance": score_value,
        })
    );
    Ok(())
}

fn main() -> Result<()> {
    let matches = cli().get_matches();
    let challenge = matches.get_one::<String>("CHALLENGE").unwrap();
    let instance_file = matches.get_one::<PathBuf>("INSTANCE_FILE").unwrap();
    let solution_file = matches.get_one::<PathBuf>("SOLUTION_FILE").unwrap();
    run_evaluate(challenge, instance_file, solution_file)
}
