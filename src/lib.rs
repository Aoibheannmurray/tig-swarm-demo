pub const BUILD_TIME_PATH: &str = env!("CARGO_MANIFEST_DIR");

// Per-challenge quality scaling factor used by the upstream evaluators.
// Kept here so vendored challenge modules compile unchanged.
#[allow(dead_code)]
pub(crate) const QUALITY_PRECISION: i32 = 1_000_000;

// Deterministic hasher + type aliases used by algorithm templates.
pub fn seeded_hasher(seed: &[u8; 32]) -> ahash::RandomState {
    let seed1 = u64::from_be_bytes(seed[0..8].try_into().unwrap());
    let seed2 = u64::from_be_bytes(seed[8..16].try_into().unwrap());
    let seed3 = u64::from_be_bytes(seed[16..24].try_into().unwrap());
    let seed4 = u64::from_be_bytes(seed[24..32].try_into().unwrap());
    ahash::RandomState::with_seeds(seed1, seed2, seed3, seed4)
}
pub type HashMap<K, V> = std::collections::HashMap<K, V, ahash::RandomState>;
pub type HashSet<T> = std::collections::HashSet<T, ahash::RandomState>;

// In the upstream tig-challenges crate, conditional_pub! gates verification
// fns on the `hide_verification` feature so contest binaries can hide them.
// The swarm demo always needs the verification path (agents evaluate
// locally), so this is unconditionally `pub`.
macro_rules! conditional_pub {
    (fn $name:ident $($rest:tt)*) => {
        pub fn $name $($rest)*
    };
}

macro_rules! impl_kv_string_serde {
    ($name:ident { $( $field:ident : $ty:ty ),* $(,)? }) => {
        paste::paste! {
            #[derive(Debug, Clone, PartialEq)]
            pub struct $name {
                $( pub $field : $ty ),*
            }

            impl serde::Serialize for $name {
                fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
                where
                    S: serde::Serializer,
                {
                    let mut parts = Vec::new();
                    $(
                        parts.push(format!("{}={}", stringify!($field), self.$field));
                    )*
                    // optional: sort keys for deterministic output
                    parts.sort();
                    let s = parts.join(",");
                    serializer.serialize_str(&s)
                }
            }

            impl<'de> serde::Deserialize<'de> for $name {
                fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
                where
                    D: serde::Deserializer<'de>
                {
                    use serde::de::{Visitor, Error};
                    use std::fmt;

                    struct VisitorImpl;

                    impl<'de> Visitor<'de> for VisitorImpl {
                        type Value = $name;

                        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                            write!(f, "a string of the form 'key=value,key=value'")
                        }

                        fn visit_str<E>(self, v: &str) -> Result<Self::Value, E>
                        where
                            E: Error,
                        {
                            let mut map = std::collections::HashMap::new();

                            if !v.is_empty() {
                                for part in v.split(',') {
                                    let mut kv = part.splitn(2, '=');
                                    let key = kv.next().ok_or_else(|| E::custom(format!("Missing key in '{}'", part)))?;
                                    let val = kv.next().ok_or_else(|| E::custom(format!("Missing value in '{}'", part)))?;
                                    map.insert(key, val);
                                }
                            }

                            Ok($name {
                                $(
                                    $field: map.get(stringify!($field))
                                        .ok_or_else(|| E::custom(format!("Missing field '{}'", stringify!($field))))?
                                        .parse::<$ty>()
                                        .map_err(E::custom)?,
                                )*
                            })
                        }
                    }

                    deserializer.deserialize_str(VisitorImpl)
                }
            }
        }
    };
}

