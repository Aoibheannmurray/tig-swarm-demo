use anyhow::Result;
use clap::{arg, value_parser, Command};
use serde_json::{Map, Value};
use std::fs;
use std::path::{Path, PathBuf};
use tig_challenges as challenges;

fn cli() -> Command {
    Command::new("tig-challenges-solver")
        .about("TIG challenge solver")
        .arg(
            arg!(<CHALLENGE> "Challenge name (satisfiability, vehicle_routing, knapsack, job_scheduling, energy_arbitrage)")
                .value_parser(value_parser!(String)),
        )
        .arg(arg!(<INSTANCE_FILE> "Path to the instance file").value_parser(value_parser!(PathBuf)))
        .arg(
            arg!(<SOLUTION_FILE> "Path to write the solution file")
                .value_parser(value_parser!(PathBuf)),
        )
        .arg(
            arg!(--hyperparameters [HYPERPARAMETERS] "JSON string for solver hyperparameters")
                .value_parser(value_parser!(String)),
        )
}

fn run_solve(
    challenge: &str,
    instance_file: &Path,
    solution_file: &Path,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()> {
    anyhow::ensure!(
        instance_file.exists(),
        "Instance file does not exist: {}",
        instance_file.display()
    );
    let content = fs::read_to_string(instance_file)?;

    macro_rules! dispatch_solve {
        ($c:ident) => {{
            let instance = challenges::$c::Challenge::from_txt(&content)?;
            let save_solution_fn = |solution: &challenges::$c::Solution| -> Result<()> {
                fs::write(&solution_file, solution.to_txt())?;
                Ok(())
            };
            challenges::$c::algorithm::solve_challenge(
                &instance,
                &save_solution_fn,
                hyperparameters,
            )?;
        }};
    }

    challenges::enabled_challenge_arms!(challenge, dispatch_solve);
    Ok(())
}

fn main() -> Result<()> {
    let matches = cli().get_matches();
    let challenge = matches.get_one::<String>("CHALLENGE").unwrap();
    let instance_file = matches.get_one::<PathBuf>("INSTANCE_FILE").unwrap();
    let solution_file = matches.get_one::<PathBuf>("SOLUTION_FILE").unwrap();
    let hyperparameters = matches
        .get_one::<String>("hyperparameters")
        .map(|s| serde_json::from_str(s))
        .transpose()
        .map_err(|e| anyhow::anyhow!("Invalid --hyperparameters JSON: {}", e))?;
    run_solve(challenge, instance_file, solution_file, &hyperparameters)
}
