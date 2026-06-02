# initial_algorithms/

Per-challenge starting code broadcast to agents on a fresh trajectory. There are
two kinds of file here — **stubs** and **seeds** — and they feed two different
agent paths (see `server/tiers.py`, `server/server.py::seed_for_agent`).

## Stubs — `initial_algorithms/<challenge>.rs` (+ optional `.cu`)

The bare `unimplemented!()` starting point. Handed to **frontier-tier explorer**
agents, which are expected to bootstrap a complete algorithm from scratch. This
is the swarm's historical default and is broadcast verbatim at `setup.py create`
(see `read_initial_algorithms`).

`energy_arbitrage/` additionally ships a full **multi-file initial** algorithm
(`mod.rs` + `track_*.rs`) rather than a single stub — that whole directory is the
energy_arbitrage starting implementation.

## Seeds — `initial_algorithms/<challenge>/seeds/<strategy_tag>.rs` (+ optional `.cu`)

Complete, **feasible** simple algorithms. `setup.py create` loads every file
under a `seeds/` directory into the server's seed pool (`read_authored_seeds` →
`POST /api/admin/seed_pool`). The server hands a seed (instead of the stub) to
**standard-tier agents and to any exploiter**, so a weaker model refines working
code rather than failing to bootstrap one.

- The **file name is the `strategy_tag`** (e.g. `greedy.rs` → tag `greedy`). A
  GPU seed pairs `<tag>.rs` with `<tag>.cu`.
- One seed per `(challenge, strategy_tag)` is kept server-side; add more files
  with distinct tags for diversity.

### Current seeds

| challenge            | seed tag       | approach (simple, feasible) | verified |
|----------------------|----------------|-----------------------------|----------|
| knapsack             | `greedy`       | degree/weight greedy fill   | ✅ compiled + ran |
| satisfiability       | `local_search` | GSAT-style flip search      | ✅ compiled + ran |
| job_scheduling       | `greedy`       | active list scheduling      | ✅ compiled + ran |
| vehicle_routing      | `construction` | Solomon I1 insertion (inline)| ✅ compiled + ran |
| energy_arbitrage     | `greedy`       | flow-aware greedy policy (inline)| ✅ compiled + ran |
| hypergraph           | `construction` | round-robin partition       | ⚠️ UNVERIFIED (no CUDA/GPU) |
| vector_search        | `brute_force`  | host exact nearest-neighbour| ⚠️ UNVERIFIED (no CUDA/GPU) |
| neuralnet_optimizer  | `sgd`          | host SGD step               | ⚠️ UNVERIFIED (no CUDA/GPU) |

The three GPU seeds could not be compiled or run in the build environment
(`cudarc` needs `nvcc`, which is absent). Compile and validate them on a CUDA box
before relying on them — see the `!!! UNVERIFIED !!!` header in each file.