// Compressed-binary serde used by upstream challenges that ship large
// instance data (SAT clause arrays, energy market histories, etc.).
// Bincode + gzip + base64; identical behavior to upstream so vendored
// challenge code compiles unchanged.
#[allow(unused_macros)]
macro_rules! impl_base64_serde {
    ($name:ident { $( $field:ident : $ty:ty ),* $(,)? }) => {
        paste::paste! {
            #[derive(Debug, Clone)]
            pub struct $name {
                $( pub $field : $ty ),*
            }

            #[derive(serde::Serialize, serde::Deserialize)]
            struct [<$name Data>] {
                $( $field : $ty ),*
            }

            impl serde::Serialize for $name {
                fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
                where
                    S: serde::Serializer,
                {
                    use flate2::{write::GzEncoder, Compression};
                    use base64::engine::general_purpose::STANDARD as BASE64;
                    use base64::Engine;
                    use std::io::Write;

                    let helper = [<$name Data>] {
                        $( $field: self.$field.clone() ),*
                    };

                    let bincode_data = bincode::serialize(&helper)
                        .map_err(|e| serde::ser::Error::custom(format!("Bincode serialization failed: {}", e)))?;

                    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
                    encoder
                        .write_all(&bincode_data)
                        .map_err(|e| serde::ser::Error::custom(format!("Compression failed: {}", e)))?;
                    let compressed_data = encoder
                        .finish()
                        .map_err(|e| serde::ser::Error::custom(format!("Compression finish failed: {}", e)))?;

                    let encoded = BASE64.encode(&compressed_data);
                    serializer.serialize_str(&encoded)
                }
            }

            impl<'de> serde::Deserialize<'de> for $name {
                fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
                where
                    D: serde::Deserializer<'de>,
                {
                    use flate2::read::GzDecoder;
                    use base64::engine::general_purpose::STANDARD as BASE64;
                    use base64::Engine;
                    use std::io::Read;
                    use std::fmt;

                    struct VisitorImpl;

                    impl<'de> serde::de::Visitor<'de> for VisitorImpl {
                        type Value = $name;

                        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                            write!(f, "a base64 encoded, compressed, bincode serialized {}", stringify!($name))
                        }

                        fn visit_str<E>(self, v: &str) -> Result<Self::Value, E>
                        where
                            E: serde::de::Error,
                        {
                            let compressed = BASE64.decode(v)
                                .map_err(|e| E::custom(format!("Base64 decode failed: {}", e)))?;

                            let mut decoder = GzDecoder::new(&compressed[..]);
                            let mut decompressed = Vec::new();
                            decoder
                                .read_to_end(&mut decompressed)
                                .map_err(|e| E::custom(format!("Decompression failed: {}", e)))?;

                            let data: [<$name Data>] = bincode::deserialize(&decompressed)
                                .map_err(|e| E::custom(format!("Bincode deserialization failed: {}", e)))?;

                            Ok($name {
                                $( $field: data.$field ),*
                            })
                        }
                    }

                    deserializer.deserialize_str(VisitorImpl)
                }
            }
        }
    };
}

// ── Per-challenge module declarations ──
//
// Cargo's `[features]` block must list each challenge by name (Cargo
// doesn't expand macros at manifest time), so the `pub mod` block here
// is also per-challenge — but it's the ONLY place a challenge name
// appears as a Rust identifier. Adding a 6th challenge: add a
// `[features]` line to Cargo.toml, add a `pub mod x;` line below, and
// add `x` to the `enabled_challenge_arms!` invocation. Three lines.
#[cfg(feature = "satisfiability")]
pub mod satisfiability;
#[cfg(feature = "vehicle_routing")]
pub mod vehicle_routing;
#[cfg(feature = "knapsack")]
pub mod knapsack;
#[cfg(feature = "job_scheduling")]
pub mod job_scheduling;
#[cfg(feature = "energy_arbitrage")]
pub mod energy_arbitrage;
#[cfg(feature = "hypergraph")]
pub mod hypergraph;
#[cfg(feature = "neuralnet_optimizer")]
pub mod neuralnet_optimizer;

// ── Per-challenge dispatch macro ──
//
// Each binary (`main_solver.rs`, `main_generator.rs`, `main_evaluator.rs`)
// previously hand-wrote a `match challenge { ... }` with 5 cfg-gated
// arms — 15 lines of boilerplate that drifted whenever a challenge was
// added. This macro emits those arms once.
//
// Caller defines a local `macro_rules!` (e.g. `dispatch_solve!`) that
// takes a single `$c:ident` and produces the per-challenge expression
// against `challenges::$c::Challenge`. Then:
//
//   enabled_challenge_arms!(challenge_name_string, dispatch_solve);
//
// emits the 5 cfg-gated arms plus a fall-through that bails with a
// "challenge unknown or disabled" error.
//
// The match expression evaluates to whatever the dispatch macro
// returns — `()` for the solver/generator, `f64` for the evaluator.
#[macro_export]
macro_rules! enabled_challenge_arms {
    ($challenge:expr, $dispatch:ident) => {
        match $challenge {
            #[cfg(feature = "satisfiability")]
            "satisfiability" => $dispatch!(satisfiability),
            #[cfg(feature = "vehicle_routing")]
            "vehicle_routing" => $dispatch!(vehicle_routing),
            #[cfg(feature = "knapsack")]
            "knapsack" => $dispatch!(knapsack),
            #[cfg(feature = "job_scheduling")]
            "job_scheduling" => $dispatch!(job_scheduling),
            #[cfg(feature = "energy_arbitrage")]
            "energy_arbitrage" => $dispatch!(energy_arbitrage),
            _ => anyhow::bail!(
                "Unknown or disabled challenge: {}. Enable its crate feature when building.",
                $challenge
            ),
        }
    };
}

#[macro_export]
macro_rules! enabled_gpu_challenge_arms {
    ($challenge:expr, $dispatch:ident) => {
        match $challenge {
            #[cfg(feature = "hypergraph")]
            "hypergraph" => $dispatch!(hypergraph),
            #[cfg(feature = "neuralnet_optimizer")]
            "neuralnet_optimizer" => $dispatch!(neuralnet_optimizer),
            _ => anyhow::bail!(
                "Unknown or disabled GPU challenge: {}. Enable its crate feature when building.",
                $challenge
            ),
        }
    };
}
