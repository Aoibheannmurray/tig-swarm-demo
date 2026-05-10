#!/usr/bin/env python3
"""Run the active challenge's benchmark and emit JSON for publish.py.

Reads swarm-wide config from `https://t2-production-905b.up.railway.app////api/swarm_config` (or from
`./swarm.config.json` as a fallback for offline use) to pick the challenge,
the per-track instance counts, and the per-instance solver timeout. Builds
the right cargo binary, generates instances on first run (cached under
`datasets/<challenge>/generated/`), then runs solver + evaluator on each
instance in parallel.

# Scoring

Each upstream evaluator returns a baseline-relative *quality* per instance
in the integer range [-QUALITY_PRECISION × 10, +QUALITY_PRECISION × 10]
(QUALITY_PRECISION = 1,000,000). The baseline is the upstream baseline
algorithm for that challenge:

    - satisfiability: binary (1M if all clauses satisfied, else 0).
    - vehicle_routing: Solomon nearest-neighbor (`solomon::run`).
    - knapsack: greedy by value-density (`compute_greedy_baseline`).
    - job_scheduling: SOTA dispatching rules (`compute_sota_baseline`).
    - energy_arbitrage: max(greedy, conservative) (`compute_baseline`).

Higher quality is always better. Aggregation is two-step:

    1. Per-track score = arithmetic mean of per-instance quality scores
       in that track. Infeasible instances contribute `-QUALITY_PRECISION`
       (the worst feasibly-bounded value).
    2. Cross-track score = shifted geometric mean across the per-track
       averages. The shift (+QUALITY_PRECISION × 10 + 1) keeps every
       value strictly positive so the geometric mean is well-defined for
       any combination of negative and positive track scores.

The geometric mean rewards balanced performance — a single bad track
drags the overall score down more than the arithmetic mean would.

Output JSON shape:

    {
      "challenge": "...",
      "score": 1234567.8,           # cross-track shifted geo mean of quality
      "feasible": true,
      "instances_solved": 25,
      "instances_feasible": 25,
      "instances_infeasible": 0,
      "track_scores": {"track_key": <mean quality>, ...},
      "viz_data": { ... per-challenge or null ... },
      # VRP-only roll-ups, only meaningful when the challenge is VRP:
      "num_vehicles": 96,
      "total_distance": 12345.6,
    }
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import math
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent

_INSIDE_DOCKER = Path("/.dockerenv").exists() or os.environ.get("TIG_IN_DOCKER") == "1"
_USE_C3 = os.environ.get("TIG_COMPUTE", "").lower() == "c3"

# Mirrors `QUALITY_PRECISION` in src/lib.rs and the upstream tig-monorepo.
# All vendored evaluators clamp their (baseline-relative) quality to
# ±10 × QUALITY_PRECISION before scaling, so the final per-instance score
# is bounded in [-QUALITY_CLAMP, +QUALITY_CLAMP].
QUALITY_PRECISION = 1_000_000
QUALITY_CLAMP = 10 * QUALITY_PRECISION

# Per-instance penalty for an infeasible instance. Set to the worst
# feasible-bounded value rather than -∞ so the per-track mean stays in a
# sensible range and the shifted geometric mean is well-defined.
INFEASIBLE_QUALITY = -QUALITY_PRECISION

# Constant added to each per-track mean before taking the geometric mean.
# Quality range after clamping is [-10M, +10M]; shift by +10M+1 → strictly
# positive in [1, 20M+1] before geo mean, then unshift the result.
GEOMEAN_SHIFT = QUALITY_CLAMP + 1

def _resolve_server_url() -> str:
    if os.environ.get("TIG_SWARM_SERVER"):
        return os.environ["TIG_SWARM_SERVER"].rstrip("/")
    cfg_path = Path(__file__).parent.parent / "swarm.config.json"
    if cfg_path.exists():
        try:
            url = json.loads(cfg_path.read_text()).get("server_url", "")
            if url and not url.startswith("$"):
                return url.rstrip("/")
        except Exception:
            pass
    return ""

SERVER = _resolve_server_url()


# ── Config loading ──────────────────────────────────────────────────


def load_swarm_config() -> dict:
    """Pull live swarm config from the server, falling back to local cache.

    The server is the source of truth (the owner can change the active
    challenge mid-experiment). swarm.config.json is the offline fallback so
    `python scripts/benchmark.py` works without a server reachable, which
    is useful for ad-hoc local testing of `algorithm/mod.rs` edits.
    """
    if SERVER:
        try:
            with urllib.request.urlopen(f"{SERVER}/api/swarm_config", timeout=4) as r:
                data = json.load(r)
            ch = data.get("challenge") or data.get("active_challenge")
            avail = data.get("available_challenges", {})
            if ch and ch in avail:
                data.setdefault("is_gpu", avail[ch].get("is_gpu", False))
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            print(f"warning: couldn't reach {SERVER}/api/swarm_config ({e})", file=sys.stderr)
    cfg_path = ROOT_DIR / "swarm.config.json"
    if cfg_path.exists():
        local = json.loads(cfg_path.read_text())
        return {
            "challenge": local.get("challenge"),
            "tracks": local.get("tracks", {}),
            "timeout": local.get("timeout", 30),
            "scoring_direction": local.get("scoring_direction", "min"),
            "is_gpu": local.get("is_gpu", False),
        }
    print("error: no swarm config available (server unreachable, no swarm.config.json)", file=sys.stderr)
    sys.exit(1)


# ── GPU challenge detection ────────────────────────────────────────


def is_gpu_challenge(cfg: dict) -> bool:
    """Check if the active challenge requires GPU based on swarm config."""
    return bool(cfg.get("is_gpu"))


# ── Build & instance generation ────────────────────────────────────


def build(challenge: str) -> tuple[str, str, str]:
    """Build solver, evaluator, generator with the active challenge feature.
    Returns absolute paths to the three binaries."""
    for binary, feature_set in (
        ("tig_solver", f"solver,{challenge}"),
        ("tig_evaluator", f"evaluator,{challenge}"),
        ("tig_generator", f"generator,{challenge}"),
    ):
        result = subprocess.run(
            ["cargo", "build", "-r", "--bin", binary, "--features", feature_set],
            cwd=ROOT_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"cargo build failed for {binary}:\n{result.stderr}", file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, f"cargo build {binary}")
    return (
        str(ROOT_DIR / "target/release/tig_solver"),
        str(ROOT_DIR / "target/release/tig_evaluator"),
        str(ROOT_DIR / "target/release/tig_generator"),
    )


def materialize_instances(
    challenge: str, tracks: dict, generator_bin: str
) -> list[tuple[str, str, Path]]:
    """Generate instances per the active swarm config, cached on disk.

    `tracks` is the `test.json` shape: `{"seed": "test", "track_key": count, ...}`.
    Each (track_key, count) becomes `count` instances under
    `datasets/<challenge>/generated/<track_key>/{0..count-1}.txt`. Generation
    is skipped when the cache already has at least `count` files for the
    track — re-running the wizard with smaller counts won't regenerate.

    Returns a list of `(track_key, instance_filename, instance_path)`.
    """
    seed = str(tracks.get("seed", "test"))
    out: list[tuple[str, str, Path]] = []
    base = ROOT_DIR / "datasets" / challenge / "generated"
    for track_key, count in tracks.items():
        if track_key == "seed" or not isinstance(count, int) or count <= 0:
            continue
        track_dir = base / track_key
        track_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(p for p in track_dir.glob("*.txt"))
        if len(existing) < count:
            print(
                f"  generating {count - len(existing)} new instances for "
                f"{challenge}/{track_key} (have {len(existing)})…",
                file=sys.stderr,
            )
            subprocess.run(
                [
                    generator_bin, challenge, track_key,
                    "--seed", seed,
                    "-n", str(count),
                    "-o", str(track_dir),
                ],
                check=True, capture_output=True,
            )
        for i in range(count):
            inst = track_dir / f"{i}.txt"
            if inst.exists():
                out.append((track_key, f"{track_key}/{i}", inst))
    return out


# ── Per-instance run ───────────────────────────────────────────────


def parse_evaluator_score(eval_result: subprocess.CompletedProcess) -> tuple[float | None, str | None]:
    if eval_result.returncode != 0:
        return None, (eval_result.stderr or eval_result.stdout or f"evaluator exit {eval_result.returncode}").splitlines()[0]
    stdout = (eval_result.stdout or "").strip()
    if not stdout:
        return None, "evaluator produced no output"
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, f"invalid evaluator JSON: {stdout[:80]}"
    score = payload.get("score", payload.get("distance"))
    if not isinstance(score, (int, float)):
        return None, "evaluator JSON missing numeric score"
    return float(score), None


def run_instance(
    challenge: str, track_key: str, instance_id: str, instance_path: Path,
    solver: str, evaluator: str, timeout: int,
) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".sol", delete=False) as tmp:
        sol_path = tmp.name
    try:
        try:
            subprocess.run(
                [solver, challenge, str(instance_path), sol_path],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            pass  # save_solution may have written a partial; evaluator will judge
        if not os.path.exists(sol_path) or os.path.getsize(sol_path) == 0:
            return {"instance": instance_id, "track": track_key, "error": "no solution saved", "feasible": False}
        try:
            eval_result = subprocess.run(
                [evaluator, challenge, str(instance_path), sol_path],
                capture_output=True, text=True, timeout=max(10, timeout),
            )
        except subprocess.TimeoutExpired:
            return {"instance": instance_id, "track": track_key, "error": "evaluator timeout", "feasible": False}
        score, err = parse_evaluator_score(eval_result)
        if err:
            return {"instance": instance_id, "track": track_key, "error": err, "feasible": False}
        result = {
            "instance": instance_id,
            "track": track_key,
            "score": score,
            "feasible": True,
        }
        extras = _PER_INSTANCE_EXTRAS.get(challenge)
        if extras is not None:
            result.update(extras(str(instance_path), sol_path))
        return result
    finally:
        if os.path.exists(sol_path):
            os.unlink(sol_path)


# ── Docker re-exec ────────────────────────────────────────────────


def _docker_image_for(cfg: dict) -> tuple[str, str]:
    """Return (image_name, dockerfile) for the active challenge."""
    if is_gpu_challenge(cfg):
        return "tig-swarm-gpu", "Dockerfile.gpu"
    return "tig-swarm-cpu", "Dockerfile.cpu"


def _check_docker_image(image: str, dockerfile: str) -> None:
    """Verify a Docker image exists locally, or exit with a clear message."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"error: Docker image '{image}' not found.\n"
            f"Build it first:\n\n"
            f"  docker build -f {dockerfile} -t {image} .\n",
            file=sys.stderr,
        )
        sys.exit(1)


