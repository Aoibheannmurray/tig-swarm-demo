"""C3 cloud compute integration for remote benchmarking.

The current C3 architecture runs a project described by a root `.c3` file.
For Docker jobs C3 pulls a public Docker Hub image, uploads the project
workspace, runs the configured bash script inside the container, then returns
files written to `$C3_ARTIFACTS_DIR` or configured `output:` directories.

Distributed benchmarking (fan-out): a benchmark's nonces are bin-packed into
fixed-size shards (``c3_nonces_per_shard``, default 8), packing *across* track
boundaries so no machine is under-filled — a shard may carry slices from several
tracks. The job count is ceil(total_nonces / size), not one-per-track. Every
shard is deployed as its own concurrent C3 job (bounded by
``c3_max_parallel_jobs``, default 3, the basic C3 plan's concurrent-job cap).
The per-shard ``combined.json`` outputs are merged back into a single combined
dict and scored once by ``_tig_adapter`` — so the score is identical to a
single-job run, only faster. A benchmark whose total nonces fit in one shard
reproduces the old single-job behavior exactly.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import re
import shutil
import random
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from challenge_files import ROOT, read_optional
# TIG-docker backend helpers (shared with the local path in benchmark.py).
from benchmark import (
    _tig_adapter as _bm_tig_adapter,
    _tig_backend as _bm_tig_backend,
    _tig_version as _bm_tig_version,
    DEFAULT_MAX_FUEL_BUDGET as _BM_DEFAULT_FUEL,
)

_POLL_INTERVAL_SECS = 15
# Hard ceilings for c3 CLI subcommands so a hung CLI (network stall, auth
# prompt, dead endpoint) can't wedge the polling loop forever. `c3 pull`
# downloads artifacts, so it gets a much larger budget than metadata queries.
_C3_CLI_TIMEOUT_SECS = 60
_C3_LOGS_TIMEOUT_SECS = 120
_C3_PULL_TIMEOUT_SECS = 600
_DEFAULT_CPU_IMAGE = "rust:1-bookworm"
_DEFAULT_GPU_IMAGE = "nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04"
_DEFAULT_CPU_HARDWARE = "cpu-d3-4vcpu-16gb"
_DEFAULT_GPU_HARDWARE = "l40"
_AUTO_HARDWARE_VALUES = {"", "auto", "default"}
# Distributed benchmarking defaults. Each shard = one C3 machine running up to
# _DEFAULT_NONCES_PER_SHARD nonces; at most _DEFAULT_MAX_PARALLEL_JOBS shards run
# concurrently (the basic C3 plan's cap — overridable per-agent via
# c3_max_parallel_jobs in fleet.config.json).
_DEFAULT_NONCES_PER_SHARD = 8
_DEFAULT_MAX_PARALLEL_JOBS = 3
# Concurrent shards upload byte-identical workspace files (same algorithm/mod.rs,
# Cargo.toml, …); a content-addressed blob store rejects simultaneous writes to
# the same object with HTTP 429 ("Reduce your concurrent request rate for the
# same object"). Retry ONLY that throttle, with exponential + FULL-jitter backoff
# so the racing shards de-synchronise instead of colliding again in lockstep. A
# 429 happens during workspace upload, before any job is submitted, so retrying
# can't orphan a running job. Happy path adds zero latency (only fires on the
# 429 signature); non-throttle deploy failures still fail fast.
_DEPLOY_MAX_ATTEMPTS = 5          # 1 initial + up to 4 retries
_DEPLOY_BACKOFF_BASE_SECS = 1.5   # jitter window for the first retry
_DEPLOY_BACKOFF_CAP_SECS = 20.0   # per-retry window ceiling
_DEPLOY_RETRY_SIGNATURES = (
    "status 429", "ServiceUnavailable", "concurrent request rate",
)
# CPU challenges served by a *baked* custom image (tig-bench-<ch>): the monorepo
# source + warm cargo cache + modified_test_algorithm are pre-built into the
# image (see Dockerfile.bench / build_bench_image.sh). C3 pulls it (cached per
# pool), injects ONLY the algorithm into the pre-baked swarm_algo slot, and
# incremental-builds (seconds) — no per-job source upload, no cold compile.
# Every challenge — CPU and GPU — is served by a *baked* custom image
# (tig-bench-<ch>): the monorepo source + warm cargo cache + modified_test_algorithm
# are pre-built into the image (see Dockerfile.bench / build_bench_image.sh). C3
# pulls it (cached per pool), injects ONLY the algorithm into the pre-baked
# swarm_algo slot, and incremental-builds in seconds — no per-job source upload, no
# cold compile. The GPU challenges (vector_search, hypergraph, neuralnet_optimizer)
# bake too: nvcc pre-builds their PTX at image-build time, and neuralnet's
# seed-blinding anti-cheat is baked into tig-challenges by Dockerfile.bench (see
# _NN_BOILERPLATE) rather than patched per job. The raw dev-image path (Option A,
# per-job monorepo upload + cold compile) has been fully retired.
_TIG_BAKED_CHALLENGES = frozenset({
    "satisfiability",
    "vehicle_routing",
    "knapsack",
    "job_scheduling",
    "energy_arbitrage",
    "vector_search",
    "hypergraph",
    "neuralnet_optimizer",
})
_SUPPORTED_TIG_C3_CHALLENGES = _TIG_BAKED_CHALLENGES


# ── Helpers ────────────────────────────────────────────────────────


def _write_container_file(path: Path, text: str) -> None:
    """Write a file that will be uploaded to and executed in the Linux C3
    container, forcing LF line endings regardless of the host OS.

    ``Path.write_text`` opens in text mode, so on Windows it would translate
    ``\\n`` -> ``\\r\\n``. A CRLF runner script then breaks bash inside the
    container (e.g. ``set -euo pipefail\\r`` -> "invalid option name"). Passing
    ``newline="\\n"`` disables that translation, so the same bytes are produced
    on Linux, macOS, and Windows.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def _yaml_quote(value: str) -> str:
    # JSON strings are valid YAML scalars and handle quotes/backslashes safely.
    return json.dumps(value)


