#!/usr/bin/env python3
"""Run the active challenge's benchmark and emit JSON for publish.py.

Reads swarm-wide config from the local `.swarm-cache.json` snapshot
(written by `setup.py sync`). `setup.py sync` is the *only* moment a
host-side challenge switch is picked up —
deliberately, so that an in-flight edit→benchmark→publish iteration can
finish on the challenge it started on. After loading local config, an
advisory probe of `/api/swarm_config` warns if the host has rotated since
the last sync; set `TIG_NO_SERVER_PROBE=1` to skip the probe. The config
tells us the active challenge, per-track instance counts, and the
per-instance solver timeout. We then build the right cargo binary,
generate instances on first run (cached under
`datasets/<challenge>/generated/`), and run solver + evaluator on each
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
       in that track. Infeasible instances contribute `-QUALITY_CLAMP`
       (the worst feasibly-bounded value), so an infeasible run can never
       outscore a feasible one.
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
      # Optional, opaque per-challenge roll-up. Only emitted when the active
      # challenge has registered an aggregator in `_AGG_METRICS` — e.g. VRP
      # publishes {"num_vehicles": ..., "total_distance": ...} here. The
      # server stores it verbatim as JSON; the dashboard reads challenge-
      # specific keys out of it.
      "challenge_metrics": { ... per-challenge or absent ... },
    }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent


_INSIDE_DOCKER = Path("/.dockerenv").exists() or os.environ.get("TIG_IN_DOCKER") == "1"

# Mirrors `QUALITY_PRECISION` in src/lib.rs and the upstream tig-monorepo.
# All vendored evaluators clamp their (baseline-relative) quality to
# ±10 × QUALITY_PRECISION before scaling, so the final per-instance score
# is bounded in [-QUALITY_CLAMP, +QUALITY_CLAMP].
QUALITY_PRECISION = 1_000_000
QUALITY_CLAMP = 10 * QUALITY_PRECISION

# Per-instance penalty for an infeasible instance. Pinned to the bottom of the
# feasible clamp (-QUALITY_CLAMP) rather than -∞ so the per-track mean stays in
# a sensible range and the shifted geometric mean is well-defined (shifted to
# exactly 1, the same floor as an all-worst-feasible track).
#
# This MUST be ≤ the worst feasible per-instance quality (-QUALITY_CLAMP). The
# old value (-QUALITY_PRECISION = -1M) was only 1/10th of the way down, so on
# challenges whose feasible scores run well below -1M (the neuralnet baseline is
# ~-2.29M) an infeasible run scored *higher* than a legitimate feasible one.
# That let an infeasible edit win a "best" comparison and trap a trajectory at
# the floor for 80+ edits. Server-side `beats_trajectory_best`/`is_new_best` are
# also feasibility-gated; this is the matching defense-in-depth at the score
# level so infeasible never outranks feasible anywhere a raw score is compared.
INFEASIBLE_QUALITY = -QUALITY_CLAMP

# Constant added to each per-track mean before taking the geometric mean.
# Quality range after clamping is [-10M, +10M]; shift by +10M+1 → strictly
# positive in [1, 20M+1] before geo mean, then unshift the result.
GEOMEAN_SHIFT = QUALITY_CLAMP + 1

def _resolve_server_url() -> str:
    if os.environ.get("TIG_SWARM_SERVER"):
        return os.environ["TIG_SWARM_SERVER"].rstrip("/")
    cfg_path = ROOT_DIR / ".swarm-cache.json"
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
    """Read the locked-in swarm config from local .swarm-cache.json.

    Local is authoritative — `setup.py sync` is the only point at which a
    host-side challenge switch is picked up. This is deliberate: once an
    iteration starts (edit mod.rs → benchmark → publish), it must not be
    silently retargeted to a different challenge between steps. After the
    config is loaded, an advisory probe of `/api/swarm_config` warns if the
    server has moved on, so the agent knows to re-sync at the top of the
    next iteration.

    Set `TIG_NO_SERVER_PROBE=1` to skip the advisory probe entirely (useful
    for fully offline iteration).
    """
    cfg_path = ROOT_DIR / ".swarm-cache.json"
    if not cfg_path.exists():
        print(
            "error: no .swarm-cache.json — run `python setup.py sync` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        local = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        print(f"error: .swarm-cache.json is malformed ({e})", file=sys.stderr)
        sys.exit(1)
    ch = local.get("active_challenge") or local.get("challenge")
    if not ch:
        print(
            "error: .swarm-cache.json has no active_challenge — "
            "run `python setup.py sync`.",
            file=sys.stderr,
        )
        sys.exit(1)
    data = {
        "challenge": ch,
        "tracks": local.get("tracks", {}),
        "timeout": local.get("timeout", 30),
        "scoring_direction": local.get("scoring_direction", "min"),
        "is_gpu": local.get("is_gpu", False),
        "synced_at": local.get("synced_at"),
    }
    # Advisory probe: tell the user if the host has rotated since the last
    # sync. Never overrides — local stays in charge.
    if SERVER and os.environ.get("TIG_NO_SERVER_PROBE") != "1":
        try:
            with urllib.request.urlopen(
                f"{SERVER}/api/swarm_config", timeout=2
            ) as r:
                live = json.load(r)
            live_ch = live.get("active_challenge")
            if live_ch and live_ch != ch:
                print(
                    f"warning: server's active_challenge={live_ch!r} differs "
                    f"from local {ch!r} — run `python setup.py sync` to "
                    f"update. Continuing on {ch!r}.",
                    file=sys.stderr,
                )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass  # probe is best-effort; silence is fine
    return data


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
    score = payload.get("score")
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
        # Wall-clock the solver run only (not the evaluator) so `elapsed`
        # matches the GPU path's semantics — algorithm run time per nonce.
        timed_out = False
        start = time.monotonic()
        try:
            subprocess.run(
                [solver, challenge, str(instance_path), sol_path],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            timed_out = True  # save_solution may have written a partial; evaluator will judge
        elapsed = time.monotonic() - start
        timing = {"elapsed": elapsed, "timed_out": timed_out}
        if not os.path.exists(sol_path) or os.path.getsize(sol_path) == 0:
            return {"instance": instance_id, "track": track_key, "error": "no solution saved", "feasible": False, **timing}
        try:
            eval_result = subprocess.run(
                [evaluator, challenge, str(instance_path), sol_path],
                capture_output=True, text=True, timeout=max(10, timeout),
            )
        except subprocess.TimeoutExpired:
            return {"instance": instance_id, "track": track_key, "error": "evaluator timeout", "feasible": False, **timing}
        score, err = parse_evaluator_score(eval_result)
        if err:
            return {"instance": instance_id, "track": track_key, "error": err, "feasible": False, **timing}
        result = {
            "instance": instance_id,
            "track": track_key,
            "score": score,
            "feasible": True,
            **timing,
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


def _daemon_running() -> bool:
    return subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode == 0


def _ensure_docker_daemon() -> None:
    """Ensure the Docker daemon is reachable, launching the app if not."""
    if _daemon_running():
        return

    print("Docker daemon not running — attempting to start…", file=sys.stderr)
    if sys.platform == "darwin":
        launched = any(
            subprocess.run(["open", "-a", app], capture_output=True).returncode == 0
            for app in ("Docker", "OrbStack")
        )
        if not launched:
            print(
                "error: could not launch Docker Desktop or OrbStack — start it manually.",
                file=sys.stderr,
            )
            sys.exit(1)
    elif sys.platform.startswith("linux"):
        # Best-effort: Docker Desktop on Linux registers a user-level service.
        # System dockerd usually needs sudo, which we don't have here.
        subprocess.run(
            ["systemctl", "--user", "start", "docker-desktop"],
            capture_output=True,
        )
    else:
        print(
            f"error: don't know how to auto-start Docker on {sys.platform} — start it manually.",
            file=sys.stderr,
        )
        sys.exit(1)

    deadline = time.time() + 90
    while time.time() < deadline:
        if _daemon_running():
            print("Docker daemon is ready.", file=sys.stderr)
            return
        time.sleep(2)
    print(
        "error: Docker daemon did not become ready within 90s — start it manually.",
        file=sys.stderr,
    )
    sys.exit(1)


def _ensure_docker_image(image: str, dockerfile: str) -> None:
    """Ensure the local Docker image exists, building it if missing."""
    if subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True
    ).returncode == 0:
        return
    print(
        f"Docker image '{image}' not found — building from {dockerfile}…",
        file=sys.stderr,
    )
    build = subprocess.run(
        ["docker", "build", "-f", dockerfile, "-t", image, str(ROOT_DIR)],
    )
    if build.returncode != 0:
        print(
            f"error: docker build failed (exit {build.returncode}).",
            file=sys.stderr,
        )
        sys.exit(build.returncode)


def _safe_volume_suffix(name: str) -> str:
    """Coerce an arbitrary string into a valid Docker volume-name fragment.

    Docker volume names must match `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. Agent names
    from the wizard are already safe (lowercase letters + hyphens), but a
    hand-edited fleet entry or an unusual repo-dir basename could include
    spaces / quotes / non-ASCII characters. Replace anything outside the
    allowed set with `_` and prepend `x` if the first character isn't
    alphanumeric. Empty input becomes `x`."""
    s = re.sub(r"[^a-zA-Z0-9_.-]", "_", name)
    if not s:
        return "x"
    if not s[0].isalnum():
        s = "x" + s
    return s


def _cargo_target_volume(cfg: dict) -> str:
    """Per-worktree cargo `target/` volume name.

    Each fleet agent runs `benchmark.py` from its own worktree
    (`worktrees/<agent_name>/`), so `ROOT_DIR.name` is unique per agent and
    we can key the volume off it. Single-agent dev (running from the repo
    root) just gets a volume tied to the repo dir name.

    Why per-agent: cargo's `target/` directory is not safe to share between
    concurrent builds of *different* source. Two agents would race on
    `target/release/tig_solver` — cargo's file locks fight each other, and
    an agent can end up benchmarking a binary that was just rewritten with
    the other agent's code. The `~/.cargo/registry` cache is kept shared
    because it's read-mostly and cargo handles concurrent reads on it
    correctly, so we don't waste GB re-downloading crates per agent."""
    suffix = "gpu" if is_gpu_challenge(cfg) else "cpu"
    agent_suffix = _safe_volume_suffix(ROOT_DIR.name)
    return f"tig-cargo-cache-{suffix}-{agent_suffix}"


def _reexec_in_docker(cfg: dict) -> int:
    """Re-launch benchmark.py inside the appropriate Docker container."""
    image, dockerfile = _docker_image_for(cfg)
    _ensure_docker_daemon()
    _ensure_docker_image(image, dockerfile)

    gpu_flags = ["--gpus", "all"] if is_gpu_challenge(cfg) else []
    env_flags = ["-e", "TIG_IN_DOCKER=1"]
    server = os.environ.get("TIG_SWARM_SERVER")
    if server:
        env_flags += ["-e", f"TIG_SWARM_SERVER={server}"]

    suffix = "gpu" if is_gpu_challenge(cfg) else "cpu"
    cmd = [
        "docker", "run", "--rm",
        *gpu_flags,
        "-v", f"{ROOT_DIR}:/app",
        "-v", f"{_cargo_target_volume(cfg)}:/app/target",
        "-v", f"tig-cargo-registry-{suffix}:/root/.cargo/registry",
        *env_flags,
        image,
        "python3", "/app/scripts/benchmark.py",
    ]
    try:
        return subprocess.run(cmd).returncode
    finally:
        _chown_worktree_back(image, cfg)


def _chown_worktree_back(image: str, cfg: dict) -> None:
    """Give root-owned files the container created back to the invoking user.

    The benchmark container runs as root and writes into the bind-mounted
    worktree (datasets/, solution files, .swarm/), leaving root-owned files
    that make `git worktree remove` / `run_fleet.py --clean` fail with
    permission-denied. We can't simply run the build as `--user` without
    breaking cargo's root-owned ~/.cargo, so instead we hand ownership back
    afterwards via a throwaway root container.

    `find -xdev` stops at filesystem boundaries, so the `target/` and cargo
    registry volume mounts (separate devices) are skipped — only the
    bind-mounted worktree files get chowned. POSIX-only; on Windows/macOS
    Docker Desktop maps ownership for the sharing user already.
    """
    if not hasattr(os, "getuid"):
        return  # Windows: no uid concept, Docker Desktop handles ownership
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return  # already root; files are removable as-is
    suffix = "gpu" if is_gpu_challenge(cfg) else "cpu"
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ROOT_DIR}:/app",
        "-v", f"{_cargo_target_volume(cfg)}:/app/target",
        "-v", f"tig-cargo-registry-{suffix}:/root/.cargo/registry",
        image,
        "find", "/app", "-xdev", "-exec", "chown", f"{uid}:{gid}", "{}", "+",
    ]
    try:
        subprocess.run(cmd, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  [BENCH] could not reclaim worktree ownership: {e}",
              file=sys.stderr)


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
    n_vehicles = len(routes)
    if not positions or not routes:
        return {"num_vehicles": n_vehicles, "route_data": None}
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
    return {"num_vehicles": n_vehicles, "route_data": route_data}


def _vrp_aggregate_metrics(results: list[dict]) -> dict:
    """Roll-up summary across all VRP instances. Lives in challenge_metrics
    on the published payload — the dashboard's VRP panel reads it for the
    headline numbers (total vehicles, total tour length)."""
    return {
        "num_vehicles": sum(
            r.get("num_vehicles", 0) for r in results if r.get("feasible")
        ),
        "total_distance": sum(
            r["score"] for r in results if r.get("feasible")
        ),
    }


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

    The matrix sent to the dashboard is K×K where K = min(num_selected,
    MAX_VIZ_ITEMS). When num_selected exceeds the cap, the visible subset
    is the top-K items ranked by marginal contribution (sum of pairwise
    interactions with every other selected item) — so the densest cluster
    is always on screen rather than an arbitrary slice by item ID.
    """
    MAX_VIZ_ITEMS = 300

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

    # True marginal contribution of each selected item: sum of its
    # interactions with every other selected item. O(num_selected²) but
    # ndarray/numpy isn't worth pulling in for this size — at 1250
    # selected items it's ~1.5M ops.
    all_marginals: dict[int, int] = {}
    for i in sorted_items:
        row = interaction_values[i]
        m = 0
        for j in sorted_items:
            if j != i:
                m += row[j]
        all_marginals[i] = m

    # Pick the K most-contributing items (and keep them sorted by ID for
    # stable downstream indexing; the dashboard re-orders by cluster anyway).
    if len(sorted_items) <= MAX_VIZ_ITEMS:
        viz_items = sorted_items
    else:
        ranked = sorted(sorted_items, key=lambda i: -all_marginals[i])
        viz_items = sorted(ranked[:MAX_VIZ_ITEMS])

    k = len(viz_items)
    sub_matrix = [[0] * k for _ in range(k)]
    for ri, i in enumerate(viz_items):
        for rj, j in enumerate(viz_items):
            if i != j:
                sub_matrix[ri][rj] = interaction_values[i][j]

    viz_weights = [weights[i] for i in viz_items]
    viz_marginals = [all_marginals[i] for i in viz_items]

    return {
        "knapsack_data": {
            "num_selected": len(sorted_items),
            "num_items": n,
            "viz_items": viz_items,
            "viz_weights": viz_weights,
            "viz_marginals": viz_marginals,
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

    SAT is a binary pass/fail challenge (`evaluate_solution` in
    `src/satisfiability/mod.rs` returns 1M iff every clause is satisfied,
    else 0), so we only need the pass/fail aggregate plus the variable
    assignment to render the panel — `num_satisfied` vs `num_clauses`
    drives the PASS/UNSAT banner, and `assignment_bits` drives the grid.
    """
    MAX_VIZ_VARS = 10000

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
    num_satisfied = 0
    for clause in clauses:
        for lit in clause:
            v_idx = abs(lit) - 1  # literals are 1-indexed
            if v_idx < 0 or v_idx >= n_vars:
                continue
            val = vars_arr[v_idx]
            if (lit > 0 and val) or (lit < 0 and not val):
                num_satisfied += 1
                break

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
    "vector" "_search":         "vector_search_data",
}

# Optional per-challenge aggregate metrics — emitted to the publish payload
# under `challenge_metrics`. Each entry is a callable `(results) -> dict`
# (the same `results` list passed to `aggregate()`). Challenges with no
# entry emit no `challenge_metrics` field. Same adjacent-string-literal
# convention as `_PER_INSTANCE_EXTRAS` for setup.py-rewrite safety.
_AGG_METRICS = {
    "vehicle" "_routing": _vrp_aggregate_metrics,
}


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
    result = math.exp(log_sum / len(values)) - shift
    # exp(log(x)) doesn't round-trip exactly: when every track matches the
    # baseline (all-zero qualities) this lands at -2**-28 (-3.725e-09) rather
    # than a clean 0.0, which then renders as a misleading "-0" / a noisy
    # "My best: -3.7e-09". Qualities are integer-scaled (QUALITY_PRECISION =
    # 1e6), so nothing finer than ~1e-6 carries meaning; round the sub-ULP
    # noise off, and `+ 0.0` collapses a resulting -0.0 to +0.0.
    return round(result, 6) + 0.0


def _log_instance_result(r: dict, timeout: int) -> None:
    """Emit a one-line per-nonce log to stderr as each instance finishes.

    stderr only — stdout carries the JSON payload that becomes benchmark.json.
    The `(timeout)` marker is the high-value signal: it flags nonces that
    burned the full budget vs. solvers that converged early.
    """
    inst = r.get("instance", "?")
    elapsed = r.get("elapsed")
    et = f"{elapsed:6.1f}s" if isinstance(elapsed, (int, float)) else "     ?  "
    if r.get("feasible"):
        status = "feasible  "
        score = r.get("score")
        tail = f"score={score}" if score is not None else ""
    else:
        status = "INFEASIBLE"
        tail = r.get("error", "")
    capped = r.get("timed_out") or (
        isinstance(elapsed, (int, float)) and elapsed >= timeout
    )
    cap = " (timeout)" if capped else ""
    print(f"  [{inst}]  {status}  {et}  {tail}{cap}", file=sys.stderr)


def _print_timing_summary(results: list[dict]) -> None:
    """Per-track wall-clock roll-up to stderr, printed at end of run."""
    by_track: dict[str, list[float]] = defaultdict(list)
    for r in results:
        e = r.get("elapsed")
        if isinstance(e, (int, float)):
            by_track[r.get("track", "unknown")].append(float(e))
    if not by_track:
        return
    print("── timing ──────────────", file=sys.stderr)
    all_times: list[float] = []
    for track in sorted(by_track):
        ts = by_track[track]
        all_times.extend(ts)
        print(
            f"  {track}:  n={len(ts)}  min {min(ts):.1f}s  "
            f"avg {sum(ts) / len(ts):.1f}s  max {max(ts):.1f}s  total {sum(ts):.1f}s",
            file=sys.stderr,
        )
    if len(by_track) > 1:
        print(
            f"  all:   n={len(all_times)}  "
            f"avg {sum(all_times) / len(all_times):.1f}s  total {sum(all_times):.1f}s",
            file=sys.stderr,
        )


def aggregate(results: list[dict]) -> dict:
    """Group per-instance qualities by track, average each track, then
    combine via shifted geometric mean. Infeasible instances contribute
    `INFEASIBLE_QUALITY` (= -QUALITY_CLAMP) to their track's average — the
    worst feasibly-bounded value, so an all-infeasible run lands at the very
    bottom of the feasible range and never outranks a feasible result, while
    the geomean stays well-defined (shifted to exactly 1).
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


def _read_json_or_none(path: Path) -> dict | None:
    """Best-effort JSON read — never raises, returns None on any problem."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return None


def _resolve_user_id() -> str:
    """Compose a stable, human-reportable user identity for this run.

    "User" here is the contributor account (`username`) plus the per-agent id
    (`agent_id`) so a problematic run can be traced back to both the person and
    the specific agent. Both live in agent.config.json (run_loop reads them
    from there); `username` also lives in fleet.config.json. Config files are
    available in every run context — the worktree is bind-mounted into the
    local docker run and copied into the C3 stage — so no env threading is
    needed, but TIG_USER_ID env still wins if a caller pre-composed one.

    Degrades gracefully: "username (agent <id>)" → "username" → "agent <id>"
    → "unknown".
    """
    pre = os.environ.get("TIG_USER_ID")
    if pre:
        return pre

    agent_cfg = _read_json_or_none(ROOT_DIR / "agent.config.json") or {}
    fleet_cfg = _read_json_or_none(ROOT_DIR / "fleet.config.json") or {}

    username = (
        os.environ.get("TIG_USERNAME")
        or agent_cfg.get("username")
        or fleet_cfg.get("username")
        or ""
    ).strip()
    agent_id = (os.environ.get("TIG_AGENT_ID") or agent_cfg.get("agent_id") or "").strip()

    if username and agent_id:
        return f"{username} (agent {agent_id})"
    if username:
        return username
    if agent_id:
        return f"agent {agent_id}"
    return "unknown"


def main() -> int:
    print("Loading swarm config…", file=sys.stderr)
    cfg = load_swarm_config()
    # Stamp the locked-in challenge once so the operator can spot an
    # accidental edit of the wrong mod.rs vs. what's about to run.
    synced = cfg.get("synced_at") or "unknown"
    print(
        f"Locked challenge: {cfg.get('challenge')} (.swarm-cache.json, "
        f"synced_at={synced}).",
        file=sys.stderr,
    )

    if not _INSIDE_DOCKER:
        return _reexec_in_docker(cfg)

    # Run identity — printed once, here, inside the container before any build
    # or compute begins. Gives the user / TIG / the compute provider a stable
    # reference to correlate a problematic run (failure, timeout, runaway
    # compute) against their logs. The benchmark id is fresh per run.
    benchmark_id = uuid.uuid4().hex[:10]
    print(f"User ID: {_resolve_user_id()}", file=sys.stderr)
    print(f"Benchmark ID: {benchmark_id}", file=sys.stderr)

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
            _log_instance_result(r, timeout)
            results.append(r)
    else:
        print(f"Building tig binaries for {challenge}…", file=sys.stderr)
        solver, evaluator, generator = build(challenge)

        print(f"Materialising instances under datasets/{challenge}/generated/…", file=sys.stderr)
        instances = materialize_instances(challenge, tracks, generator)
        if not instances:
            print(
                "error: no instances to run. Run `python setup.py` to configure this clone, "
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
                r = fut.result()
                _log_instance_result(r, timeout)
                results.append(r)

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

    # Per-challenge aggregate metrics, dispatched from `_AGG_METRICS`.
    # Surfaced on the published payload under `challenge_metrics` (a generic
    # JSON dict) — challenges that don't register an aggregator simply omit
    # the field, so the wire payload stays clean.
    metrics_fn = _AGG_METRICS.get(challenge)
    if metrics_fn is not None:
        out["challenge_metrics"] = metrics_fn(results)

    _print_timing_summary(results)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