def _reexec_in_docker(cfg: dict) -> int:
    """Re-launch benchmark.py inside the appropriate Docker container."""
    image, dockerfile = _docker_image_for(cfg)
    _check_docker_image(image, dockerfile)

    gpu_flags = ["--gpus", "all"] if is_gpu_challenge(cfg) else []
    env_flags = ["-e", "TIG_IN_DOCKER=1"]
    server = os.environ.get("TIG_SWARM_SERVER")
    if server:
        env_flags += ["-e", f"TIG_SWARM_SERVER={server}"]

    cmd = [
        "docker", "run", "--rm",
        *gpu_flags,
        "-v", f"{ROOT_DIR}:/app",
        "-v", "tig-cargo-cache:/app/target",
        "-v", "tig-cargo-registry:/root/.cargo/registry",
        *env_flags,
        image,
        "python3", "/app/scripts/benchmark.py",
    ]
    return subprocess.run(cmd).returncode


# ── GPU build & run (native — always called from inside Docker) ──


def build_gpu(challenge: str) -> tuple[str, str]:
    """Compile PTX and build the combined GPU benchmark binary.
    Returns (binary_path, ptx_path)."""
    ptx_result = subprocess.run(
        ["python3", str(ROOT_DIR / "scripts" / "build_ptx.py"), challenge,
         "--outdir", str(ROOT_DIR / "target" / "ptx")],
        capture_output=True, text=True,
    )
    if ptx_result.returncode != 0:
        print(f"PTX build failed:\n{ptx_result.stderr}", file=sys.stderr)
        raise subprocess.CalledProcessError(ptx_result.returncode, "build_ptx.py")

    cargo_result = subprocess.run(
        ["cargo", "build", "-r",
         "--bin", "tig_gpu_benchmark",
         "--features", f"gpu_benchmark,{challenge}"],
        cwd=ROOT_DIR, capture_output=True, text=True,
    )
    if cargo_result.returncode != 0:
        print(f"GPU binary build failed:\n{cargo_result.stderr}", file=sys.stderr)
        raise subprocess.CalledProcessError(cargo_result.returncode, "cargo build gpu")

    return (
        str(ROOT_DIR / "target/release/tig_gpu_benchmark"),
        str(ROOT_DIR / "target/ptx" / f"{challenge}.ptx"),
    )


