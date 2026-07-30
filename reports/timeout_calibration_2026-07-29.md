# Per-challenge timeout calibration — 2026-07-29

Calibrates `default_timeout` in `server/challenges.py` against how long the
**current mainnet winning algorithm** for each challenge actually takes, at the
**hyperparameters mainnet benchmarkers actually use**. Raw data:
`timeout_calibration_2026-07-29.json` (same directory).

## Method

1. **Winner per challenge** = highest-adoption code on the current mainnet
   block (`get-algorithms` + `block_data.adoption`).
2. **Hyperparameters**: every opow player's precommits were scanned
   (`get-benchmarks`), deduplicated by (track, hyperparameters); per track the
   set with the most benchmarked nonces was used.
3. **Timing**:
   - **CPU challenges** — winner source fetched from the tig-monorepo branch,
     dropped into `src/<challenge>/algorithm/`, built `-r` and run through
     `tig_generator`/`tig_solver` on all 5 mainnet tracks × 3 instances,
     sequentially, on this 8-core host. (`titan_v6` links the runtime's
     `__fuel_remaining` symbol; a never-decremented stub was provided.)
   - **GPU challenges** — real mainnet harness (tig-runtime + tig-verifier,
     fuel 5e12) on a C3 L40 via `c3_tig_bench.py`, 3 nonces/track.
   - **Cross-check** — satisfiability and vehicle_routing also run under the
     instrumented mainnet harness on C3 (fuel instrumentation + 3 workers make
     those ~2–3.7× slower than native; swarm solvers run uninstrumented, so
     native times are the calibration basis).
4. **Timeout** = slowest track's max plus headroom (slower contributor
   machines, concurrent bench workers), rounded; final values set by the host.
   Default instances per track raised 2 → 5 alongside
   (`hostadmin/swarm.py:DEFAULT_INSTANCES_PER_TRACK` / `DEFAULT_TRACKS_PER_CHALLENGE`).

## Measured (per-instance seconds, max over 3 runs)

| challenge | winner | slowest track | max s | other tracks | old → new timeout |
|---|---|---|---|---|---|
| satisfiability | sat_imp_v4 | n_vars=10000,ratio=4267 | **408** | 2–283 (100k tracks: 19/278 — benchmarkers cap fuel there) | 300 → **420** |
| vehicle_routing | hgs_advance | n_nodes=1000 | **136** | 46–111 | 260 → **200** |
| knapsack | superfast_knap_v1 | n_items=5000,budget=25 | **15.6** | 0.4–5.5 | 60 → **30** |
| job_scheduling | adaptive_js_v9 | n=50,s=job_shop | **57** | 1–31 | 260 → **90** |
| energy_arbitrage | titan_v6 | s=capstone | **21** | 0.1–6.8 | 30 → **45** |
| hypergraph (L40) | sigma_freud_v8 | n_h_edges=100000 | **137** | 3.5–103 | 60 → **180** |
| neuralnet_optimizer (L40) | prometheus_aidda | n_hidden=18 | **46** | 7.5–31 | 120 → **90** |
| vector_search (L40) | there_v10 | n_queries=13000 | **0.7** | 0.3–0.5 | 60 → **30** (floor for GPU warmup) |

C3 instrumented cross-check (mainnet-harness wall time, worst track):
satisfiability ~799s, vehicle_routing ~502s — i.e. mainnet benchmarkers give
these winners even longer than the new defaults; the native numbers above are
what the swarm's uninstrumented builds need.

## Notes

- Old defaults were wrong in both directions: satisfiability (300s) and
  hypergraph (60s) would **kill the mainnet winner before it finishes**
  (408s / 137s), while job_scheduling (260s vs 57s needed) and vector_search
  (60s vs 0.7s) burned benchmark wall-clock for nothing.
- Changing `timeout` on an **existing** swarm changes the config fingerprint
  (`db.config_fingerprint`) and marks old scores incomparable — these defaults
  only flow into newly created swarms (`setup.py create` / control-ui host
  wizard). Existing `swarm.admin*.json` files were left untouched.
- Timings are per-instance and sequential; the local bench runs instances
  concurrently (`bench_workers`), which adds some contention — covered by the
  headroom factor.
