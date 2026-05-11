/// Time step duration in hours (15 minutes)
pub const DELTA_T: f64 = 0.25;

/// Slack bus index (1-indexed in spec, 0-indexed in code)
pub const SLACK_BUS: usize = 0;

/// Action quantization step (MW)
pub const Q_U: f64 = 0.01;

/// SOC quantization step (MWh)
pub const Q_E: f64 = 0.01;

/// Fractional SOC lower bound
pub const E_MIN_FRAC: f64 = 0.10;

/// Fractional SOC upper bound
pub const E_MAX_FRAC: f64 = 0.90;

/// Initial SOC fraction
pub const E_INIT_FRAC: f64 = 0.50;

/// Default charge efficiency
pub const ETA_CHARGE: f64 = 0.95;

/// Default discharge efficiency
pub const ETA_DISCHARGE: f64 = 0.95;

/// Transaction cost ($/MWh)
pub const KAPPA_TX: f64 = 0.25;

/// Degradation scale ($)
pub const KAPPA_DEG: f64 = 1.00;

/// Degradation exponent
pub const BETA_DEG: f64 = 2.0;

/// RT bias term
pub const MU_BIAS: f64 = 0.0;

/// Spatial correlation parameter
pub const RHO_SPATIAL: f64 = 0.70;

/// Congestion premium scale ($/MWh)
pub const GAMMA_PRICE: f64 = 20.0;

/// Congestion proximity threshold
pub const TAU_CONG: f64 = 0.90;

/// Jump probability
pub const RHO_JUMP: f64 = 0.02;

/// Pareto tail index
pub const ALPHA_TAIL: f64 = 3.5;

/// RT price floor ($/MWh)
pub const LAMBDA_MIN: f64 = -200.0;

/// RT price cap ($/MWh)
pub const LAMBDA_MAX: f64 = 5000.0;

/// DA price floor ($/MWh)
pub const LAMBDA_DA_MIN: f64 = 0.0;

/// Flow feasibility tolerance (per-unit)
pub const EPS_FLOW: f64 = 1e-6;

/// SOC feasibility tolerance (MWh)
pub const EPS_SOC: f64 = 1e-9;

// ── Baseline-shared tunables ──
// Used by every baseline solver under `baselines/`. Centralised here so a
// numerical retune doesn't have to track down N copies; agent-side
// `algorithm/mod.rs` files (gitignored, per-agent) may re-declare their
// own values if they're tuning more aggressively.

/// Generic flow-comparison tolerance used inside baseline iteration loops.
pub const EPS_BASELINE: f64 = 1e-12;

/// Max iterations of `enforce_flow_feasibility` in baseline policies.
pub const MAX_FLOW_ADJUST_ITERS: usize = 64;

/// Bisection iterations for the global-action-scale fallback when
/// per-line softening can't find a feasible point.
pub const GLOBAL_SCALE_BSEARCH_ITERS: usize = 32;

/// Nominal battery capacity (MWh)
pub const NOMINAL_CAPACITY: f64 = 100.0;

/// Nominal battery power (MW)
pub const NOMINAL_POWER: f64 = 25.0;

/// Nominal line flow limit (MW)
pub const NOMINAL_FLOW_LIMIT: f64 = 50.0;

/// Base susceptance for network generation
pub const BASE_SUSCEPTANCE: f64 = 10.0;

/// Mean DA price ($/MWh)
pub const MEAN_DA_PRICE: f64 = 50.0;

/// DA price amplitude ($/MWh)
pub const DA_AMPLITUDE: f64 = 20.0;