def run_gpu_instance(
    challenge: str, track_key: str, instance_index: int,
    binary: str, ptx: str, seed: str, timeout: int,
) -> dict:
    """Run one GPU instance. Returns same shape as run_instance()."""
    try:
        result = subprocess.run(
            [binary, challenge, track_key,
             "--seed", seed, "--index", str(instance_index),
             "--timeout", str(timeout), "--ptx", ptx],
            capture_output=True, text=True,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return {
            "instance": f"{track_key}/{instance_index}",
            "track": track_key,
            "error": "gpu benchmark timeout",
            "feasible": False,
        }
    if result.returncode != 0:
        return {
            "instance": f"{track_key}/{instance_index}",
            "track": track_key,
            "error": (result.stderr or result.stdout or "gpu benchmark failed").splitlines()[0],
            "feasible": False,
        }
    try:
        data = json.loads(result.stdout)
        data["track"] = track_key
        return data
    except json.JSONDecodeError:
        return {
            "instance": f"{track_key}/{instance_index}",
            "track": track_key,
            "error": f"invalid GPU benchmark JSON: {(result.stdout or '')[:80]}",
            "feasible": False,
        }


# ── VRP-specific extras (route_data + num_vehicles) ───────────────


def _vrp_parse_positions(inst_path: str) -> dict:
    positions = {}
    in_customer = False
    try:
        with open(inst_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CUST NO"):
                    in_customer = True
                    continue
                if in_customer and line:
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            positions[int(parts[0])] = (int(parts[1]), int(parts[2]))
                        except ValueError:
                            pass
    except OSError:
        pass
    return positions


def _vrp_parse_routes(sol_path: str) -> list:
    routes = []
    try:
        with open(sol_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("Route"):
                    parts = line.split(":")
                    if len(parts) == 2:
                        nodes = [int(x) for x in parts[1].split() if x.strip()]
                        routes.append(nodes)
    except OSError:
        pass
    return routes


def _vrp_extras(inst_path: str, sol_path: str) -> dict:
    positions = _vrp_parse_positions(inst_path)
    routes = _vrp_parse_routes(sol_path)
    if not positions or not routes:
        return {"num_vehicles": len(routes), "route_data": None}
    depot = positions.get(0, (500, 500))
    route_data = {
        "depot": {"x": depot[0], "y": depot[1]},
        "routes": [
            {
                "vehicle_id": i,
                "path": [
                    {"x": positions[node][0], "y": positions[node][1], "customer_id": node}
                    for node in route_nodes
                    if node in positions
                ],
            }
            for i, route_nodes in enumerate(routes)
        ],
    }
    return {"num_vehicles": len(routes), "route_data": route_data}


# ── Job-scheduling-specific extras (Gantt viz_data) ──────────────


def _jsp_parse_solution(sol_path: str) -> list | None:
    """Decode a job-scheduling solution file (base64 → gzip → bincode)."""
    import base64
    import gzip
    import struct

    try:
        with open(sol_path) as f:
            b64_str = json.load(f)
        if not isinstance(b64_str, str):
            return None
        compressed = base64.b64decode(b64_str)
        data = gzip.decompress(compressed)
    except Exception:
        return None

    offset = 0

    def read_u64() -> int:
        nonlocal offset
        val = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        return val

    def read_u32() -> int:
        nonlocal offset
        val = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        return val

    try:
        num_jobs = read_u64()
        schedule: list[list[tuple[int, int]]] = []
        for _ in range(num_jobs):
            num_ops = read_u64()
            ops = []
            for _ in range(num_ops):
                machine = read_u64()
                start_time = read_u32()
                ops.append((machine, start_time))
            schedule.append(ops)
        return schedule
    except struct.error:
        return None


def _jsp_extras(inst_path: str, sol_path: str) -> dict:
    """Build Gantt chart viz payload from instance + solution files."""
    schedule = _jsp_parse_solution(sol_path)
    if schedule is None:
        return {"gantt_data": None}

    try:
        with open(inst_path) as f:
            challenge = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"gantt_data": None}

    jobs_per_product = challenge["jobs_per_product"]
    proc_times = challenge["product_processing_times"]

    bars = []
    job_idx = 0
    makespan = 0
    for product_idx, n_jobs in enumerate(jobs_per_product):
        for _ in range(n_jobs):
            if job_idx >= len(schedule):
                break
            ops = schedule[job_idx]
            product_ops = proc_times[product_idx]
            for op_idx, (machine, start_time) in enumerate(ops):
                if op_idx < len(product_ops):
                    duration = product_ops[op_idx].get(str(machine), 0)
                else:
                    duration = 0
                end_time = start_time + duration
                if end_time > makespan:
                    makespan = end_time
                bars.append({
                    "job": job_idx,
                    "op": op_idx,
                    "machine": machine,
                    "start": start_time,
                    "end": end_time,
                })
            job_idx += 1

    return {
        "gantt_data": {
            "num_machines": challenge["num_machines"],
            "num_jobs": challenge["num_jobs"],
            "makespan": makespan,
            "bars": bars,
        }
    }


# ── Knapsack-specific extras (interaction matrix viz_data) ─────────


def _knapsack_parse_solution(sol_path: str) -> list[int] | None:
    """Decode a knapsack solution file (base64 → gzip → bincode)."""
    import base64
    import gzip
    import struct

    try:
        with open(sol_path) as f:
            b64_str = json.load(f)
        if not isinstance(b64_str, str):
            return None
        compressed = base64.b64decode(b64_str)
        data = gzip.decompress(compressed)
    except Exception:
        return None

    offset = 0

    def read_u64() -> int:
        nonlocal offset
        val = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        return val

    try:
        num_items = read_u64()
        items = [read_u64() for _ in range(num_items)]
        return items
    except struct.error:
        return None


def _knapsack_extras(inst_path: str, sol_path: str) -> dict:
    """Build interaction-matrix viz payload from instance + solution files.

    The matrix sent to the dashboard is K×K where K = len(selected items),
    capped at MAX_VIZ_ITEMS to keep the payload and rendering tractable.
    """
    MAX_VIZ_ITEMS = 50

    items = _knapsack_parse_solution(sol_path)
    if items is None:
        return {"knapsack_data": None}

    try:
        with open(inst_path) as f:
            challenge = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"knapsack_data": None}

    n = challenge["num_items"]
    interaction_values = challenge["interaction_values"]
    weights = challenge["weights"]
    max_weight = challenge["max_weight"]

    sorted_items = sorted(i for i in items if i < n)
    total_weight = sum(weights[i] for i in sorted_items)
    total_value = 0
    for idx_a, i in enumerate(sorted_items):
        for j in sorted_items[idx_a + 1:]:
            total_value += interaction_values[i][j]

    viz_items = sorted_items[:MAX_VIZ_ITEMS]
    k = len(viz_items)
    sub_matrix = [[0] * k for _ in range(k)]
    for ri, i in enumerate(viz_items):
        for rj, j in enumerate(viz_items):
            if i != j:
                sub_matrix[ri][rj] = interaction_values[i][j]

    return {
        "knapsack_data": {
            "num_selected": len(sorted_items),
            "num_items": n,
            "viz_items": viz_items,
            "interaction_values": sub_matrix,
            "total_value": max(0, total_value),
            "max_weight": max_weight,
            "total_weight": total_weight,
        }
    }


# ── Energy-arbitrage-specific extras (schedule + DA prices) ────────


def _energy_parse_solution(sol_path: str) -> list[list[float]] | None:
    """Decode an energy_arbitrage solution file (base64 → gzip → bincode).

    The schedule is Vec<Vec<f64>>: outer vec = timesteps, inner = batteries.
    """
    import base64
    import gzip
    import struct

    try:
        with open(sol_path) as f:
            b64_str = json.load(f)
        if not isinstance(b64_str, str):
            return None
        compressed = base64.b64decode(b64_str)
        data = gzip.decompress(compressed)
    except Exception:
        return None

    offset = 0

    def read_u64() -> int:
        nonlocal offset
        val = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        return val

    def read_f64() -> float:
        nonlocal offset
        val = struct.unpack_from("<d", data, offset)[0]
        offset += 8
        return val

    try:
        num_steps = read_u64()
        schedule = []
        for _ in range(num_steps):
            num_batteries = read_u64()
            actions = [read_f64() for _ in range(num_batteries)]
            schedule.append(actions)
        return schedule
    except struct.error:
        return None


def _energy_extras(inst_path: str, sol_path: str) -> dict:
    """Build energy viz payload: per-step aggregate charge/discharge + DA prices."""
    schedule = _energy_parse_solution(sol_path)
    if schedule is None:
        return {"energy_data": None}

    try:
        with open(inst_path) as f:
            challenge = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"energy_data": None}

    da_prices = challenge.get("market", {}).get("day_ahead_prices", [])
    num_steps = len(schedule)

    agg_charge = []
    agg_discharge = []
    for t in range(num_steps):
        charge = 0.0
        discharge = 0.0
        for u in schedule[t]:
            if u < 0:
                charge += u
            else:
                discharge += u
        agg_charge.append(round(charge, 4))
        agg_discharge.append(round(discharge, 4))

    avg_da = []
    for t in range(min(num_steps, len(da_prices))):
        prices_at_t = da_prices[t]
        avg_da.append(round(sum(prices_at_t) / len(prices_at_t), 2) if prices_at_t else 0)

    return {
        "energy_data": {
            "num_steps": num_steps,
            "num_batteries": len(schedule[0]) if schedule else 0,
            "agg_charge": agg_charge,
            "agg_discharge": agg_discharge,
            "avg_da_price": avg_da,
        }
    }