def _parse_walltime(c3_time: str) -> int:
    """Parse 'HH:MM:SS' (or 'MM:SS' or 'SS') to seconds. Returns 7200 on failure."""
    try:
        parts = [int(p) for p in c3_time.split(":")]
    except ValueError:
        return 7200
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 1:
        h, m, s = 0, 0, parts[0]
    else:
        return 7200
    return h * 3600 + m * 60 + s


def _arg_value(args: argparse.Namespace, name: str, default=None):
    return getattr(args, name, default)


def _is_gpu_config(config: dict) -> bool:
    return bool(config.get("is_gpu"))


def _resolve_c3_hardware(config: dict, requested: str | None = None) -> str:
    value = str(
        requested if requested is not None else config.get("c3_hardware", "")
    ).strip().lower()
    if value not in _AUTO_HARDWARE_VALUES:
        return value
    return _DEFAULT_GPU_HARDWARE if _is_gpu_config(config) else _DEFAULT_CPU_HARDWARE


def _docker_requires_accelerator(config: dict) -> str:
    return "cuda" if _is_gpu_config(config) else "none"


def _select_docker_image(args: argparse.Namespace, config: dict) -> str:
    explicit = (
        _arg_value(args, "env")
        or _arg_value(args, "env_image")
        or config.get("env")
        or config.get("env_image")
        or config.get("c3_image")
    )
    if explicit:
        return explicit
    if _is_gpu_config(config):
        return (
            _arg_value(args, "env_gpu")
            or _arg_value(args, "c3_gpu_image")
            or config.get("env_gpu")
            or config.get("c3_gpu_image")
            or _DEFAULT_GPU_IMAGE
        )
    return (
        _arg_value(args, "env_cpu")
        or _arg_value(args, "c3_cpu_image")
        or config.get("env_cpu")
        or config.get("c3_cpu_image")
        or _DEFAULT_CPU_IMAGE
    )


def _copy_required(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"required C3 workspace path missing: {src}")
    if src.is_dir():
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "target"),
        )
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _copy_optional(src: Path, dst: Path) -> None:
    if src.exists():
        _copy_required(src, dst)


# ── Nonce sharding (distributed fan-out) ───────────────────────────


def _shard_size(cfg: dict) -> int:
    """Nonces per shard/machine. <= 0 disables sharding (one shard for all nonces)."""
    try:
        return int(cfg.get("c3_nonces_per_shard", _DEFAULT_NONCES_PER_SHARD))
    except (TypeError, ValueError):
        return _DEFAULT_NONCES_PER_SHARD


def _max_parallel(cfg: dict) -> int:
    """Max concurrent C3 jobs. Clamped to >= 1."""
    try:
        value = int(cfg.get("c3_max_parallel_jobs", _DEFAULT_MAX_PARALLEL_JOBS))
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_PARALLEL_JOBS
    return max(1, value)


def _plan_shards(tracks: dict, shard_size: int) -> list[list[dict]]:
    """Bin-pack every track's nonces into shards of at most ``shard_size`` nonces
    each, packing *across* track boundaries so no machine is under-filled.

    Returns a list of shards; each shard is a list of ``{track_key, start,
    count}`` slices (a single job runs one ``modified_test_algorithm`` per slice,
    which the driver already supports). So the job count is
    ``ceil(total_nonces / shard_size)`` — not one-per-track.

    e.g. tracks {9: 10, 16: 8} @ size 8 -> 3 shards:
        [9[0:8]], [9[8:10], 16[0:6]], [16[6:8]]
    ``shard_size <= 0`` disables sharding: one shard holds every slice (the
    original single-job behavior). The ``seed`` key and any non-positive / non-int
    count are skipped (matches the driver / adapter).
    """
    units = [
        (k, v) for k, v in (tracks or {}).items()
        if k != "seed" and isinstance(v, int) and v > 0
    ]
    total = sum(count for _, count in units)
    if total == 0:
        return []
    cap = shard_size if (shard_size and shard_size > 0) else total

    shards: list[list[dict]] = []
    cur: list[dict] = []
    cur_used = 0
    for track_key, count in units:
        pos = 0
        while pos < count:
            if cur_used == cap:  # current shard full -> start a new one
                shards.append(cur)
                cur, cur_used = [], 0
            take = min(cap - cur_used, count - pos)
            cur.append({"track_key": track_key, "start": pos, "count": take})
            pos += take
            cur_used += take
    if cur:
        shards.append(cur)
    return shards


