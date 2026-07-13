# initial_algorithms/

Per-challenge starting code broadcast to agents on a fresh trajectory. One
directory per challenge, two slots that feed different agent paths (see
`server/tiers.py`, `server/server.py::seed_for_agent`):

```
initial_algorithms/<challenge>/
  stub/                         # the starting-code slot: mod.rs [+ kernels.cu]
  seeds/<strategy_tag>.rs (.cu) # authored seed pool entries
```

## Stub — `<challenge>/stub/` (`mod.rs` [+ `*.cu`, + sibling modules])

The challenge's starting code — what the server broadcasts on iteration 1 and
on fresh-start trajectory resets (`read_initial_algorithms`,
`server/first_boot.py`). Its sophistication varies by design:

- **CPU challenges** ship the bare `unimplemented!()` placeholder. Handed to
  **frontier-tier explorer** agents, which are expected to bootstrap a
  complete algorithm from scratch — the swarm's historical default.
- **GPU challenges** ship a real working implementation, because
  bootstrapping a *compiling* CUDA kernel from a bare placeholder is too hard
  even for frontier models (see `seed_for_agent` — on GPU challenges every
  agent starts from working code).

A host can replace any challenge's slot with stronger code before
`setup.py create` — by editing it, or by staging a mainnet algorithm with
`scripts/download_algorithm.py` (which replaces ONLY `stub/`; `seeds/` is
never touched). Single- and multi-file algorithms land the same way: `mod.rs`
plus optional `*.cu` / sibling modules, filenames preserved.

## Seeds — `<challenge>/seeds/<strategy_tag>.rs` (+ optional `.cu`)

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
| hypergraph           | `construction` | GPU round-robin partition (kernel) | ✅ compiled + ran (L40/C3), feasible |
| vector_search        | `brute_force`  | GPU exact nearest-neighbour (kernel)| ✅ compiled + ran (L40/C3), feasible (beats baseline) |
| neuralnet_optimizer  | `sgd`          | GPU SGD step (kernel)       | ✅ compiled + ran (L40/C3), feasible (learns) |

All three GPU seeds use real CUDA kernels and were compiled + run on an L40 GPU
via C3 (`nvcc` PTX build + `cudarc` cargo build all succeeded), each feasible on
its smallest track:

- **hypergraph / construction** — `round_robin_partition` kernel assigns
  `partition[i] = i % num_parts`, one thread per node. Feasible, balanced
  partition; the edge-cut is poor by design (score −658834 on `n_h_edges=10000`)
  so reducing the cut is the refiner's job.
- **vector_search / brute_force** — `nearest_neighbor_search` kernel, one thread
  per query doing an exact full-database L2 scan. Feasible and **beats the
  baseline** (score +44520 on `n_queries=10`). Earlier the host-side version
  crashed with `CUDA_ERROR_INVALID_CONTEXT` (its first device op was a
  `memcpy_dtov` on the spawned solver thread, which doesn't bind the context);
  doing the work in a kernel makes `alloc_zeros` the first device op, which binds
  the context. The refiner's job is to make it fast (tiling / pruning / an index).
- **neuralnet_optimizer / sgd** — `sgd_step` kernel computes the weight *update*
  `-lr*clip(grad)` on the GPU. An earlier host version returned the new weights
  instead of the delta, but the scaffold's `apply_parameter_updates_direct` kernel
  *adds* the return value (`params += update`), so it doubled the weights each step
  and scored at the floor (−10M). Returning the delta fixed it: the model now
  learns — loss 0.119 < noise floor 0.356, score +666570 on `n_hidden=4`. Adding
  momentum / Adam / an lr schedule remains the refiner's job.