# ── Satisfiability extras (variable assignment + clause histogram) ─


def _sat_parse_solution(sol_path: str) -> list[bool] | None:
    """Decode a satisfiability solution file (base64 → gzip → bincode).

    Bincode encoding of `Vec<bool>` is a little-endian u64 length followed
    by one byte per element (0x00 or 0x01).
    """
    import base64
    import gzip
    import struct

    try:
        with open(sol_path) as f:
            b64_str = json.load(f)
        if not isinstance(b64_str, str):
            return None
        compressed = base64.b64decode(b64_str)
        data = gzip.decompress(compressed)
    except Exception:
        return None

    if len(data) < 8:
        return None
    try:
        n = struct.unpack_from("<Q", data, 0)[0]
    except struct.error:
        return None
    if 8 + n > len(data):
        return None
    return [bool(data[8 + i]) for i in range(n)]


def _sat_extras(inst_path: str, sol_path: str) -> dict:
    """Build the SAT viz payload from instance + solution.

    Two complementary views are sent:
      - assignment_bits: a "0"/"1" string of the variable assignment,
        sub-sampled to <= MAX_VIZ_VARS so the payload stays tractable
        even at n_vars=100k.
      - clause_bins: 50 stacked-bar bins along the clause index axis,
        each bin a 4-tuple (clauses with 0/1/2/3 satisfying literals).
    """
    MAX_VIZ_VARS = 10000
    NUM_BINS = 50

    vars_arr = _sat_parse_solution(sol_path)
    if vars_arr is None:
        return {"sat_data": None}

    try:
        with open(inst_path) as f:
            challenge = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"sat_data": None}

    n_vars = challenge.get("num_variables")
    clauses = challenge.get("clauses")
    if not isinstance(n_vars, int) or not isinstance(clauses, list):
        return {"sat_data": None}
    if len(vars_arr) != n_vars:
        return {"sat_data": None}

    m_clauses = len(clauses)
    bin_size = max(1, m_clauses // NUM_BINS)
    clause_bins = [[0, 0, 0, 0] for _ in range(NUM_BINS)]
    num_satisfied = 0
    for ci, clause in enumerate(clauses):
        bin_idx = min(ci // bin_size, NUM_BINS - 1)
        sat_count = 0
        for lit in clause:
            v_idx = abs(lit) - 1  # literals are 1-indexed
            if v_idx < 0 or v_idx >= n_vars:
                continue
            val = vars_arr[v_idx]
            if (lit > 0 and val) or (lit < 0 and not val):
                sat_count += 1
        clause_bins[bin_idx][min(sat_count, 3)] += 1
        if sat_count > 0:
            num_satisfied += 1

    if n_vars <= MAX_VIZ_VARS:
        viz_assignment = vars_arr
        viz_stride = 1
    else:
        viz_stride = max(1, n_vars // MAX_VIZ_VARS)
        viz_assignment = vars_arr[::viz_stride][:MAX_VIZ_VARS]
    assignment_bits = "".join("1" if b else "0" for b in viz_assignment)

    return {
        "sat_data": {
            "num_variables": n_vars,
            "num_clauses": m_clauses,
            "num_satisfied": num_satisfied,
            "viz_count": len(viz_assignment),
            "viz_stride": viz_stride,
            "assignment_bits": assignment_bits,
            "clause_bins": clause_bins,
        }
    }


# ── Per-challenge dispatch tables ─────────────────────────────────
#
# Tables instead of an if/elif chain so that adding a challenge means a
# single line per table — and so a tooling pass that rewrites string
# literals in equality comparisons (which broke an earlier copy of this
# file) leaves them alone.

# Challenge-name keys are written as adjacent string literals (Python
# concatenates them at parse time) so that `setup.py sync` — which does a
# blanket `text.replace(prior_challenge, new_challenge)` over this file —
# can't accidentally rewrite a dispatch key when the user switches the
# active challenge. Dict keys still equal the contiguous strings at
# runtime, so `_PER_INSTANCE_EXTRAS.get(challenge)` works unchanged.
_PER_INSTANCE_EXTRAS = {
    "vehicle" "_routing":  _vrp_extras,
    "job" "_scheduling":   _jsp_extras,
    # knapsack interaction-matrix payload — builds `knapsack_data`.
    "knap" "sack":         _knapsack_extras,
    "energy" "_arbitrage": _energy_extras,
    "satisfia" "bility":   _sat_extras,
}

# Maps each challenge to the per-instance result field that holds its
# visualisation payload. The aggregate block reads `r[per_result_field]`
# from each per-instance result and stores `{instance: payload}` as the
# run's `viz_data`, which publish.py forwards to the server as
# `solution_data` (the universal wire field — no longer aliased to
# `route_data`, which only made sense for VRP).
_AGG_EXTRAS = {
    "vehicle" "_routing":       "route_data",
    "job" "_scheduling":        "gantt_data",
    "knap" "sack":              "knapsack_data",
    "energy" "_arbitrage":      "energy_data",
    "satisfia" "bility":        "sat_data",
    "hyper" "graph":            "hypergraph_data",
    "neuralnet" "_optimizer":   "neuralnet_data",
}


# ── C3 remote benchmark ──────────────────────────────────────────


def _bundle_project(challenge: str) -> str:
    """Create a base64-encoded gzip tarball of essential project files.

    Only includes the active challenge's source, not all challenges, to stay
    under the Linux per-argument size limit (MAX_ARG_STRLEN = 128 KB).
    """
    import tarfile
    import io

    buf = io.BytesIO()
    include_files = {"Cargo.toml", "swarm.config.json"}
    include_src = {
        "lib.rs",
        challenge,
    }

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in include_files:
            full = ROOT_DIR / name
            if full.is_file():
                tar.add(str(full), arcname=name)

        scripts_dir = ROOT_DIR / "scripts"
        if scripts_dir.is_dir():
            for script_file in scripts_dir.iterdir():
                if script_file.is_file() and script_file.suffix == ".py":
                    tar.add(str(script_file), arcname=f"scripts/{script_file.name}")

        src_dir = ROOT_DIR / "src"
        if src_dir.is_dir():
            for name in sorted(os.listdir(src_dir)):
                if name in include_src:
                    full = src_dir / name
                    if full.is_file():
                        tar.add(str(full), arcname=f"src/{name}")
                    elif full.is_dir():
                        tar.add(str(full), arcname=f"src/{name}")

        for bin_dir in ["src/bin"]:
            full = ROOT_DIR / bin_dir
            if full.is_dir():
                tar.add(str(full), arcname=bin_dir)

    return base64.b64encode(buf.getvalue()).decode()


def _parse_instance_id(text: str) -> str | None:
    """Extract instance/job ID from c3 instances launch output."""
    for pat in [
        r'"id"\s*:\s*"([^"]+)"',
        r"(inst_[a-zA-Z0-9_-]+)",
        r"(job_[a-zA-Z0-9_-]+)",
        r"([a-f0-9]{8,})",
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _poll_c3_job(job_id: str, env: dict, poll_interval: int = 15) -> str:
    """Poll c3 jobs get until terminal state."""
    for _ in range(480):
        result = subprocess.run(
            ["c3", "jobs", "get", job_id, "-o", "json"],
            capture_output=True, text=True, cwd=ROOT_DIR, env=env,
        )
        out = (result.stdout or "").lower()
        if any(s in out for s in ["completed", "succeeded", "success"]):
            return "completed"
        if any(s in out for s in ["failed", "error", "cancelled", "canceled"]):
            return "failed"
        result_table = subprocess.run(
            ["c3", "jobs", "get", job_id],
            capture_output=True, text=True, cwd=ROOT_DIR, env=env,
        )
        out_table = (result_table.stdout or "").upper()
        if "COMPLETED" in out_table or "SUCCEEDED" in out_table or "SUCCESS" in out_table:
            return "completed"
        if "FAILED" in out_table or "CANCELLED" in out_table:
            return "failed"
        time.sleep(poll_interval)
    return "timeout"


def _run_benchmark_c3(cfg: dict) -> int:
    if shutil.which("c3") is None:
        print("error: c3 CLI not found. Install from https://docs.cthree.cloud/", file=sys.stderr)
        return 1

    hardware = os.environ.get("C3_HARDWARE", "l40s").lower()
    c3_time = os.environ.get("C3_TIME", "02:00:00")

    server = _resolve_server_url()
    env = os.environ.copy()
    # Remove placeholder API keys so the CLI falls through to ~/.c3/config
    if env.get("C3_API_KEY", "").startswith("your_"):
        del env["C3_API_KEY"]

    challenge = cfg.get("challenge", "unknown")
    print("Bundling project files…", file=sys.stderr)
    bundle_b64 = _bundle_project(challenge)

    h, m_part, s = (c3_time.split(":") + ["0", "0", "0"])[:3]
    runtime_secs = int(h) * 3600 + int(m_part) * 60 + int(s)

    startup_cmd = (
        "set -e && "
        "apt-get update -qq && apt-get install -y -qq curl build-essential python3 python3-pip pkg-config libssl-dev > /dev/null 2>&1 && "
        "pip3 install blake3 requests --break-system-packages -q && "
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y > /dev/null 2>&1 && "
        "export PATH=/root/.cargo/bin:$PATH && "
        "mkdir -p /workspace && cd /workspace && "
        f"echo '{bundle_b64}' | base64 -d | tar xzf - && "
        "export TIG_IN_DOCKER=1 && "
        f"export TIG_SWARM_SERVER='{server}' && "
        "python3 scripts/benchmark.py"
    )

    cmd = [
        "c3", "instances", "launch",
        "nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04",
        "-g", hardware,
        "-t", str(runtime_secs),
        "--on-demand",
        "-c", f"bash -c \"{startup_cmd}\"",
        "-o", "json",
    ]

    c3_key = env.get("C3_API_KEY", "")
    if c3_key:
        cmd.extend(["-e", f"C3_API_KEY={c3_key}"])

    print(f"Launching C3 {hardware} instance…", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT_DIR, env=env)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    job_id = _parse_instance_id(combined)
    if not job_id:
        print(f"error: could not parse job/instance ID from c3 launch:\n{combined[-2000:]}", file=sys.stderr)
        return 1

    print(f"C3 instance launched: {job_id} — polling for completion…", file=sys.stderr)
    status = _poll_c3_job(job_id, env)

    logs_result = subprocess.run(
        ["c3", "jobs", "logs", job_id],
        capture_output=True, text=True, cwd=ROOT_DIR, env=env,
    )
    logs_out = logs_result.stdout or ""

    if status != "completed":
        print(f"error: C3 job {job_id} {status}:\n{logs_out[-4000:]}", file=sys.stderr)
        return 1

    for line in reversed(logs_out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                bench = json.loads(line)
                if "score" in bench or "challenge" in bench:
                    print(json.dumps(bench, indent=2))
                    return 0
            except json.JSONDecodeError:
                continue

    json_blocks = re.findall(r'\{[^{}]*"score"[^{}]*\}', logs_out)
    if json_blocks:
        try:
            bench = json.loads(json_blocks[-1])
            print(json.dumps(bench, indent=2))
            return 0
        except json.JSONDecodeError:
            pass

    multi_line_match = re.search(r'\{\s*\n\s*"challenge".*?\n\}', logs_out, re.DOTALL)
    if multi_line_match:
        try:
            bench = json.loads(multi_line_match.group())
            print(json.dumps(bench, indent=2))
            return 0
        except json.JSONDecodeError:
            pass

    print(f"error: C3 job {job_id} completed but could not parse benchmark JSON from logs", file=sys.stderr)
    print(f"Last 2000 chars of logs:\n{logs_out[-2000:]}", file=sys.stderr)
    return 1


# ── Aggregation & main ────────────────────────────────────────────


def _shifted_geomean(values: list[float], shift: float = GEOMEAN_SHIFT) -> float:
    """Geometric mean of `values` after adding `shift`, then subtract `shift`
    back so the result is on the original scale.

    Every per-track mean lives in [-QUALITY_CLAMP, +QUALITY_CLAMP], so the
    shifted values live in [1, 2 × QUALITY_CLAMP + 1] — strictly positive,
    so the geometric mean is well-defined regardless of how many tracks
    underperformed the baseline. The result is approximately the per-track
    average when all tracks score similarly, but penalised toward the
    worst track when the spread is wide.
    """
    if not values:
        return 0.0
    log_sum = sum(math.log(v + shift) for v in values)
    return math.exp(log_sum / len(values)) - shift


def aggregate(results: list[dict]) -> dict:
    """Group per-instance qualities by track, average each track, then
    combine via shifted geometric mean. Infeasible instances contribute
    `INFEASIBLE_QUALITY` to their track's average — they're worse than
    matching the baseline, but bounded so the geomean stays well-defined.
    """
    by_track: dict[str, list[float]] = defaultdict(list)
    feasible_count = 0
    infeasible_count = 0
    for r in results:
        track = r.get("track", "unknown")
        if r.get("feasible"):
            by_track[track].append(float(r["score"]))
            feasible_count += 1
        else:
            by_track[track].append(float(INFEASIBLE_QUALITY))
            infeasible_count += 1

    # Per-track arithmetic mean of per-instance quality.
    track_scores: dict[str, float] = {
        track: sum(scores) / len(scores)
        for track, scores in by_track.items()
        if scores
    }

    overall = _shifted_geomean(list(track_scores.values()))

    return {
        "score": overall,
        "feasible": infeasible_count == 0 and feasible_count > 0,
        "instances_solved": len(results),
        "instances_feasible": feasible_count,
        "instances_infeasible": infeasible_count,
        "track_scores": track_scores,
    }


def main() -> int:
    print("Loading swarm config…", file=sys.stderr)
    cfg = load_swarm_config()

    if _USE_C3 and not _INSIDE_DOCKER:
        return _run_benchmark_c3(cfg)

    if not _INSIDE_DOCKER:
        return _reexec_in_docker(cfg)

    challenge = cfg["challenge"]
    timeout = int(cfg.get("timeout", 30))
    # Direction is no longer used by aggregation — every challenge's
    # quality score is higher-is-better. Kept here for forward-compat
    # with downstream callers that still read it.
    _direction = cfg.get("scoring_direction", "max")  # noqa: F841
    tracks = cfg.get("tracks") or {}
    seed = str(tracks.get("seed", "test"))

    results: list[dict] = []

    if is_gpu_challenge(cfg):
        print(f"Building GPU binary + PTX for {challenge}…", file=sys.stderr)
        binary, ptx = build_gpu(challenge)

        instance_list = []
        for track_key, count in tracks.items():
            if track_key == "seed" or not isinstance(count, int) or count <= 0:
                continue
            for i in range(count):
                instance_list.append((track_key, i))

        if not instance_list:
            print("error: no instances configured.", file=sys.stderr)
            return 2
        print(f"  {len(instance_list)} GPU instance(s) total (sequential)", file=sys.stderr)

        for track_key, idx in instance_list:
            r = run_gpu_instance(challenge, track_key, idx, binary, ptx, seed, timeout)
            results.append(r)
    else:
        print(f"Building tig binaries for {challenge}…", file=sys.stderr)
        solver, evaluator, generator = build(challenge)

        print(f"Materialising instances under datasets/{challenge}/generated/…", file=sys.stderr)
        instances = materialize_instances(challenge, tracks, generator)
        if not instances:
            print(
                "error: no instances to run. Run `python setup.py create` (owner) or "
                "`python setup.py join <url>` (contributor) to fetch swarm config, "
                "or check datasets/<challenge>/test.json.",
                file=sys.stderr,
            )
            return 2
        print(f"  {len(instances)} instance(s) total", file=sys.stderr)

        workers = min(len(instances), min(4, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_instance, challenge, tk, iid, ipath, solver, evaluator, timeout): iid
                for tk, iid, ipath in instances
            }
            for fut in as_completed(futures):
                results.append(fut.result())

    agg = aggregate(results)
    out: dict = {
        "challenge": challenge,
        **agg,
        "errors": [f"{r['instance']}: {r['error']}" for r in results if "error" in r] or None,
    }

    # Per-challenge viz_data aggregation. Driven by `_AGG_EXTRAS` so a
    # tooling pass that rewrites quoted challenge names in `==` chains
    # can't disable a branch unintentionally.
    per_field = _AGG_EXTRAS.get(challenge)
    if per_field is None:
        out["viz_data"] = None
    else:
        viz = {
            r["instance"]: r[per_field]
            for r in results
            if r.get(per_field)
        } or None
        out["viz_data"] = viz

    # VRP-only roll-ups for the routes panel's headline numbers. Omitted
    # entirely for other challenges so the wire payload doesn't carry
    # num_vehicles=0 / total_distance=<score> placeholders that mean
    # nothing for SAT, knapsack, scheduling, or energy.
    if challenge == "vehicle" "_routing":
        out["num_vehicles"] = sum(
            r.get("num_vehicles", 0) for r in results if r.get("feasible")
        )
        out["total_distance"] = sum(
            r["score"] for r in results if r.get("feasible")
        )

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