# ── C3 command parsing / polling ───────────────────────────────────


def _parse_c3_id(text: str) -> str | None:
    for pat in [
        r'"(?:id|job_id|jobId)"\s*:\s*"(job_[^"]+)"',
        r"\b(job_[a-zA-Z0-9_-]+)\b",
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _normalise_status(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().replace("-", "_").replace(" ", "_").upper()


def _iter_job_dicts(data) -> Iterable[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _iter_job_dicts(item)
    elif isinstance(data, dict):
        if any(k in data for k in ("id", "job_id", "jobId")):
            yield data
        for key in ("jobs", "items", "data", "results", "queue"):
            yield from _iter_job_dicts(data.get(key))


_TERMINAL_OK = ("COMPLETED", "SYNCED", "SUCCEEDED", "SUCCESS")
_TERMINAL_BAD = ("FAILED", "CANCELLED", "CANCELED", "ERROR", "TIMED_OUT", "TIMEOUT")
_NON_TERMINAL = ("PENDING", "SCHEDULING", "RUNNING", "QUEUED", "SUBMITTED")


def _run_c3(
    cmd: list[str], env: dict, cwd: Path, timeout_secs: int,
) -> subprocess.CompletedProcess | None:
    """Run a c3 CLI command with a hard timeout. Returns None on timeout so
    callers treat a hung CLI like a query that produced no output."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            timeout=timeout_secs,
        )
    except subprocess.TimeoutExpired:
        print(f"    [C3] `{' '.join(cmd)}` timed out after {timeout_secs}s")
        return None


def _read_job_status(job_id: str, env: dict, cwd: Path) -> str | None:
    result = _run_c3(["c3", "squeue", "--json"], env, cwd, _C3_CLI_TIMEOUT_SECS)
    raw = (result.stdout or "").strip() if result else ""
    if raw:
        try:
            data = json.loads(raw)
            for job in _iter_job_dicts(data):
                ids = {str(job.get(k, "")) for k in ("id", "job_id", "jobId")}
                if job_id in ids:
                    return _normalise_status(
                        job.get("status") or job.get("state") or job.get("phase")
                    )
        except json.JSONDecodeError:
            pass

    table = _run_c3(["c3", "squeue", "-n", "50"], env, cwd, _C3_CLI_TIMEOUT_SECS)
    if table is None:
        return None
    for line in (table.stdout or "").splitlines():
        if job_id not in line:
            continue
        upper = line.upper()
        for marker in _TERMINAL_OK + _TERMINAL_BAD + _NON_TERMINAL:
            if marker in upper:
                return marker
    return None


def _poll_c3_job(
    job_id: str,
    env: dict,
    cwd: Path,
    walltime_secs: int,
    timeout_secs: int | None = None,
) -> str:
    """Poll a C3 job until it reaches a terminal state."""
    wait_secs = timeout_secs if timeout_secs is not None else max(1800, walltime_secs + 2700)
    deadline = time.monotonic() + wait_secs
    poll = 0
    while time.monotonic() < deadline:
        status = _read_job_status(job_id, env, cwd)
        if status:
            if any(s in status for s in _TERMINAL_OK):
                return "completed"
            if any(s in status for s in _TERMINAL_BAD):
                return "failed"
            if poll % 4 == 0:
                print(f"    [C3] {job_id}: {status}")
        elif poll % 4 == 0:
            print(f"    [C3] Still waiting on {job_id} (poll {poll + 1})...")
        poll += 1
        time.sleep(_POLL_INTERVAL_SECS)
    return "timeout"


def _c3_poll_timeout(args: argparse.Namespace) -> int | None:
    value = _arg_value(args, "c3_poll_timeout", None)
    if value in (None, ""):
        return None
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _read_logs(job_id: str, env: dict, cwd: Path) -> str:
    result = _run_c3(["c3", "logs", job_id], env, cwd, _C3_LOGS_TIMEOUT_SECS)
    if result is None:
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _cancel_c3_job(job_id: str, env: dict, cwd: Path) -> str:
    result = _run_c3(["c3", "cancel", job_id], env, cwd, _C3_CLI_TIMEOUT_SECS)
    if result is None:
        return f"[C3] c3 cancel {job_id} timed out"
    return (result.stdout or "") + (result.stderr or "")


def _cancel_deployed_job(stage: Path, env: dict) -> None:
    """Best-effort cancel of a job whose deploy succeeded but whose ID we
    could not parse from the deploy output.

    Without this the orphaned job burns compute until its walltime. The
    staged `.c3` config carries a unique per-deploy `job_name` (contains a
    fresh run_id), so look the job up by name in `c3 squeue --json` and
    cancel by ID. Silently gives up on any failure — this is cleanup, not
    correctness."""
    try:
        text = (stage / ".c3").read_text(encoding="utf-8")
        m = re.search(r'^job_name:\s*"?([^"\n]+)"?\s*$', text, re.MULTILINE)
        job_name = m.group(1).strip() if m else ""
    except OSError:
        job_name = ""
    if not job_name:
        return
    result = _run_c3(["c3", "squeue", "--json"], env, stage, _C3_CLI_TIMEOUT_SECS)
    if result is None or not (result.stdout or "").strip():
        return
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return
    for job in _iter_job_dicts(data):
        names = {str(job.get(k, "")) for k in ("name", "job_name", "jobName")}
        if job_name not in names:
            continue
        job_id = next(
            (str(job[k]) for k in ("id", "job_id", "jobId") if job.get(k)), "",
        )
        if job_id:
            _cancel_c3_job(job_id, env, stage)
            print(f"    [C3] canceled orphaned job {job_id} ({job_name})")
        return


def _check_c3_auth(env: dict) -> str:
    result = _run_c3(["c3", "whoami"], env, ROOT, _C3_CLI_TIMEOUT_SECS)
    if result is None:
        return (
            "[C3] `c3 whoami` timed out — the CLI may be waiting on an "
            "interactive login or an unreachable endpoint. Run `c3 login` "
            "manually, or export C3_API_KEY."
        )
    if result.returncode == 0:
        return ""
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    return (
        "[C3] Authentication failed. Run `c3 login`, or create an API key with "
        "`c3 apikey create tig-swarm` and export it as `C3_API_KEY` before "
        "running TIG. "
        f"`c3 whoami` said: {detail[-1000:]}"
    )


def _pull_artifacts(job_id: str, env: dict, cwd: Path) -> str:
    result = _run_c3(["c3", "pull", job_id], env, cwd, _C3_PULL_TIMEOUT_SECS)
    if result is None:
        return f"[C3] c3 pull {job_id} timed out"
    if result.returncode == 0:
        return (result.stdout or "") + (result.stderr or "")

    fallback = _run_c3(
        ["c3", "squeue", "pull", job_id], env, cwd, _C3_PULL_TIMEOUT_SECS,
    )
    if fallback is None:
        return (result.stdout or "") + (result.stderr or "")
    return (
        (result.stdout or "")
        + (result.stderr or "")
        + (fallback.stdout or "")
        + (fallback.stderr or "")
    )


def _read_benchmark_stderr(stage: Path) -> str:
    """Return the most-recent non-empty benchmark.stderr artifact, if any.

    This is where benchmark.py's real failure (e.g. a cargo/rustc compile
    error) lands — the C3 job's own stdout only carries runner noise such as
    the rustup install banner.
    """
    candidates = sorted(
        stage.rglob("benchmark.stderr"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.stat().st_size:
            return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _load_benchmark_json(stage: Path) -> tuple[dict | None, str]:
    candidates = sorted(
        stage.rglob("benchmark.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    decode_errors = []
    for path in candidates:
        if path.stat().st_size == 0:
            continue
        try:
            bench = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            decode_errors.append(f"{path}: {exc}")
            continue
        if "score" in bench or "challenge" in bench:
            print(f"    [C3] Results extracted from {path}")
            return bench, ""

    benchmark_stderr = _read_benchmark_stderr(stage)

    detail = "\n".join(decode_errors[-3:])
    if benchmark_stderr:
        detail = f"{detail}\nbenchmark.stderr:\n{benchmark_stderr}".strip()
    return None, detail


# ── Run benchmark on C3 ───────────────────────────────────────────


# ── TIG-docker backend on C3 ──────────────────────────────────────
# Mirrors the local run_tig_benchmark (benchmark.py): the C3 job's image IS the
# TIG image (no docker-in-docker), the runner runs tig_bench_driver.py directly,
# and the host adapts combined.json -> benchmark.json. Reuses the deploy/poll/
# pull helpers below.

_NN_BOILERPLATE = """
// Injected by the swarm: TIG boilerplate solve_challenge (calls training_loop
// with the agent's hooks). training_loop is patched to blind the seed.
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> anyhow::Result<()>,
    _hyperparameters: &Option<serde_json::Map<String, serde_json::Value>>,
    module: Arc<CudaModule>,
    stream: Arc<CudaStream>,
    prop: &cudaDeviceProp,
) -> anyhow::Result<()> {
    training_loop(
        challenge, save_solution, module, stream, prop,
        optimizer_init_state, optimizer_query_at_params, optimizer_step,
    )?;
    Ok(())
}
"""


def _tig_c3_image(cfg: dict) -> str:
    if cfg.get("tig_c3_image"):
        return cfg["tig_c3_image"]
    challenge = cfg["challenge"]
    if challenge not in _SUPPORTED_TIG_C3_CHALLENGES:
        supported = ", ".join(sorted(_SUPPORTED_TIG_C3_CHALLENGES))
        raise ValueError(
            f"TIG C3 Docker image is not configured for {challenge!r}; "
            f"currently supported: {supported}"
        )
    # Default namespace points at the TIG dev's public Docker Hub images
    # (the prebuilt tig-bench-* baked images); override with the `tig_dockerhub`
    # config key or the TIG_DOCKERHUB env var.
    ns = cfg.get("tig_dockerhub") or os.environ.get("TIG_DOCKERHUB", "danieltiagoadams")
    return f"docker.io/{ns}/tig-bench-{challenge}:{_bm_tig_version()}"


def _create_tig_workspace(stage: Path, cfg: dict) -> None:
    challenge = cfg["challenge"]
    # Every challenge is baked (Option B): the monorepo source + warm cargo cache
    # live in the image at /app. Upload only the small driver + the agent's
    # algorithm (and, for neuralnet, the harness boilerplate solve_challenge). No
    # per-job source tar.
    _copy_required(ROOT / "scripts" / "tig_bench_driver.py", stage / "tig_bench_driver.py")
    _copy_required(ROOT / "scripts" / "modified_test_algorithm", stage / "modified_test_algorithm")
    _stage_tig_algorithm(stage, cfg)
    if challenge == "neuralnet_optimizer":
        _write_container_file(stage / "boilerplate.rs", _NN_BOILERPLATE)


def _stage_tig_algorithm(stage: Path, cfg: dict) -> None:
    challenge = cfg["challenge"]
    algo_rel = cfg.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs")
    algo_path = ROOT / algo_rel
    dst = stage / "algorithm"
    dst.mkdir(parents=True, exist_ok=True)

    if algo_path.is_file():
        _copy_required(algo_path, dst / "mod.rs")
    else:
        _copy_required(algo_path.parent, dst)

    kernel_rel = cfg.get("kernel_path")
    if kernel_rel:
        kernel_path = ROOT / kernel_rel
        if kernel_path.is_file():
            _copy_required(kernel_path, dst / "kernels.cu")


def _tig_runner_prologue(challenge: str) -> str:
    """Shared head of both TIG runner scripts (baked and raw)."""
    return f"""#!/bin/bash
set -euo pipefail
cd "${{C3_JOB_WORKDIR:-/workspace}}"
OUT="${{C3_ARTIFACTS_DIR:-./c3-artifacts}}"; mkdir -p "$OUT" c3-artifacts
export CHALLENGE={challenge}
cp modified_test_algorithm /usr/local/bin/tig-scripts/modified_test_algorithm
chmod +x /usr/local/bin/tig-scripts/modified_test_algorithm
"""


def _tig_runner_epilogue(
    cfg: dict, seed: str, hyperparameters: str | None, workdir_expr: str,
) -> str:
    """Shared tail of both TIG runner scripts: env exports + driver run +
    artifact copy. `workdir_expr` is the shell expression for TIG_WORKDIR
    (`/app` baked, `"$PWD/tig-source"` raw)."""
    tracks = {
        k: v for k, v in (cfg.get("tracks") or {}).items()
        if k != "seed" and isinstance(v, int) and v > 0
    }
    # Per-track nonce start offsets (distributed shard window). Absent -> {} so
    # the driver defaults every track to start 0 (single-job behavior).
    starts = {
        k: int(v) for k, v in (cfg.get("track_starts") or {}).items()
        if k != "seed" and k in tracks
    }
    fuel = int(cfg.get("max_fuel_budget", _BM_DEFAULT_FUEL))
    hp_export = ""
    if hyperparameters is not None:
        hp_export = f"export TIG_HYPERPARAMETERS={_yaml_quote(hyperparameters)}\n"
    return f"""export TIG_WORKDIR={workdir_expr}
export TIG_TRACKS={_yaml_quote(json.dumps(tracks))}
export TIG_STARTS={_yaml_quote(json.dumps(starts))}
export TIG_SEED={_yaml_quote(seed)}
export TIG_FUEL={fuel}
{hp_export}python3 tig_bench_driver.py > "$OUT/combined.json" 2> "$OUT/driver.stderr" || echo "driver rc=$?" >> "$OUT/driver.stderr"
cp "$OUT/combined.json" c3-artifacts/ 2>/dev/null || true
cp "$OUT/driver.stderr" c3-artifacts/ 2>/dev/null || true
"""


def _tig_runner_script(cfg: dict, seed: str, hyperparameters: str | None) -> str:
    challenge = cfg["challenge"]
    is_nn = challenge == "neuralnet_optimizer"

    # Baked (Option B, all challenges): the monorepo source + warm cargo cache live
    # in the image at /app. Inject the algorithm into the pre-baked swarm_algo slot
    # and incremental-build (seconds). No source re-add, no `pub mod swarm_algo`
    # append (the Dockerfile already added it), no runtime seed-blinding
    # (neuralnet's blind is baked into tig-challenges at image-build time — see
    # Dockerfile.bench).
    #
    # neuralnet only: the agent writes just the three optimizer hooks, so the slot
    # needs the harness boilerplate solve_challenge concatenated on. That's a cheap
    # local join of two already-staged files (algorithm/mod.rs + boilerplate.rs) —
    # no source upload, exactly as before.
    assemble = ('cat algorithm/mod.rs boilerplate.rs > "$SLOT/mod.rs"'
                if is_nn else 'cp algorithm/mod.rs "$SLOT/mod.rs"')
    return (
        _tig_runner_prologue(challenge)
        + f"""SLOT=/app/tig-algorithms/src/{challenge}/swarm_algo
mkdir -p "$SLOT"
{assemble}
cp algorithm/*.cu "$SLOT/" 2>/dev/null || true
"""
        + _tig_runner_epilogue(cfg, seed, hyperparameters, "/app")
    )


def _c3_project_yaml(
    config: dict, script_name: str, c3_time: str, image: str, run_id: str,
) -> str:
    """The `.c3` project file describing one C3 benchmark job."""
    challenge = config.get("challenge", "unknown")
    hardware = _resolve_c3_hardware(config)
    requires_accelerator = _docker_requires_accelerator(config)
    return f"""\
project: tig-swarm-benchmark
script: {_yaml_quote(script_name)}
hardware: {_yaml_quote(hardware)}
time: {_yaml_quote(c3_time)}
job_name: {_yaml_quote(f"tig-{challenge}-{run_id}")}

docker:
  image: {_yaml_quote(image)}
  requires_accelerator: {_yaml_quote(requires_accelerator)}

output:
  - ./c3-artifacts
"""


def _write_tig_c3_project(stage: Path, cfg: dict, c3_time: str, image: str,
                          seed: str, hyperparameters: str | None) -> str:
    run_id = uuid.uuid4().hex[:10]
    script_name = f"run-tig-{run_id}.sh"
    _write_container_file(
        stage / ".c3", _c3_project_yaml(cfg, script_name, c3_time, image, run_id)
    )
    script_path = stage / script_name
    _write_container_file(script_path, _tig_runner_script(cfg, seed, hyperparameters))
    script_path.chmod(0o755)
    return script_name


def _load_tig_combined(stage: Path) -> tuple[dict | None, str]:
    # Newest-mtime-first, like the sibling benchmark.json/benchmark.stderr
    # loaders — rglob order is filesystem-dependent, and a stale artifact
    # from a previous pull must not shadow the fresh one.
    candidates = sorted(
        stage.rglob("combined.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None, "combined.json not found in pulled artifacts"
    try:
        return json.loads(candidates[0].read_text()), ""
    except Exception as e:
        return None, f"combined.json not parseable: {e}"


def _stale_cli_error(output: str) -> str | None:
    """Detect a `.c3` schema rejection from an OUTDATED c3 CLI (e.g. `field
    hardware not found in type data.C3Config`, `field requires_accelerator not
    found in type data.DockerConfig`).

    This is an environment problem, not a code problem — without this
    translation the raw YAML unmarshal error looks like a build failure, the
    loop wastes LLM calls "fixing" Rust that isn't broken, and the contributor
    is left decoding Go struct names. The returned message starts with a
    marker run_loop's infra_markers matches, so the LLM-fix retry is skipped.
    The c3 binary is NOT a Python dependency: no requirements.txt updates it —
    only re-running C3's own installer does."""
    if "not found in type data." not in output:
        return None
    return (
        "c3 CLI is out of date — it doesn't understand this project's .c3 "
        "config fields.\n"
        "  Update it:    curl -fsSL https://cthree.cloud/install.sh | sh\n"
        "  Then verify:  which -a c3    (an old copy earlier on PATH wins —\n"
        "                remove it or re-open your terminal), then relaunch.\n"
        f"  The CLI said: {output[-400:]}"
    )


# ── Stale-CLI self-heal ──
# When _stale_cli_error fires, run C3's official installer once and retry the
# deploy in place, instead of failing the benchmark and telling the user to do
# it. Shards deploy from parallel threads, so the outcome is computed once
# under a lock and shared — later callers reuse it rather than racing the
# installer.
_c3_update_lock = threading.Lock()
_c3_update_result: bool | None = None  # None = not attempted this process


def _run_c3_installer() -> bool:
    """Run C3's official installer (`curl … install.sh | sh`) to update the
    CLI in place. macOS/Linux only — there's no Windows c3 — and needs curl +
    sh on PATH. Returns True when the installer exits 0."""
    if os.name == "nt" or shutil.which("curl") is None or shutil.which("sh") is None:
        return False
    print("    [C3] c3 CLI is out of date — running the official installer to update it…")
    try:
        result = subprocess.run(
            ["sh", "-c", "curl -fsSL https://cthree.cloud/install.sh | sh"],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"    [C3] installer failed to run ({e})")
        return False
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-300:]
        print(f"    [C3] installer exited {result.returncode}: {tail}")
        return False
    print("    [C3] c3 CLI updated — retrying the deploy.")
    return True


def _update_c3_cli_once() -> bool:
    global _c3_update_result
    with _c3_update_lock:
        if _c3_update_result is None:
            _c3_update_result = _run_c3_installer()
        return _c3_update_result


def _run_one_c3_shard(
def _run_one_c3_shard_inner(
    args: argparse.Namespace, env: dict, stage: Path, label: str,
    _cli_updated: bool = False,
) -> tuple[dict | None, str]:
    """Deploy one already-staged shard job, poll it, pull artifacts, and return
    its raw ``combined.json`` dict (unadapted — the orchestrator merges + scores
    all shards together). ``stage`` must already contain the `.c3` + runner.

    `_cli_updated` is the self-heal recursion guard: after one
    update-and-retry, a still-stale CLI falls through to the actionable error
    (the likely cause then is an old copy shadowing the new one on PATH)."""
    cmd = ["c3", "deploy"]
    provider = _arg_value(args, "c3_provider")
    if provider:
        cmd.extend(["-p", provider])

    combined = ""
    for attempt in range(1, _DEPLOY_MAX_ATTEMPTS + 1):
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", cwd=stage, env=env,
        )
        lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            print(f"    [C3][{label}]   {line}")
        proc.wait()
        combined = "\n".join(lines)
        if proc.returncode == 0:
            break
        # Retry only the object-store throttle (see _DEPLOY_RETRY_SIGNATURES);
        # every other deploy failure fails fast.
        throttled = any(sig in combined for sig in _DEPLOY_RETRY_SIGNATURES)
        if not throttled or attempt == _DEPLOY_MAX_ATTEMPTS:
            return None, f"c3 deploy failed ({proc.returncode}):\n{combined[-2000:]}"
        window = min(_DEPLOY_BACKOFF_CAP_SECS, _DEPLOY_BACKOFF_BASE_SECS * 2 ** (attempt - 1))
        delay = random.uniform(0, window)
        print(
            f"    [C3][{label}]   deploy throttled (429 same-object); "
            f"retry {attempt}/{_DEPLOY_MAX_ATTEMPTS - 1} in {delay:.1f}s"
        )
        time.sleep(delay)

    job_id = _parse_c3_id(combined)
    if not job_id:
        # Deploy succeeded, so a job may be running that we can no longer
        # track — best-effort cancel it by its unique job_name.
        _cancel_deployed_job(stage, env)
        return None, f"could not parse job ID:\n{combined[-1000:]}"

    walltime_secs = _parse_walltime(args.c3_time)
    print(f"    [C3][{label}] job {job_id} — polling for completion...")
    status = _poll_c3_job(
        job_id, env, stage, walltime_secs, _c3_poll_timeout(args)
    )
    cancel_output = ""
    if status == "timeout" and _arg_value(args, "c3_cancel_on_timeout", False):
        cancel_output = _cancel_c3_job(job_id, env, stage)
        print(f"    [C3][{label}] canceled timed-out job {job_id}")
    _pull_artifacts(job_id, env, stage)
    if status != "completed":
        logs_out = _read_logs(job_id, env, stage)
        details = "\n".join(
            part for part in (cancel_output[-1000:], logs_out[-3000:]) if part
        )
        return None, f"job {job_id} {status}\n{details}"

    comb, perr = _load_tig_combined(stage)
    if comb is None:
        logs_out = _read_logs(job_id, env, stage)
        return None, f"job {job_id} completed but {perr}\n{logs_out[-3000:]}"
    return comb, ""


def _run_one_c3_shard(
    args: argparse.Namespace, env: dict, stage: Path, label: str,
    job_slots=None,
) -> tuple[dict | None, str]:
    """Global-cap wrapper around one shard's deploy+poll. When parallel HPO
    passes a shared `job_slots` semaphore, hold a slot for this shard's entire
    lifetime, so concurrent config evaluations never exceed c3_max_parallel_jobs
    total live C3 jobs (respecting the account concurrency limit)."""
    if job_slots is None:
        return _run_one_c3_shard_inner(args, env, stage, label)
    with job_slots:
        return _run_one_c3_shard_inner(args, env, stage, label)


def _merge_combined(results: list, challenge: str, fuel: int) -> dict:
    """Reassemble per-shard ``combined.json`` dicts into one, concatenating each
    track's nonce records across shards. Shaped exactly like a single-job
    combined so ``_tig_adapter`` scores it unchanged. Iterating ``results`` in
    shard order keeps nonces in ascending order (shards are disjoint windows)."""
    merged: dict = {"challenge": challenge, "fuel_limit": fuel, "tracks": {}}
    tracks = merged["tracks"]
    for comb in results:
        if not comb:
            continue
        for track_key, payload in (comb.get("tracks") or {}).items():
            slot = tracks.setdefault(track_key, {"nonces": []})
            if not isinstance(payload, dict):
                continue
            if "error" in payload and "error" not in slot:
                slot["error"] = payload["error"]
            slot["nonces"].extend(payload.get("nonces") or [])
    return merged


def _run_tig_benchmark_c3(
    args: argparse.Namespace, cfg: dict, server: str, env: dict,
    seed: str | None, hyperparameters: str | None, job_slots=None,
) -> tuple[dict | None, str]:
    challenge = cfg.get("challenge", "unknown")
    image = _tig_c3_image(cfg)
    cfg = dict(cfg)
    cfg["env"] = image
    if seed is None:
        seed = str((cfg.get("tracks") or {}).get("seed", "test"))
    fuel = int(cfg.get("max_fuel_budget", _BM_DEFAULT_FUEL))

    shards = _plan_shards(cfg.get("tracks") or {}, _shard_size(cfg))
    if not shards:
        return None, f"[C3] No positive-count tracks to benchmark for {challenge}"
    max_parallel = _max_parallel(cfg)
    print(
        f"    [C3] (TIG backend) {challenge}: {len(shards)} shard job(s) "
        f"across ≤{max_parallel} machine(s), image {image}..."
    )

    with tempfile.TemporaryDirectory(prefix="tig-c3-tig-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        base = root / "base"
        base.mkdir()
        # Stage the shared workspace ONCE (for raw challenges this is the pricey
        # monorepo tar); each shard then gets a cheap copytree + its own project.
        try:
            _create_tig_workspace(base, cfg)
        except Exception as exc:
            return None, f"[C3] Failed to create TIG workspace: {exc}"

        jobs: list[tuple[int, Path, str]] = []
        for idx, shard in enumerate(shards):
            stage = root / f"shard-{idx}"
            try:
                shutil.copytree(base, stage)
                shard_cfg = dict(cfg)
                # A shard may hold slices from several tracks (bin-packed); each
                # distinct track appears at most once per shard, so these dicts
                # never collide.
                shard_cfg["tracks"] = {s["track_key"]: s["count"] for s in shard}
                shard_cfg["track_starts"] = {s["track_key"]: s["start"] for s in shard}
                _write_tig_c3_project(
                    stage, shard_cfg, args.c3_time, image, seed, hyperparameters
                )
            except Exception as exc:
                return None, f"[C3] Failed to stage shard {idx}: {exc}"
            label = "+".join(
                f"{s['track_key']}[{s['start']}:{s['start'] + s['count']}]"
                for s in shard
            )
            jobs.append((idx, stage, label))

        results: list[dict | None] = [None] * len(jobs)
        errs: list[str] = [""] * len(jobs)
        # With a shared job_slots semaphore (parallel HPO), the semaphore is the
        # global cap; let this config submit all its shards and block on slots.
        pool_workers = len(jobs) if job_slots is not None else max_parallel
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            futures = {
                pool.submit(_run_one_c3_shard, args, env, stage, label, job_slots): idx
                for idx, stage, label in jobs
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    comb, err = future.result()
                except Exception as exc:  # defensive: never let a thread crash silently
                    comb, err = None, f"shard raised {exc!r}"
                results[idx], errs[idx] = comb, err

        # Strict all-or-nothing (mirrors the single-job path): any failed shard
        # means missing nonces, so fail rather than score on a partial set.
        failed = [(jobs[i][2], errs[i]) for i in range(len(jobs)) if results[i] is None]
        if failed:
            detail = "\n".join(f"  {label}: {err}" for label, err in failed)
            return None, (
                f"[C3] {len(failed)}/{len(jobs)} TIG shard job(s) failed:\n{detail}"
            )

        merged = _merge_combined(results, challenge, fuel)
        return _bm_tig_adapter(merged, cfg), ""


def run_benchmark_c3(
    args: argparse.Namespace, config: dict, server: str,
    seed: str | None = None, hyperparameters: str | None = None, job_slots=None,
) -> tuple[dict | None, str]:
    if shutil.which("c3") is None:
        return None, "[C3] c3 CLI not found. Install from https://cthree.cloud/install.sh"

    c3_key = args.c3_api_key or os.environ.get("C3_API_KEY", "")
    challenge = config.get("challenge", "unknown")

    cfg = dict(config)
    cfg["server_url"] = server
    cfg["c3_hardware"] = _resolve_c3_hardware(cfg, _arg_value(args, "hardware"))
    # Distributed sharding knobs come from args (like c3_time/c3_provider);
    # fall back to any config value, then the module defaults.
    cfg["c3_nonces_per_shard"] = _arg_value(
        args, "c3_nonces_per_shard",
        cfg.get("c3_nonces_per_shard", _DEFAULT_NONCES_PER_SHARD),
    )
    cfg["c3_max_parallel_jobs"] = _arg_value(
        args, "c3_max_parallel_jobs",
        cfg.get("c3_max_parallel_jobs", _DEFAULT_MAX_PARALLEL_JOBS),
    )

    image = _select_docker_image(args, cfg)
    cfg["env"] = image

    env = os.environ.copy()
    if c3_key and not c3_key.startswith("your_"):
        env["C3_API_KEY"] = c3_key
    elif c3_key:
        env.pop("C3_API_KEY", None)

    auth_err = _check_c3_auth(env)
    if auth_err:
        return None, auth_err

    # Fuel-instrumented TIG-docker backend (TIG image + driver → combined.json →
    # benchmark.json) is the ONLY C3 benchmarking path. The custom wall-clock
    # staging path (_create_workspace / _write_c3_project) has been retired.
    return _run_tig_benchmark_c3(args, cfg, server, env, seed, hyperparameters, job_slots)


