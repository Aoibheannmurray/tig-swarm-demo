# C3 CPU implementation plan for TIG swarm demo

## Context checked

- Branch reviewed: `Aoibheannmurray/tig-swarm-demo`, branch `tig-docker`.
- C3 CPU PRs reviewed locally in `/home/sam/personal_projects/c3_workspace/repo/c3-3`:
  - `#612` added neutral `hardware`, `hardware_kind`, `accelerator_kind`, CPU profiles, and Docker accelerator guards.
  - `#619` enabled Nebius CPU offerings.
  - `#621` made CPU availability image-map/operator gated instead of GPU stock-oracle gated.
  - `#623` documented public CPU usage.
  - `#622` matters because GPU routing now uses exact provider offerings.
- C3 CPU profiles currently relevant for public routing:
  - `cpu-d3-4vcpu-16gb`
  - `cpu-e2-4vcpu-16gb`
  - `cpu-e2-48vcpu-192gb`
  - `cpu-d3-96vcpu-384gb`

## Key C3 rules to follow

1. CPU jobs must use `.c3` `hardware: ...`, not `gpu: ...`.
2. Docker jobs should set `docker.requires_accelerator`:
   - `none` for CPU/no-accelerator images.
   - `cuda` for CUDA/GPU images.
3. C3 still only accepts public Docker Hub images for `docker.image`; GHCR images must be mirrored or rebuilt into Docker Hub.
4. C3 injects these env vars into jobs and containers:
   - `C3_HARDWARE_PROFILE`
   - `C3_HARDWARE_KIND`
   - `C3_ACCELERATOR_KIND`
   - `C3_GPU_PROFILE` as a legacy alias
5. On C3 CPU hardware the execution agent omits Docker `--gpus all`; on CUDA hardware it adds it.

## Current TIG state

- TIG challenge GPU flag source of truth is `server/challenges.py`.
- GPU challenges today:
  - `hypergraph`
  - `neuralnet_optimizer`
  - `vector_search`
- CPU challenges today:
  - `satisfiability`
  - `vehicle_routing`
  - `knapsack`
  - `job_scheduling`
  - `energy_arbitrage`
- `scripts/c3_compute.py` has two C3 paths:
  - generic benchmark path (`_write_c3_project`)
  - TIG-native path (`_write_tig_c3_project`)
- Both paths now write `hardware: ...` and set Docker accelerator requirements:
  - CPU/no-accelerator challenges: `docker.requires_accelerator: none`
  - GPU/CUDA challenges: `docker.requires_accelerator: cuda`
- `hardware: auto` now resolves by challenge type:
  - CPU: `cpu-d3-4vcpu-16gb`
  - GPU: `l40`
- The TIG-native C3 image is currently `docker.io/<namespace>/tig-dev-<challenge>:<tig_pin>`.
- Registry check against the default namespace/tag found Docker Hub mirrors only for:
  - `knapsack`
  - `vector_search`
  - `hypergraph`
- Upstream GHCR dev images exist for all eight challenges at `0.0.6`, but C3 cannot pull GHCR directly.

## Implemented in this branch

- C3 hardware resolution is challenge-aware in `scripts/c3_compute.py`.
- Both C3 project writers emit `hardware:` instead of `gpu:`.
- Docker jobs emit `requires_accelerator: none` for CPU and `cuda` for GPU.
- `run_loop.py`, `init_fleet.py`, `fleet.config.example.json`, and `README.md` default to `auto` hardware terminology.
- `_tig_c3_image` rejects default Docker Hub image usage for challenges without a known mirror, unless an explicit `tig_c3_image` override is provided.
- TIG source staging excludes generated outputs (`target`, `.git`, `tig-algorithms/lib`) to keep C3 workspace uploads small enough.
- Seed algorithms for `knapsack`, `vector_search`, and `hypergraph` import current `tig_challenges::*` APIs and expose the `help()` hook expected by the new TIG image build path.
- `scripts/c3_tig_smoke.py` submits the currently mirrored challenges to C3 and supports:
  - `--challenges` for targeted reruns.
  - `--cpu-hardware` / `--gpu-hardware` for type-specific profile overrides.
  - `--stagger-seconds` to avoid C3 upload rate limits while keeping jobs concurrent.

## Remaining work

### Docker image coverage

Minimal path:

- Run/fix `.github/workflows/mirror-tig-images.yml` so it mirrors all eight upstream GHCR `dev` images to Docker Hub:

```text
docker.io/<namespace>/tig-dev-satisfiability:0.0.6
docker.io/<namespace>/tig-dev-vehicle_routing:0.0.6
docker.io/<namespace>/tig-dev-knapsack:0.0.6
docker.io/<namespace>/tig-dev-job_scheduling:0.0.6
docker.io/<namespace>/tig-dev-energy_arbitrage:0.0.6
docker.io/<namespace>/tig-dev-vector_search:0.0.6
docker.io/<namespace>/tig-dev-hypergraph:0.0.6
docker.io/<namespace>/tig-dev-neuralnet_optimizer:0.0.6
```

The default namespace currently lacks five of those images. Full CPU rollout beyond `knapsack` is blocked until the other CPU images exist. Full TIG compatibility is blocked until `neuralnet_optimizer` is mirrored too.

Better production path:

- Build source-baked custom images from `Dockerfile.bench` for all eight challenges and push them to Docker Hub.
- Update `_tig_c3_image` to prefer those custom images.
- Then `_create_tig_workspace` can stop uploading the full TIG monorepo source for C3 jobs and only upload the algorithm, driver, and small runner files.

That is a larger change but it reduces C3 upload size and cold compile cost.

### TIG source packaging

The available local source for smoke tests is under:

```text
/home/sam/personal_projects/tig_2026/tig-monorepo
```

The smoke runner supports this via `--tig-monorepo-path` or `TIG_MONOREPO_PATH`, with the path above as a fallback.

If custom source-baked Docker Hub images are used, this host-side source dependency can be removed from the C3 path.

## Validation status

- Local tests:
  - `python3 scripts/test_benchmark_run_ids.py`
  - `python3 -m py_compile scripts/c3_compute.py scripts/c3_tig_smoke.py scripts/run_loop.py scripts/init_fleet.py`
  - `git diff --check`
- Docker Hub manifests verified for:
  - `docker.io/danieltiagoadams/tig-dev-knapsack:0.0.6`
  - `docker.io/danieltiagoadams/tig-dev-vector_search:0.0.6`
  - `docker.io/danieltiagoadams/tig-dev-hypergraph:0.0.6`
- C3 smoke results:
  - Full `auto` run completed feasible on all three supported challenges:
    - `knapsack` on `cpu-d3-4vcpu-16gb` (`job_1782549883679_lcmpt3`).
    - `vector_search` on `l40` (`job_1782549885766_u35lxf`).
    - `hypergraph` on `l40` (`job_1782549888314_ad3i79`).
  - Earlier probes also completed `knapsack` on `cpu-d3-96vcpu-384gb` (`job_1782547125392_zrlo72`) and `cpu-e2-48vcpu-192gb` (`job_1782548482510_w7nm9n`).

## Suggested rollout order

1. Mirror the five missing Docker Hub images under the configured namespace.
2. Smoke-test each newly mirrored CPU challenge on C3 CPU.
3. Mirror or build the missing `neuralnet_optimizer` image and smoke-test it on C3 GPU.
4. Decide whether to switch C3 from mirrored dev images to source-baked custom images.
