"""C3 cloud compute integration for remote benchmarking.

The current C3 architecture runs a project described by a root `.c3` file.
For Docker jobs C3 pulls a public Docker Hub image, uploads the project
workspace, runs the configured bash script inside the container, then returns
files written to `$C3_ARTIFACTS_DIR` or configured `output:` directories.

A benchmark is ONE C3 job: the minimal swarm workspace (Cargo.toml + src/ +
scripts/ + .swarm-cache.json) is staged into a temp dir and uploaded, and the
job runs `python3 scripts/benchmark.py` inside a plain toolchain image
(default `rust:1-bookworm` CPU / `nvidia/cuda:…-devel` GPU — the runner script
apt-installs anything missing), then pulls back `benchmark.json`.

Fleet-wide slot pool: every agent sharing one C3 key draws from a single FCFS
pool of ``c3_max_parallel_jobs`` slots (see ``c3_pool.py``), so N agents never
exceed the C3 plan's concurrent-job cap; extra benchmarks queue first-come-
first-served.
"""

from __future__ import annotations

import argparse
import json
import math
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

import c3_pool
from challenge_files import ROOT, read_optional
# Shard merging re-aggregates per-instance results with the SAME math a
# single job uses, so a sharded score is identical to an unsharded run's.
from benchmark import aggregate as _bm_aggregate

_POLL_INTERVAL_SECS = 15
# Hard ceilings for c3 CLI subcommands so a hung CLI (network stall, auth
# prompt, dead endpoint) can't wedge the polling loop forever. `c3 pull`
# downloads artifacts, so it gets a much larger budget than metadata queries.
_C3_CLI_TIMEOUT_SECS = 60
_C3_LOGS_TIMEOUT_SECS = 120
_C3_PULL_TIMEOUT_SECS = 600
# How long a deployed job may stay unknown to `c3 squeue` before we call it
# unrecognised rather than slow. Generous enough to cover a job that hasn't
# propagated yet or a few minutes of API flakiness, short enough that a bad
# job ID doesn't hold a fleet pool slot for hours.
_UNRECOGNISED_JOB_GRACE_SECS = 600
# Warm images are the default C3 path (see _warm_c3_image). They remove the
# cold cargo build from every shard, which is most of a shard's fixed cost.
_WARM_IMAGES_DEFAULT = True
# Wall clock a shard burns before it solves anything: provision + image pull +
# build. Measured on this fleet — three IDENTICAL 1-instance full-source shards
# deployed 8s apart finished 2m37s / 6m01s / 9m20s after submit — so it is both
# large and highly variable. Warm images drop the build and land near a minute.
# Override per swarm with `c3_shard_fixed_secs`.
_SHARD_FIXED_SECS_FULL_SOURCE = 300.0
_SHARD_FIXED_SECS_WARM = 60.0
# vCPUs to assume when the hardware profile name doesn't carry a count (e.g.
# `auto`, resolved job-side). The small C3 CPU profiles are 4-vCPU.
_ASSUMED_SHARD_VCPUS = 4
def pinned_rust_version(default: str = "1.89.0") -> str:
    """The compiler version from rust-toolchain.toml — the single source of
    truth every build path derives from.

    Falls back to `default` for a clone predating the pin, so an older
    checkout still benchmarks instead of failing on a missing file."""
    try:
        text = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    except OSError:
        return default
    m = re.search(r'^\s*channel\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else default


# Exact, not floating `rust:1-bookworm`: a floating tag silently changes the
# compiler under a running swarm. Cargo fingerprints the rustc version, so a
# tag that drifts away from the pin doesn't just lose reproducibility — it
# invalidates the warm image's baked target and turns every job back into a
# cold compile. Derived, never hand-written twice.
_DEFAULT_CPU_IMAGE = f"rust:{pinned_rust_version()}-bookworm"
_DEFAULT_GPU_IMAGE = "nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04"
_DEFAULT_CPU_HARDWARE = "cpu-d3-4vcpu-16gb"
_DEFAULT_GPU_HARDWARE = "l40"
_AUTO_HARDWARE_VALUES = {"", "auto", "default"}
# Fleet-wide FCFS slot-pool size (the basic C3 plan's concurrent-job cap —
# overridable fleet-wide/per-agent in fleet.config.json).
_DEFAULT_MAX_PARALLEL_JOBS = 3
# Concurrent agents upload byte-identical workspace files (same Cargo.toml,
# src/, …); a content-addressed blob store rejects simultaneous writes to
# the same object with HTTP 429 ("Reduce your concurrent request rate for the
# same object"). Retry ONLY that throttle, with exponential + FULL-jitter backoff
# so the racing deploys de-synchronise instead of colliding again in lockstep. A
# 429 happens during workspace upload, before any job is submitted, so retrying
# can't orphan a running job. Happy path adds zero latency (only fires on the
# 429 signature); non-throttle deploy failures still fail fast.
_DEPLOY_MAX_ATTEMPTS = 5          # 1 initial + up to 4 retries
_DEPLOY_BACKOFF_BASE_SECS = 1.5   # jitter window for the first retry
_DEPLOY_BACKOFF_CAP_SECS = 20.0   # per-retry window ceiling
_DEPLOY_RETRY_SIGNATURES = (
    "status 429", "ServiceUnavailable", "concurrent request rate",
)
# The OTHER 429: the plan's concurrent-chip cap (free 3 / pro 10 / …). The
# fleet pool (c3_pool.py) keeps THIS fleet's live jobs under the cap, but C3's
# real-time count can still reject a submit — a just-released sibling chip that
# C3 still counts as "running" for a few seconds of teardown, a manual/lone
# `c3` job holding a chip outside the pool, or a pool sized above the account's
# true limit. The error says "No new job was queued", so nothing is orphaned:
# unlike a compile error a chip WILL free, so we wait for one and retry instead
# of failing the benchmark. Patient (chips free on a minute scale) but bounded,
# so a persistent external hold eventually surfaces a clear, actionable error
# rather than parking forever. The pool slot is held throughout, so this shard
# stays first in line for the freed chip. Checked BEFORE the object-store
# throttle: this needs the long wait, not the sub-20s upload backoff.
_CONCURRENCY_RETRY_SIGNATURES = (
    "CONCURRENCY_LIMIT", "Concurrency limit reached", "maximum concurrent chip",
)
_CONCURRENCY_MAX_WAIT_SECS = 900.0   # total budget to wait for a chip to free
_CONCURRENCY_RETRY_MIN_SECS = 15.0   # jittered gap between submit retries
_CONCURRENCY_RETRY_MAX_SECS = 30.0
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


# `auto` CPU hardware resolves to the profile C3 can actually deliver RIGHT
# NOW that this benchmark is cheapest on — a 5-instance run has no business on
# a 96-vCPU box (bench_workers scales solver parallelism to the machine, but
# workers beyond the instance count just idle cores at the big box's rate).
# The LISTING is cached briefly so a sharded benchmark's jobs make one
# control-plane query, not one per shard; the pick itself is recomputed per
# benchmark because it depends on the workload.
_AVAILABILITY_RANK = {"high": 3, "medium": 2, "low": 1}
_HW_CACHE_TTL_SECS = 600
_hw_cache: tuple[float, list[dict]] | None = None
# Profiles auto-selection must never pick, even when C3 lists them available.
# Deliberately EMPTY: a hardcoded list goes stale in both directions — it keeps
# avoiding pools C3 has since fixed, and says nothing about the one that breaks
# tomorrow. (It used to pin cpu-d3-96vcpu-384gb, whose jobs died with an
# internal C3 error under support code C3-JOB-2457736F44XT on 2026-07-15.)
# Bad pools are now learned per session instead — see
# _blocklist_profile_for_session. Add a standing entry per fleet via
# `c3_hardware_blocklist` config / TIG_C3_HW_BLOCKLIST env (comma-separated;
# merged with this default and with what the session has learned).
_DEFAULT_HW_BLOCKLIST: frozenset[str] = frozenset()


# Profiles this process has watched fail to provision. `auto` re-queries C3
# every 10 minutes and would otherwise keep re-picking the same dead pool for
# the rest of the session — the agent hangs, the human switches hardware by
# hand. Session-scoped on purpose: a pool that's wedged now may be fine in an
# hour, so this never persists to disk.
_session_hw_blocklist: set[str] = set()


def _blocklist_profile_for_session(profile: str, why: str) -> None:
    global _hw_cache
    if not profile or profile in _session_hw_blocklist:
        return
    _session_hw_blocklist.add(profile)
    _hw_cache = None  # force a re-pick on the next benchmark
    print(f"    [C3] not auto-selecting {profile} again this session ({why})")


def _hw_blocklist(cfg: dict | None) -> frozenset[str]:
    extra: set[str] = set(_session_hw_blocklist)
    raw = (cfg or {}).get("c3_hardware_blocklist")
    if isinstance(raw, str):
        extra.update(p.strip() for p in raw.split(",") if p.strip())
    elif isinstance(raw, (list, tuple, set)):
        extra.update(str(p).strip() for p in raw if str(p).strip())
    env_raw = os.environ.get("TIG_C3_HW_BLOCKLIST", "")
    extra.update(p.strip() for p in env_raw.split(",") if p.strip())
    return _DEFAULT_HW_BLOCKLIST | extra


def _list_cpu_profiles(env: dict) -> list[dict]:
    """The control plane's currently-available CPU profiles as
    ``{name, tier, vcpu, rate}`` dicts (tier = _AVAILABILITY_RANK value, rate
    in GBP/hour, both 0 when unlisted). Cached for _HW_CACHE_TTL_SECS —
    failures too, so a dead CLI is probed once per TTL, not once per
    benchmark. Empty when the query fails (offline, stale CLI, no auth)."""
    global _hw_cache
    now = time.monotonic()
    if _hw_cache is not None and now - _hw_cache[0] < _HW_CACHE_TTL_SECS:
        return _hw_cache[1]
    profiles: list[dict] = []
    result = _run_c3(["c3", "list", "-al", "--json"], env, ROOT, _C3_CLI_TIMEOUT_SECS)
    if result is not None and result.returncode == 0 and (result.stdout or "").strip():
        try:
            data = json.loads(result.stdout)
            for cls in data.get("hardware") or []:
                for p in cls.get("profiles") or []:
                    if p.get("hardware_kind") != "cpu" or not p.get("available"):
                        continue
                    name = str(p.get("hardware_profile", ""))
                    if not name:
                        continue
                    profiles.append({
                        "name": name,
                        "tier": _AVAILABILITY_RANK.get(
                            str(p.get("availability_tier", "")).lower(), 0),
                        "vcpu": int(p.get("vcpu") or 0),
                        "rate": float(p.get("rate_per_hour_gbp") or 0.0),
                    })
        except (ValueError, KeyError, TypeError) as exc:
            print(f"    [C3] hardware listing unparseable ({exc}) — "
                  f"using {_DEFAULT_CPU_HARDWARE}")
            profiles = []
    _hw_cache = (now, profiles)
    return profiles


def _best_cpu_hardware(env: dict, cfg: dict | None = None,
                       total_instances: int | None = None) -> str:
    """The CPU profile to deploy THIS benchmark on, sized to the workload.

    Availability tier is a reliability gate, not a ranking: LOW pools may
    simply never provision, so they're considered only when nothing better is
    listed. Among the trustworthy pools the pick minimizes the estimated cost
    of this benchmark — hourly rate × estimated job wall clock (shard fixed
    cost + solving waves at `timeout` each) — with the wall clock itself
    priced at the cheapest pool's rate, so a bigger box wins only when the
    time it saves is worth what it bills. A 5-instance benchmark therefore
    lands on a 4-vCPU box (workers beyond the instance count just idle cores
    at the big box's rate); a 1000-instance benchmark still gets the 96-vCPU
    one. An explicitly configured `bench_workers` caps every profile's
    parallelism the same way, which collapses the big boxes' one advantage
    and routes the pick to the cheapest rate.

    When the workload can't be costed (no instance count, no per-instance
    `timeout`, no listed rates) the old ranking applies — most vCPUs, then
    cheaper rate. Blocklisted pools are skipped throughout (_hw_blocklist);
    _DEFAULT_CPU_HARDWARE covers a failed control-plane query."""
    cfg = cfg or {}
    blocked = _hw_blocklist(cfg)
    profiles = [p for p in _list_cpu_profiles(env) if p["name"] not in blocked]
    if not profiles:
        return _DEFAULT_CPU_HARDWARE
    pool = [p for p in profiles
            if p["tier"] >= _AVAILABILITY_RANK["medium"]] or profiles
    try:
        n = int(total_instances or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        timeout = float(cfg.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    rated = [p for p in pool if p["rate"] > 0]
    if n > 0 and timeout > 0 and rated:
        # Unrated pools get a flat per-vCPU estimate so a missing price reads
        # as "typical", never as "free".
        vcpu_rates = [p["rate"] / p["vcpu"] for p in rated if p["vcpu"] > 0]
        per_vcpu = (sum(vcpu_rates) / len(vcpu_rates)) if vcpu_rates \
            else max(p["rate"] for p in rated)
        wall_rate = min(p["rate"] for p in rated)
        fixed = _shard_fixed_secs(cfg)
        scored = []
        for p in pool:
            workers = _profile_workers(p["vcpu"], cfg)
            waves = math.ceil(n / workers)
            wall = fixed + waves * timeout
            rate = p["rate"] if p["rate"] > 0 \
                else per_vcpu * (p["vcpu"] or _ASSUMED_SHARD_VCPUS)
            job_cost = rate * wall / 3600.0
            scored.append((job_cost + wall_rate * wall / 3600.0, wall,
                           -p["tier"], -p["vcpu"], p["name"],
                           (p, waves, job_cost)))
        scored.sort(key=lambda s: s[:5])
        p, waves, job_cost = scored[0][5]
        print(f"    [C3] auto hardware: {p['name']} ({p['vcpu']} vCPU — "
              f"{n} instance(s) in {waves} wave(s), ~£{job_cost:.3f}/job)")
        return p["name"]
    pool.sort(key=lambda p: (p["tier"], p["vcpu"], -p["rate"]), reverse=True)
    best = pool[0]
    print(f"    [C3] auto hardware: {best['name']} "
          f"({best['vcpu']} vCPU, tier rank {best['tier']})")
    return best["name"]


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


def _flag(value, default: bool) -> bool:
    """Interpret a tri-state config/env flag: unset (None / empty string) means
    `default`, the usual falsey spellings mean False, anything else True.

    Needed because these knobs now default ON — a bare truthiness test would
    read `TIG_C3_WARM_IMAGES=0` as "enabled", so opting back out via env would
    be impossible."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text not in ("0", "false", "no", "off")


def _warm_c3_image(cfg: dict) -> str | None:
    """Resolve the pre-baked WARM image for this challenge's flavor, or None
    to use the plain toolchain image + full-source staging.

    Warm images (Dockerfile.warm / scripts/build_warm_image.sh) carry the
    swarm crate source and a pre-built release cargo target, so a job only
    injects the algorithm dir and does an incremental build — seconds-to-a-
    minute instead of a cold 10-20 minute compile. That fixed cost is what
    makes sharding worth paying for (see _worthwhile_shards), so warm is the
    DEFAULT: the full-source path is the fallback you opt into, not the norm.

    Resolution: explicit ref (`c3_warm_image` config / TIG_C3_WARM_IMAGE env)
    wins; else `docker.io/<ns>/tig-swarm-warm-{cpu|gpu}:latest` from the
    `tig_dockerhub` config / TIG_DOCKERHUB env namespace (default: the TIG
    Foundation's public namespace, published by CI build-warm-images.yml).
    Opt out with `c3_warm_images: false` (or TIG_C3_WARM_IMAGES=0) — worth
    doing only if you're running a namespace whose images aren't published.

    A drifted or stale-cached image self-corrects: the job overlays the
    current Cargo manifests and src/ tree under a cmp guard, so a benchmark
    stays correct (just slower) rather than failing on a missing API.

    UPGRADING RUST: CI also publishes a toolchain-scoped tag
    (`tig-swarm-warm-{cpu|gpu}:rust<version>`). `:latest` stays the default
    because it always exists, but it is republished in place — so a Rust bump
    changes the image under every running swarm at once, and cargo
    fingerprints the compiler, meaning the baked target no longer matches and
    every job cold-compiles. To upgrade without that: pin `c3_warm_image` to
    your CURRENT compiler's tag, bump the pin, let CI publish the new one,
    then move the fleets over. Rolling back is then a config edit, not a
    rebuild."""
    explicit = cfg.get("c3_warm_image") or os.environ.get("TIG_C3_WARM_IMAGE")
    if explicit:
        return explicit
    enabled = cfg.get("c3_warm_images")
    if enabled is None:
        enabled = os.environ.get("TIG_C3_WARM_IMAGES")
    if not _flag(enabled, _WARM_IMAGES_DEFAULT):
        return None
    ns = (cfg.get("tig_dockerhub") or os.environ.get("TIG_DOCKERHUB")
          or "tigfoundation")
    kind = "gpu" if _is_gpu_config(cfg) else "cpu"
    return f"docker.io/{ns}/tig-swarm-warm-{kind}:latest"


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


# ── Workspace staging ──────────────────────────────────────────────


def _write_current_source_files(stage: Path, config: dict) -> None:
    challenge = config.get("challenge", "unknown")
    algorithm_rel = config.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs")
    algorithm_code = read_optional(ROOT / algorithm_rel)
    if algorithm_code:
        algorithm_path = stage / algorithm_rel
        algorithm_path.parent.mkdir(parents=True, exist_ok=True)
        _write_container_file(algorithm_path, algorithm_code)

    kernel_rel = config.get("kernel_path")
    if kernel_rel:
        kernel_code = read_optional(ROOT / kernel_rel)
        if kernel_code:
            kernel_path = stage / kernel_rel
            kernel_path.parent.mkdir(parents=True, exist_ok=True)
            _write_container_file(kernel_path, kernel_code)


def _copy_build_config(stage: Path) -> None:
    """Stage `.cargo/config.toml` + `rust-toolchain.toml` into a C3 workspace.

    Best-effort by design: an older clone may not have either file, and a
    missing build config must not fail the benchmark — it just means the job
    builds the way it did before these were introduced."""
    cargo_dir = stage / ".cargo"
    for rel in (Path(".cargo") / "config.toml", Path("rust-toolchain.toml")):
        src = ROOT / rel
        if not src.exists():
            continue
        if rel.parent.name == ".cargo":
            cargo_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, stage / rel)


def _create_workspace(stage: Path, config: dict, server: str) -> dict:
    """Create the minimal TIG workspace C3 should upload."""
    challenge = config.get("challenge", "unknown")
    staged_config = dict(config)
    staged_config["server_url"] = server

    for name in ("Cargo.toml", "Cargo.lock", "requirements.txt"):
        _copy_required(ROOT / name, stage / name)
    # The build config and toolchain pin travel with the manifests. Without
    # them the job compiles under a different compiler and WITHOUT
    # --cap-lints, so a lint promotion fails in the cloud while local builds
    # pass — the hardest version of this bug to track down.
    _copy_build_config(stage)

    src_stage = stage / "src"
    src_stage.mkdir(parents=True, exist_ok=True)
    for name in (
        "lib.rs",
        "main_generator.rs",
        "main_solver.rs",
        "main_evaluator.rs",
        "main_gpu_benchmark.rs",
    ):
        _copy_optional(ROOT / "src" / name, src_stage / name)
    _copy_required(ROOT / "src" / challenge, src_stage / challenge)
    _write_current_source_files(stage, staged_config)

    scripts_stage = stage / "scripts"
    scripts_stage.mkdir(parents=True, exist_ok=True)
    for script in (ROOT / "scripts").glob("*.py"):
        _copy_required(script, scripts_stage / script.name)

    _write_container_file(
        stage / ".swarm-cache.json",
        json.dumps(staged_config, indent=2, sort_keys=True) + "\n",
    )
    return staged_config


def _write_c3_project(
    stage: Path,
    config: dict,
    server: str,
    c3_time: str,
    image: str,
    seed: str | None = None,
    hyperparameters: str | None = None,
) -> str:
    run_id = uuid.uuid4().hex[:10]
    challenge = config.get("challenge", "unknown")
    script_name = f"run-benchmark-{run_id}.sh"
    hardware = _resolve_c3_hardware(config)
    requires_accelerator = _docker_requires_accelerator(config)

    c3_config = f"""\
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
    _write_container_file(stage / ".c3", c3_config)

    gpu_check = ""
    if _is_gpu_config(config):
        gpu_check = """
if ! command -v nvcc >/dev/null 2>&1; then
  echo "nvcc is required for GPU challenges; use a CUDA devel Docker image" >&2
  exit 127
fi
"""

    identity_export = ""
    tig_user_id = str(config.get("tig_user_id") or "").strip()
    if tig_user_id:
        identity_export = f"export TIG_USER_ID={_yaml_quote(tig_user_id)}"

    # Hyperparameter-search hooks (see benchmark.py): forwarded into the c3 job
    # env so a remote benchmark tunes/scores with the right seed and solver
    # hyperparameters.
    hpo_exports = ""
    if seed is not None:
        hpo_exports += f"\nexport TIG_BENCH_SEED={_yaml_quote(seed)}"
    if hyperparameters is not None:
        hpo_exports += f"\nexport TIG_HYPERPARAMETERS={_yaml_quote(hyperparameters)}"
    # Distributed shard window (see _plan_shards): per-track first-instance
    # offsets benchmark.py applies so this job runs exactly its slice.
    starts = {k: int(v) for k, v in (config.get("track_starts") or {}).items()}
    if starts:
        hpo_exports += f"\nexport TIG_TRACK_STARTS={_yaml_quote(json.dumps(starts))}"

    runner = f"""\
#!/bin/bash
set -euo pipefail

cd "${{C3_JOB_WORKDIR:-/workspace}}"
mkdir -p "${{C3_ARTIFACTS_DIR}}" c3-artifacts

export TIG_IN_DOCKER=1
export TIG_SWARM_SERVER={_yaml_quote(server)}
{identity_export}{hpo_exports}
export PATH="${{HOME:-/root}}/.cargo/bin:/root/.cargo/bin:${{PATH}}"

needs_apt=0
command -v python3 >/dev/null 2>&1 || needs_apt=1
python3 -m pip --version >/dev/null 2>&1 || needs_apt=1
command -v curl >/dev/null 2>&1 || needs_apt=1
command -v cc >/dev/null 2>&1 || needs_apt=1
test -e /usr/include/openssl/ssl.h || needs_apt=1

if [ "$needs_apt" -eq 1 ]; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "missing required system tools and apt-get is unavailable in this image" >&2
    exit 127
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates build-essential python3 python3-pip pkg-config libssl-dev
fi

if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  export PATH="${{HOME:-/root}}/.cargo/bin:/root/.cargo/bin:${{PATH}}"
fi

PIP_ROOT_USER_ACTION=ignore python3 -m pip install --break-system-packages -q -r requirements.txt
{gpu_check}
status=0
python3 scripts/benchmark.py \\
  > "${{C3_ARTIFACTS_DIR}}/benchmark.json" \\
  2> "${{C3_ARTIFACTS_DIR}}/benchmark.stderr" || status=$?

cp "${{C3_ARTIFACTS_DIR}}/benchmark.json" c3-artifacts/benchmark.json 2>/dev/null || true
cp "${{C3_ARTIFACTS_DIR}}/benchmark.stderr" c3-artifacts/benchmark.stderr 2>/dev/null || true

exit "$status"
"""
    script_path = stage / script_name
    _write_container_file(script_path, runner)
    script_path.chmod(0o755)
    return script_name


def _create_warm_workspace(stage: Path, config: dict, server: str) -> dict:
    """Stage the upload for a warm-image job: the algorithm dir (multi-file
    sources + CUDA kernels), current scripts/, Cargo manifests + the crate
    source (both cmp-guarded overlays in the runner) and the locked config.
    The pre-built cargo target is already in the image at /app.

    src/ (the challenge harnesses, ~0.5MB with algorithm dirs excluded) rides
    along so a C3 node serving a STALE cached :latest warm image still builds
    against the current harness/cudarc pairing — without it, an algorithm
    written against the current API fails on such a node with
    method-not-found errors (seen live: `memcpy_dtov` E0599s on hypergraph
    while other shards of the same benchmark compiled fine). The runner's
    per-file cmp guard keeps every baked mtime when nothing drifted, so an
    up-to-date image stays a pure cache hit."""
    challenge = config.get("challenge", "unknown")
    staged_config = dict(config)
    staged_config["server_url"] = server

    for name in ("Cargo.toml", "Cargo.lock"):
        _copy_required(ROOT / name, stage / name)
    _copy_build_config(stage)

    shutil.copytree(
        ROOT / "src", stage / "src",
        ignore=shutil.ignore_patterns("algorithm", "__pycache__", "target"),
    )

    _copy_required(ROOT / "src" / challenge / "algorithm", stage / "algorithm")

    scripts_stage = stage / "scripts"
    scripts_stage.mkdir(parents=True, exist_ok=True)
    for script in (ROOT / "scripts").glob("*.py"):
        _copy_required(script, scripts_stage / script.name)

    _write_container_file(
        stage / ".swarm-cache.json",
        json.dumps(staged_config, indent=2, sort_keys=True) + "\n",
    )
    return staged_config


def _write_warm_c3_project(
    stage: Path,
    config: dict,
    server: str,
    c3_time: str,
    image: str,
    seed: str | None = None,
    hyperparameters: str | None = None,
) -> str:
    """The `.c3` + runner for a warm-image job (see _warm_c3_image). The
    runner overlays the uploaded orchestration onto the baked crate at /app,
    replaces ONLY the algorithm dir, and runs benchmark.py there — cargo hits
    the pre-built target and rebuilds just the crate + relinks."""
    run_id = uuid.uuid4().hex[:10]
    challenge = config.get("challenge", "unknown")
    script_name = f"run-warm-{run_id}.sh"
    hardware = _resolve_c3_hardware(config)
    requires_accelerator = _docker_requires_accelerator(config)

    c3_config = f"""\
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
    _write_container_file(stage / ".c3", c3_config)

    gpu_check = ""
    if _is_gpu_config(config):
        gpu_check = """
if ! command -v nvcc >/dev/null 2>&1; then
  echo "nvcc is required for GPU challenges; the warm GPU image should be built FROM a CUDA devel base" >&2
  exit 127
fi
"""

    identity_export = ""
    tig_user_id = str(config.get("tig_user_id") or "").strip()
    if tig_user_id:
        identity_export = f"export TIG_USER_ID={_yaml_quote(tig_user_id)}"

    hpo_exports = ""
    if seed is not None:
        hpo_exports += f"\nexport TIG_BENCH_SEED={_yaml_quote(seed)}"
    if hyperparameters is not None:
        hpo_exports += f"\nexport TIG_HYPERPARAMETERS={_yaml_quote(hyperparameters)}"
    # Distributed shard window (see _plan_shards): per-track first-instance
    # offsets benchmark.py applies so this job runs exactly its slice.
    starts = {k: int(v) for k, v in (config.get("track_starts") or {}).items()}
    if starts:
        hpo_exports += f"\nexport TIG_TRACK_STARTS={_yaml_quote(json.dumps(starts))}"

    runner = f"""\
#!/bin/bash
set -euo pipefail

cd "${{C3_JOB_WORKDIR:-/workspace}}"
WORK="$PWD"
mkdir -p "${{C3_ARTIFACTS_DIR}}" c3-artifacts

export TIG_IN_DOCKER=1
export TIG_SWARM_SERVER={_yaml_quote(server)}
{identity_export}{hpo_exports}
export PATH="${{HOME:-/root}}/.cargo/bin:/root/.cargo/bin:${{PATH}}"

APP=/app
if [ ! -d "$APP/target/release" ]; then
  echo "warm image is missing the pre-built /app/target — was it built from Dockerfile.warm?" >&2
  exit 127
fi

# Overlay the CURRENT orchestration + config onto the baked crate. The Cargo
# manifests are cmp-guarded so an unchanged manifest keeps its baked mtime and
# the no-drift case stays a pure cache hit.
cp .swarm-cache.json "$APP/.swarm-cache.json"
cp -f scripts/*.py "$APP/scripts/"
cmp -s Cargo.toml "$APP/Cargo.toml" || cp Cargo.toml "$APP/Cargo.toml"
cmp -s Cargo.lock "$APP/Cargo.lock" || cp Cargo.lock "$APP/Cargo.lock"
mkdir -p "$APP/.cargo"
cmp -s .cargo/config.toml "$APP/.cargo/config.toml" 2>/dev/null \
  || cp .cargo/config.toml "$APP/.cargo/config.toml" 2>/dev/null || true
cmp -s rust-toolchain.toml "$APP/rust-toolchain.toml" 2>/dev/null \
  || cp rust-toolchain.toml "$APP/rust-toolchain.toml" 2>/dev/null || true

# Overlay the current crate source (challenge harnesses) file-by-file, same
# cmp guard: an up-to-date image keeps every baked mtime (pure cache hit),
# while a node stuck on a stale cached :latest rebuilds against the CURRENT
# harness/cudarc pairing instead of failing the algorithm build with
# method-not-found errors.
if [ -d src ]; then
  ( cd src && find . -type f | while IFS= read -r f; do
      if ! cmp -s "$f" "$APP/src/$f"; then
        mkdir -p "$APP/src/$(dirname "$f")"
        cp "$f" "$APP/src/$f"
      fi
    done )
fi

# Inject ONLY the algorithm (whole dir: multi-file sources + CUDA kernels).
rm -rf "$APP/src/{challenge}/algorithm"
cp -r algorithm "$APP/src/{challenge}/algorithm"
{gpu_check}
cd "$APP"
status=0
python3 scripts/benchmark.py \\
  > "${{C3_ARTIFACTS_DIR}}/benchmark.json" \\
  2> "${{C3_ARTIFACTS_DIR}}/benchmark.stderr" || status=$?

cp "${{C3_ARTIFACTS_DIR}}/benchmark.json" "$WORK/c3-artifacts/benchmark.json" 2>/dev/null || true
cp "${{C3_ARTIFACTS_DIR}}/benchmark.stderr" "$WORK/c3-artifacts/benchmark.stderr" 2>/dev/null || true

exit "$status"
"""
    script_path = stage / script_name
    _write_container_file(script_path, runner)
    script_path.chmod(0o755)
    return script_name


# ── Fleet-wide C3 slot pool ────────────────────────────────────────


def _max_parallel(cfg: dict) -> int:
    """Fleet-wide C3 slot count = the CEILING on a benchmark's shard count
    (`_worthwhile_shards` decides how much of it to actually use). Clamped >= 1."""
    try:
        value = int(cfg.get("c3_max_parallel_jobs", _DEFAULT_MAX_PARALLEL_JOBS))
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_PARALLEL_JOBS
    return max(1, value)


# ── How many of those slots a given benchmark should actually spend ──


def _hardware_vcpus(hardware: str | None) -> int:
    """vCPU count out of a C3 profile name (`cpu-e2-48vcpu-192gb` -> 48).
    0 when the name doesn't carry one (`auto`, `l40`)."""
    m = re.search(r"(\d+)vcpu", str(hardware or "").lower())
    return int(m.group(1)) if m else 0


def _profile_workers(vcpu: int, cfg: dict) -> int:
    """Concurrent solvers a CPU job would run on a box with ``vcpu`` vCPUs:
    an explicitly configured bench_workers wins, else
    benchmark.resolve_bench_workers' machine-scaled default (half the vCPUs
    ~= the physical cores)."""
    for raw in (os.environ.get("TIG_BENCH_WORKERS"), cfg.get("bench_workers")):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return max(4, (vcpu or _ASSUMED_SHARD_VCPUS) // 2)


def _estimate_shard_workers(cfg: dict) -> int:
    """Concurrent solvers ONE shard job will run. Only used to size the shard
    plan, so an estimate is fine. GPU jobs are one solver per job — sharding
    IS their parallelism."""
    if _is_gpu_config(cfg):
        return 1
    return _profile_workers(_hardware_vcpus(cfg.get("c3_hardware")), cfg)


def _shard_fixed_secs(cfg: dict) -> float:
    """Wall clock one extra shard costs before it does any solving."""
    override = cfg.get("c3_shard_fixed_secs")
    if override not in (None, ""):
        try:
            value = float(override)
        except (TypeError, ValueError):
            value = -1.0
        if value >= 0:
            return value
    return (_SHARD_FIXED_SECS_WARM if _warm_c3_image(cfg)
            else _SHARD_FIXED_SECS_FULL_SOURCE)


def _worthwhile_shards(total: int, cfg: dict, cap: int) -> int:
    """How many shards this benchmark should use — at most `cap`, often fewer.

    Sharding is not free: every shard provisions its own C3 box and repeats the
    build, and because shards run in PARALLEL the slowest provision sets the
    wall clock. Splitting a benchmark whose solving takes a minute across three
    boxes that take five minutes each to appear buys nothing, bills three
    machines, and starves fleet-mates of their slot pool.

    So spend a slot only when it pays for itself. Going from `s-1` to `s`
    shards saves `solve / (s * (s - 1))` of wall clock; take it only while that
    beats the fixed cost of the machine it takes to get it. Never plan more
    shards than solving "waves" either — a shard that can't fill its workers
    leaves cores idle and still pays a full provision.

    Falls back to the old "use the whole cap" behavior when the inputs needed
    to reason about cost are missing (no per-instance timeout, fixed cost
    explicitly zeroed)."""
    cap = max(1, min(int(cap), max(1, int(total))))
    if cap == 1:
        return 1
    try:
        timeout = float(cfg.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    fixed = _shard_fixed_secs(cfg)
    if timeout <= 0 or fixed <= 0:
        return cap
    workers = max(1, _estimate_shard_workers(cfg))
    waves = math.ceil(total / workers)
    solve = waves * timeout
    best = 1
    for s in range(2, min(cap, waves) + 1):
        if solve / (s * (s - 1)) <= fixed:
            break
        best = s
    return best


def _instance_units(tracks: dict) -> list[tuple[str, int]]:
    """The (track_key, count) pairs a benchmark actually runs. The ``seed`` key
    and any non-positive / non-int count are skipped (matches benchmark.py)."""
    return [
        (k, v) for k, v in (tracks or {}).items()
        if k != "seed" and isinstance(v, int) and v > 0
    ]


def _balanced_sizes(total: int, num_machines: int) -> list[int]:
    """Split ``total`` instances across ``min(num_machines, total)`` machines as
    evenly as possible: no two machines differ by more than one instance. The
    ``total % m`` larger shards come first.

    e.g. (22, 3) -> [8, 7, 7]; (22, 4) -> [6, 6, 5, 5]; (2, 3) -> [1, 1]."""
    m = min(max(1, int(num_machines)), total)
    base, rem = divmod(total, m)
    return [base + 1] * rem + [base] * (m - rem)


def _plan_shards(tracks: dict, num_machines: int) -> list[list[dict]]:
    """Split every track's instances into exactly ``min(num_machines, total)``
    **balanced** shards (sizes differ by at most one), packing *across* track
    boundaries so no machine is under-filled.

    Returns a list of shards; each shard is a list of ``{track_key, start,
    count}`` slices. A shard job runs benchmark.py with ``tracks`` set to its
    per-track counts and ``TIG_TRACK_STARTS`` to its per-track starts — the
    per-instance seed depends only on the global index, so the union of shard
    windows is byte-identical to the unsharded run (see
    benchmark.materialize_instances).

    e.g. tracks {a: 10, b: 8} over 3 machines (18 instances -> [6, 6, 6]):
        [a[0:6]], [a[6:10], b[0:2]], [b[2:8]]
    ``num_machines <= 1`` yields a single shard holding every slice (the
    single-job behavior). The ``seed`` key and any non-positive / non-int
    count are skipped (matches benchmark.py)."""
    units = _instance_units(tracks)
    total = sum(count for _, count in units)
    if total == 0:
        return []

    sizes = _balanced_sizes(total, num_machines)
    shards: list[list[dict]] = []
    cur: list[dict] = []
    cur_used = 0
    cap = sizes[0]
    for track_key, count in units:
        pos = 0
        while pos < count:
            if cur_used == cap:  # current shard full -> start the next one
                shards.append(cur)
                cap = sizes[len(shards)]
                cur, cur_used = [], 0
            take = min(cap - cur_used, count - pos)
            cur.append({"track_key": track_key, "start": pos, "count": take})
            pos += take
            cur_used += take
    if cur:
        shards.append(cur)
    return shards


def _merge_shard_benchmarks(shard_benches: list[dict], challenge: str) -> dict:
    """Reassemble per-shard benchmark.json dicts into one, concatenating the
    per-instance records and re-aggregating with benchmark.aggregate — so the
    merged score is identical to a single-job run of every instance.

    viz_data merges by key union (it's keyed per instance and shard windows
    are disjoint). challenge_metrics is omitted on merged runs: it's an
    opaque per-challenge roll-up computed from the full in-process results
    list, which shards don't carry."""
    results: list[dict] = []
    errors: list[str] = []
    viz: dict = {}
    for bench in shard_benches:
        for r in bench.get("instance_results") or []:
            results.append(r)
        for e in bench.get("errors") or []:
            errors.append(e)
        if isinstance(bench.get("viz_data"), dict):
            viz.update(bench["viz_data"])
    agg = _bm_aggregate(results)
    return {
        "challenge": challenge,
        **agg,
        "errors": errors or None,
        "instance_results": results,
        "viz_data": viz or None,
    }


# ── C3 command parsing / polling ───────────────────────────────────


def _parse_c3_id(text: str) -> str | None:
    """Pull the C3 job ID out of `c3 deploy` output.

    Order matters. A deploy prints the job NAME before the job ID, and this
    project's names are `tig-<challenge>-<run_id>` — so on the `job_scheduling`
    challenge the name `tig-job_scheduling-3aae77f7b5` contains a `job_…` token
    that a loose pattern grabs first. Polling then asks C3 about a job that
    never existed, gets no status back, and waits out the whole poll timeout
    while the real job completes unpulled. Hence: match C3's actual ID shape
    (`job_<epoch-ms>_<suffix>`) before anything looser, and never let the
    fallback start mid-token (the `-` before `job_scheduling` above).
    """
    for pat in [
        r'"(?:id|job_id|jobId)"\s*:\s*"(job_[^"]+)"',
        r"(?<![\w-])(job_\d{6,}_[a-zA-Z0-9]+)\b",
        r"(?<![\w-])(job_[a-zA-Z0-9_-]+)\b",
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
# States that mean "accepted, but no machine yet". A pool that never
# provisions parks a job in one of these indefinitely — see _poll_c3_job.
_PRE_RUN = ("PENDING", "SCHEDULING", "QUEUED", "SUBMITTED")
# How long a job may sit in a pre-run state before we treat the pool as unable
# to deliver. Generous (a busy pool does queue) but nowhere near the poll
# timeout, which is walltime-derived: at the default c3_time of 02:00:00 that
# is 2h45m of an agent looking hung. Override with `c3_queue_grace_secs` in
# config or TIG_C3_QUEUE_GRACE; 0 disables the check.
_QUEUE_GRACE_SECS = 1200
# Stable substring so callers can tell this apart from a job that ran and
# failed. Survives the shard-error grouping in run_benchmark_c3.
_NEVER_STARTED_MARKER = "never left the C3 queue"
# Stamped into every error whose cause is C3/pool infrastructure rather than
# the algorithm. run_loop._benchmark_with_compile_fix routes on it: an error
# carrying this marker must never spend an LLM "compile fix" on code that was
# never the problem (seen live: a pool killed a job before benchmark.py wrote
# a byte, the profile was correctly blocklisted — and the LLM was then asked
# to "fix" the working algorithm). Keep it on the error's FIRST line: the
# shard-error grouping keys on the body after line one.
C3_INFRA_MARKER = "[C3 infra]"
# C3's internal-error banner ("The job could not be completed. Contact C3
# support with the error code below. Code: C3-JOB-…"). It shows up in the
# job's LOGS while `c3 squeue` can go on reporting the job as RUNNING — the
# job is dead but never reaches a terminal status, so without a log check
# the swarm sits "watching" a corpse until the walltime-derived timeout,
# holding its fleet-pool slot the whole time. _poll_c3_job samples the logs
# for the banner every _SUPPORT_ERROR_POLL_EVERY polls (~1 minute).
_SUPPORT_ERROR_MARKERS = ("could not be completed", "contact c3 support")
_SUPPORT_CODE_RE = re.compile(r"\bcode:\s*(C3-[A-Z0-9-]+)", re.IGNORECASE)
_SUPPORT_ERROR_POLL_EVERY = 4


def _support_error_code(text: str) -> str | None:
    """The support code out of a C3 internal-error banner in ``text``, or
    None when no banner is present ("unknown" when the banner appears
    without a parseable code)."""
    low = (text or "").lower()
    if not any(marker in low for marker in _SUPPORT_ERROR_MARKERS):
        return None
    m = _SUPPORT_CODE_RE.search(text)
    return m.group(1) if m else "unknown"


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
    except FileNotFoundError:
        print(f"    [C3] `{cmd[0]}` not found on PATH")
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
    cfg: dict | None = None,
) -> str:
    """Poll a C3 job until it reaches a terminal state.

    A job whose status NEVER resolves once is not a slow job — it's a job C3
    has never heard of (a mis-parsed ID, a deploy that half-landed). Waiting
    out the full walltime for that is silent and expensive: the fleet pool slot
    is held the whole time and the agent looks hung. So give an unrecognised ID
    a bounded grace period and then fail fast with a message that names the
    cause. A job that resolves even once gets the full timeout — after that,
    unknown really can mean transient API flakiness.

    The same reasoning covers a job C3 *does* know about but never starts.
    Some pools accept a deploy and leave it QUEUED forever; that resolves a
    status fine, so the unrecognised-ID guard above never fires and the agent
    burns the whole walltime-derived timeout on a machine it will never get.
    A job that hasn't reached RUNNING within the queue grace gives up as
    `never_started`, which the caller turns into "try different hardware".

    Finally, a job can die on a C3-internal error ("Contact C3 support …
    Code: C3-JOB-…") while squeue goes on reporting it RUNNING. That banner
    only ever shows up in the job's logs, so they're sampled every few polls;
    a hit returns `infra_error:<code>` and the caller cancels the job (C3
    still counts it against the plan cap) and moves off the hardware.
    """
    wait_secs = timeout_secs if timeout_secs is not None else max(1800, walltime_secs + 2700)
    deadline = time.monotonic() + wait_secs
    queue_grace = min(_queue_grace_secs(cfg), wait_secs)
    queue_deadline = time.monotonic() + queue_grace
    ever_running = False
    poll = 0
    # Log on status CHANGE plus a ~5-minute heartbeat, not every minute:
    # with sharding there are several concurrent polls and the old cadence
    # filled the console with identical RUNNING lines.
    last_printed: str | None = None
    ever_resolved = False
    unknown_deadline = time.monotonic() + min(_UNRECOGNISED_JOB_GRACE_SECS, wait_secs)
    while time.monotonic() < deadline:
        status = _read_job_status(job_id, env, cwd)
        if status:
            ever_resolved = True
            if any(s in status for s in _TERMINAL_OK):
                return "completed"
            if any(s in status for s in _TERMINAL_BAD):
                return "failed"
            if not any(s in status for s in _PRE_RUN):
                ever_running = True  # RUNNING (or anything past the queue)
            elif (not ever_running and queue_grace
                    and time.monotonic() >= queue_deadline):
                print(f"    [C3] {job_id} sat in {status} for "
                      f"{queue_grace // 60}m without starting — giving up on "
                      f"this pool instead of waiting out the "
                      f"{wait_secs // 60}m timeout")
                return "never_started"
            if status != last_printed or poll % 20 == 0:
                print(f"    [C3] {job_id}: {status}")
                last_printed = status
        else:
            if not ever_resolved and time.monotonic() >= unknown_deadline:
                print(f"    [C3] {job_id} never appeared in `c3 squeue` after "
                      f"{_UNRECOGNISED_JOB_GRACE_SECS // 60}m — giving up "
                      f"instead of waiting out the {wait_secs // 60}m timeout")
                return "unrecognised"
            if poll % 20 == 0:
                print(f"    [C3] Still waiting on {job_id} (poll {poll + 1})...")
        if poll % _SUPPORT_ERROR_POLL_EVERY == _SUPPORT_ERROR_POLL_EVERY - 1:
            code = _support_error_code(_read_logs(job_id, env, cwd))
            if code:
                print(f"    [C3] {job_id} died with a C3 internal error "
                      f"(support code {code}) — squeue still said "
                      f"{status or 'unknown'}, but the job is gone")
                return f"infra_error:{code}"
        poll += 1
        time.sleep(_POLL_INTERVAL_SECS)
    return "timeout"


def _queue_grace_secs(cfg: dict | None) -> int:
    """Seconds a job may sit unstarted before we call the pool undeliverable.
    Config `c3_queue_grace_secs` / env TIG_C3_QUEUE_GRACE; 0 disables."""
    raw = os.environ.get("TIG_C3_QUEUE_GRACE")
    if raw is None:
        raw = (cfg or {}).get("c3_queue_grace_secs", _QUEUE_GRACE_SECS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return _QUEUE_GRACE_SECS


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


def _run_one_c3_job_inner(
    args: argparse.Namespace, env: dict, stage: Path, label: str,
    _cli_updated: bool = False, echo_stderr: bool = True,
    cfg: dict | None = None,
) -> tuple[dict | None, str]:
    """Deploy one already-staged benchmark job, poll it, pull artifacts, and
    return its ``benchmark.json`` dict. ``stage`` must already contain the
    `.c3` + runner script.

    `_cli_updated` is the self-heal recursion guard: after one
    update-and-retry, a still-stale CLI falls through to the actionable error
    (the likely cause then is an old copy shadowing the new one on PATH)."""
    cmd = ["c3", "deploy"]
    provider = _arg_value(args, "c3_provider")
    if provider:
        cmd.extend(["-p", provider])

    combined = ""
    throttle_attempt = 0
    concurrency_deadline: float | None = None
    concurrency_waits = 0
    while True:
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
        stale = _stale_cli_error(combined)
        if stale:
            # Self-heal: update the CLI and retry this job once. The stage
            # dir is untouched by a failed deploy, so a straight re-run is safe.
            if not _cli_updated and _update_c3_cli_once():
                return _run_one_c3_job_inner(
                    args, env, stage, label, _cli_updated=True,
                    echo_stderr=echo_stderr, cfg=cfg,
                )
            return None, stale
        # Plan concurrent-chip cap (see _CONCURRENCY_RETRY_SIGNATURES): wait for
        # a chip to free and retry — no job was queued, so nothing is orphaned.
        # Checked before the object-store throttle because it needs the long
        # wait, not the sub-20s upload backoff.
        if any(sig in combined for sig in _CONCURRENCY_RETRY_SIGNATURES):
            now = time.monotonic()
            if concurrency_deadline is None:
                concurrency_deadline = now + _CONCURRENCY_MAX_WAIT_SECS
            if now >= concurrency_deadline:
                return None, (
                    f"c3 deploy failed ({proc.returncode}): C3 concurrent-chip "
                    f"limit still reached after waiting "
                    f"{_CONCURRENCY_MAX_WAIT_SECS / 60:.0f}m for a chip to free "
                    f"— jobs outside this fleet (a manual `c3` run, or another "
                    f"fleet on the same C3 key) are holding every slot. Cancel "
                    f"them (`c3 squeue` / `c3 cancel`) or raise the plan "
                    f"tier:\n{combined[-1500:]}"
                )
            concurrency_waits += 1
            delay = random.uniform(_CONCURRENCY_RETRY_MIN_SECS, _CONCURRENCY_RETRY_MAX_SECS)
            remaining = concurrency_deadline - now
            print(
                f"    [C3][{label}]   C3 concurrent-chip limit reached; waiting "
                f"{delay:.0f}s for a slot (retry {concurrency_waits}, "
                f"≤{remaining / 60:.0f}m left)"
            )
            time.sleep(delay)
            continue
        # Retry only the object-store throttle (see _DEPLOY_RETRY_SIGNATURES);
        # every other deploy failure fails fast.
        throttled = any(sig in combined for sig in _DEPLOY_RETRY_SIGNATURES)
        throttle_attempt += 1
        if not throttled or throttle_attempt >= _DEPLOY_MAX_ATTEMPTS:
            return None, f"c3 deploy failed ({proc.returncode}):\n{combined[-2000:]}"
        window = min(_DEPLOY_BACKOFF_CAP_SECS, _DEPLOY_BACKOFF_BASE_SECS * 2 ** (throttle_attempt - 1))
        delay = random.uniform(0, window)
        print(
            f"    [C3][{label}]   deploy throttled (429 same-object); "
            f"retry {throttle_attempt}/{_DEPLOY_MAX_ATTEMPTS - 1} in {delay:.1f}s"
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
        job_id, env, stage, walltime_secs, _c3_poll_timeout(args), cfg=cfg,
    )
    if status == "unrecognised":
        # C3 doesn't know this ID, so pull/logs/cancel would all just burn
        # their timeouts against nothing. The real job (if the deploy landed)
        # is findable by its unique job_name — cancel it so it can't run on
        # untracked and keep billing the account.
        _cancel_deployed_job(stage, env)
        return None, (
            f"[C3] job {job_id} was never recognised by C3 — the deploy "
            f"output may not have carried a usable job ID:\n{combined[-1000:]}"
        )
    if status == "never_started":
        # It's still queued, so it still holds a chip against the plan cap.
        # Cancel before returning or the slot leaks until C3 reaps it.
        _cancel_c3_job(job_id, env, stage)
        profile = str((cfg or {}).get("c3_hardware") or "?")
        if (cfg or {}).get("c3_hardware_auto"):
            _blocklist_profile_for_session(
                profile, "it accepted a job and never ran it")
        return None, (
            f"{C3_INFRA_MARKER} job {job_id} {_NEVER_STARTED_MARKER} on "
            f"{profile} — the pool accepted the job but never gave it a "
            f"machine. Cancelled it rather than waiting out the full timeout."
        )
    if status.startswith("infra_error"):
        code = status.partition(":")[2] or "unknown"
        # C3 still lists the job as live, so it still holds a chip against
        # the plan cap — cancel rather than trust C3 to reap it.
        _cancel_c3_job(job_id, env, stage)
        profile = str((cfg or {}).get("c3_hardware") or "?")
        if (cfg or {}).get("c3_hardware_auto"):
            _blocklist_profile_for_session(
                profile, f"its job died with C3 internal error {code}")
            hint = "the next benchmark will pick different hardware"
        else:
            hint = (f"c3_hardware is pinned to {profile} — try a different "
                    f"profile, or switch this agent to local compute")
        return None, (
            f"{C3_INFRA_MARKER} job {job_id} died with a C3 internal error "
            f"on {profile} (support code {code}, for C3 support) while C3 "
            f"still reported it running. Cancelled it; {hint}."
        )
    cancel_output = ""
    if status == "timeout" and _arg_value(args, "c3_cancel_on_timeout", False):
        cancel_output = _cancel_c3_job(job_id, env, stage)
        print(f"    [C3][{label}] canceled timed-out job {job_id}")
    # Pull artifacts even on failure: benchmark.py's real failure (cargo/rustc
    # errors) lands in benchmark.stderr, not the job's stdout, which only
    # carries runner noise (e.g. the rustup install banner).
    _pull_artifacts(job_id, env, stage)
    if status != "completed":
        bench_stderr = _read_benchmark_stderr(stage)
        # A job that died without our benchmark writing a single byte never
        # got as far as running the code — that's the pool, not the algorithm
        # (a cargo error would be sitting in bench_stderr). This is the shape
        # the old hardcoded blocklist existed for: a profile whose jobs die on
        # an internal C3 error, where even a trivial `nproc` job fails.
        died_before_bench = status == "failed" and not bench_stderr
        if died_before_bench and (cfg or {}).get("c3_hardware_auto"):
            _blocklist_profile_for_session(
                str(cfg.get("c3_hardware") or "?"),
                "its job died before the benchmark produced any output")
        if bench_stderr:
            # Don't dump the stderr here: sibling shards usually fail with the
            # IDENTICAL error (they compile the same code), so per-shard dumps
            # printed the same cargo errors once per shard. The full text
            # rides in the returned err; run_benchmark_c3 dedupes across
            # shards and prints each distinct error once.
            print(f"    [C3][{label}] job failed — captured "
                  f"{len(bench_stderr)} chars of benchmark.stderr")
        logs_out = _read_logs(job_id, env, stage)
        details = "\n".join(
            part
            for part in (
                cancel_output[-1000:], bench_stderr[-4000:], logs_out[-3000:],
            )
            if part
        )
        # Inline on the head line, not a line of its own: the shard-error
        # grouping splits head from body at the first newline, and a
        # prepended line would put the per-shard job ID into the body and
        # break the dedup.
        infra_note = (
            f" — {C3_INFRA_MARKER} died before the benchmark produced any "
            f"output (a C3/pool problem, not the algorithm)"
            if died_before_bench else ""
        )
        return None, f"job {job_id} {status}{infra_note}\n{details}"

    bench, perr = _load_benchmark_json(stage)
    if bench is None:
        logs_out = _read_logs(job_id, env, stage)
        return None, f"job {job_id} completed but benchmark.json was not found or parseable\n{perr}\n{logs_out[-3000:]}"
    # benchmark.py writes the per-instance log + timing summary to stderr
    # (stdout is reserved for the benchmark.json payload). Echo it in full on
    # a single-job run so timing is visible on a practice bench — but shard
    # jobs (echo_stderr=False) get a one-line summary instead: N shards each
    # dumping every per-instance line drowned the console, and the merged
    # per-track scores are printed by the caller anyway. TIG_C3_VERBOSE=1
    # restores the full per-shard dump when debugging.
    bench_stderr = _read_benchmark_stderr(stage)
    if bench_stderr:
        if echo_stderr or os.environ.get("TIG_C3_VERBOSE"):
            print(f"    [C3][{label}] benchmark.stderr:\n{bench_stderr}")
        else:
            feasible = len(re.findall(r"\bfeasible\b", bench_stderr))
            infeasible = len(re.findall(r"\binfeasible\b", bench_stderr))
            print(f"    [C3][{label}] shard done: "
                  f"{feasible + infeasible} instance(s), {feasible} feasible")
    return bench, ""


def _run_one_c3_shard(
    args: argparse.Namespace, env: dict, stage: Path, label: str,
    pool, cfg: dict | None = None,
) -> tuple[dict | None, str]:
    """Fleet-wide-cap wrapper around one shard's deploy+poll. Holds a C3 slot
    from the shared FCFS `pool` for this shard's entire lifetime, so all agents
    sharing one C3 key never exceed c3_max_parallel_jobs total live C3 jobs
    (respecting the plan's concurrency limit); extra shards queue FCFS."""
    with pool.lease():
        return _run_one_c3_job_inner(args, env, stage, label,
                                    echo_stderr=False, cfg=cfg)


# Consecutive failed C3 benchmarks in THIS agent process. C3 is a young
# platform, and a wedged pool or a control-plane outage costs far more than
# the failures themselves: the agent keeps paying an LLM for edits it can
# never score. After a short streak, say plainly that local Docker exists.
_C3_TROUBLE_STREAK = 3
_consecutive_c3_failures = 0
_c3_advice_shown = False


def _note_c3_outcome(ok: bool, challenge: str) -> None:
    """Track the C3 failure streak and, once it's clearly not a blip, tell the
    human how to get unstuck. Printed rather than returned: the error string
    goes on to the compile-fix router and the LLM prompt, where a paragraph of
    operator advice is noise (and has misrouted errors before)."""
    global _consecutive_c3_failures, _c3_advice_shown
    if ok:
        _consecutive_c3_failures = 0
        _c3_advice_shown = False
        return
    _consecutive_c3_failures += 1
    if _consecutive_c3_failures < _C3_TROUBLE_STREAK or _c3_advice_shown:
        return
    _c3_advice_shown = True  # once per streak, not once per failure
    print(
        f"\n    [C3] ⚠  {_consecutive_c3_failures} C3 benchmarks in a row have "
        f"failed for {challenge}.\n"
        f"    [C3]    Nothing has been scored since, and each iteration still "
        f"spends LLM tokens.\n"
        f"    [C3]    To keep working while C3 is unhappy, switch this agent "
        f"to local compute:\n"
        f"    [C3]      • in the setup UI: Reconfigure fleet → Compute → "
        f"local, then Save & start\n"
        f"    [C3]      • or in fleet.config.json, set \"compute\": \"local\" "
        f"on the agent and relaunch\n"
        f"    [C3]    Local needs Docker and compiles on this machine (slower "
        f"first run, no C3 credits).\n"
        f"    [C3]    If you'd rather stay on C3, pinning \"c3_hardware\": "
        f"\"cpu-d3-4vcpu-16gb\" avoids\n"
        f"    [C3]    the larger pools, which are the ones that usually fail "
        f"to provision.\n"
    )


def run_benchmark_c3(
    args: argparse.Namespace, config: dict, server: str,
    seed: str | None = None, hyperparameters: str | None = None,
) -> tuple[dict | None, str]:
    """Run one benchmark on C3. Thin wrapper: `_run_benchmark_c3` does the
    work, this records the outcome so a run of failures can offer a way out."""
    bench, err = _run_benchmark_c3(args, config, server, seed, hyperparameters)
    _note_c3_outcome(bench is not None, str(config.get("challenge", "unknown")))
    return bench, err


def _run_benchmark_c3(
    args: argparse.Namespace, config: dict, server: str,
    seed: str | None = None, hyperparameters: str | None = None,
) -> tuple[dict | None, str]:
    if shutil.which("c3") is None:
        return None, "[C3] c3 CLI not found. Install from https://cthree.cloud/install.sh"

    c3_key = args.c3_api_key or os.environ.get("C3_API_KEY", "")
    challenge = config.get("challenge", "unknown")

    cfg = dict(config)
    cfg["server_url"] = server
    # c3_max_parallel_jobs (arg wins, then config, then default) is the
    # fleet-wide FCFS pool size.
    cfg["c3_max_parallel_jobs"] = _arg_value(
        args, "c3_max_parallel_jobs",
        cfg.get("c3_max_parallel_jobs", _DEFAULT_MAX_PARALLEL_JOBS),
    )

    warm_image = _warm_c3_image(cfg)
    image = warm_image or _select_docker_image(args, cfg)
    cfg["env"] = image

    env = os.environ.copy()
    if c3_key and not c3_key.startswith("your_"):
        env["C3_API_KEY"] = c3_key
    elif c3_key:
        env.pop("C3_API_KEY", None)

    auth_err = _check_c3_auth(env)
    if auth_err:
        return None, auth_err

    tracks = cfg.get("tracks") or {}
    total_instances = sum(count for _, count in _instance_units(tracks))

    # Hardware: an explicit request pins the profile; `auto` resolves to the
    # GPU default (l40) or, for CPU challenges, to the cheapest available CPU
    # box that fits THIS benchmark's workload — a 5-instance run gets a small
    # box, not a 96-vCPU one (see _best_cpu_hardware).
    requested = _arg_value(args, "hardware")
    raw_hw = str(
        requested if requested is not None else cfg.get("c3_hardware", "")
    ).strip().lower()
    if raw_hw in _AUTO_HARDWARE_VALUES and not _is_gpu_config(cfg):
        cfg["c3_hardware"] = _best_cpu_hardware(env, cfg, total_instances)
        # Remembered so a pool that never provisions can be dropped from
        # future auto-picks. An explicitly pinned profile is the contributor's
        # decision and is never second-guessed.
        cfg["c3_hardware_auto"] = True
    else:
        cfg["c3_hardware"] = _resolve_c3_hardware(cfg, requested)

    # Balanced intra-benchmark sharding: split this benchmark's instances
    # across shard jobs so ONE benchmark can use several chips at once (e.g. an
    # agent benchmarking while its fleet-mates wait on LLM responses). Each
    # shard runs its per-track window (TIG_TRACK_STARTS) and the per-instance
    # results are merged + re-scored, so the score matches a single-job run
    # exactly. The plan cap is a CEILING, not a target: _worthwhile_shards
    # spends a slot only when the wall clock it saves beats the provisioning it
    # costs, so a small benchmark runs as one job instead of three.
    cap = _max_parallel(cfg)
    num_shards = _worthwhile_shards(total_instances, cfg, cap)
    shards = _plan_shards(tracks, num_shards)
    if not shards:
        return None, f"[C3] No positive-count tracks to benchmark for {challenge}"
    if num_shards < cap:
        print(f"    [C3] {total_instances} instance(s): using {num_shards} of "
              f"{cap} slot(s) — {cap - num_shards} more would cost longer in "
              f"provisioning than they'd save in solving")

    # Fleet-wide FCFS pool: all agents sharing one C3 key gate on it, so
    # total live jobs never exceed the pool cap. A lone agent (no
    # C3_POOL_DIR) gets an in-process semaphore instead. run_fleet injects
    # C3_POOL_SIZE so every agent agrees on one cap.
    pool_size = int(os.environ.get("C3_POOL_SIZE") or _max_parallel(cfg))
    slot_pool = c3_pool.get_pool(pool_size, os.environ.get("C3_POOL_DIR"))

    mode = "warm" if warm_image else "full-source"
    print(
        f"    [C3] Staging {mode} project for {challenge} with image {image} "
        f"({len(shards)} shard job(s), fleet pool ≤{pool_size})..."
    )

    write_project = _write_warm_c3_project if warm_image else _write_c3_project
    with tempfile.TemporaryDirectory(prefix="tig-c3-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        base = root / "base"
        base.mkdir()
        # Stage the shared workspace ONCE; each extra shard is a cheap copytree.
        try:
            if warm_image:
                _create_warm_workspace(base, cfg, server)
            else:
                _create_workspace(base, cfg, server)
        except Exception as exc:
            return None, f"[C3] Failed to create staged project: {exc}"

        if len(shards) == 1:
            try:
                write_project(
                    base, cfg, server, args.c3_time, image,
                    seed=seed, hyperparameters=hyperparameters,
                )
            except Exception as exc:
                return None, f"[C3] Failed to create staged project: {exc}"
            print("    [C3] C3 will upload the staged workspace and pull the Docker Hub image")
            with slot_pool.lease():
                bench, err = _run_one_c3_job_inner(
                    args, env, base, challenge, cfg=cfg)
            if bench is None:
                return None, f"[C3] {err}"
            return bench, ""

        jobs: list[tuple[int, Path, str]] = []
        for idx, shard in enumerate(shards):
            stage = root / f"shard-{idx}"
            try:
                shutil.copytree(base, stage)
                shard_cfg = dict(cfg)
                # A shard may hold slices from several tracks (bin-packed);
                # each distinct track appears at most once per shard, so these
                # dicts never collide. Keep the tracks' `seed` entry — inside
                # the job benchmark.py reads the instance seed from it.
                shard_tracks: dict = {s["track_key"]: s["count"] for s in shard}
                if "seed" in tracks:
                    shard_tracks["seed"] = tracks["seed"]
                shard_cfg["tracks"] = shard_tracks
                shard_cfg["track_starts"] = {s["track_key"]: s["start"] for s in shard}
                # The staged .swarm-cache.json was written from the FULL cfg;
                # rewrite it so this shard's job only runs its own window.
                _write_container_file(
                    stage / ".swarm-cache.json",
                    json.dumps({**shard_cfg, "server_url": server},
                               indent=2, sort_keys=True) + "\n",
                )
                write_project(
                    stage, shard_cfg, server, args.c3_time, image,
                    seed=seed, hyperparameters=hyperparameters,
                )
            except Exception as exc:
                return None, f"[C3] Failed to stage shard {idx}: {exc}"
            # Short console label — the full per-track window mapping
            # (`n_h_edges=20000[14:20]+…`) prefixed EVERY log line and made
            # sharded runs unreadable. It's diagnostic detail: shown once
            # under TIG_C3_VERBOSE=1, and always recoverable from the shard
            # job's own stderr ("[BENCH] shard window starts: …").
            label = f"shard {idx + 1}/{len(shards)}"
            if os.environ.get("TIG_C3_VERBOSE"):
                windows = "+".join(
                    f"{s['track_key']}[{s['start']}:{s['start'] + s['count']}]"
                    for s in shard
                )
                print(f"    [C3] {label}: {windows}")
            jobs.append((idx, stage, label))

        results: list[dict | None] = [None] * len(jobs)
        errs: list[str] = [""] * len(jobs)
        # Submit every shard at once; the fleet-wide FCFS pool — not this
        # executor — is the real concurrency cap, so a thread per shard just
        # parks on the pool until a slot frees.
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {
                executor.submit(_run_one_c3_shard, args, env, stage, label,
                                slot_pool, cfg): idx
                for idx, stage, label in jobs
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    bench, err = future.result()
                except Exception as exc:  # defensive: never let a thread crash silently
                    bench, err = None, f"shard raised {exc!r}"
                results[idx], errs[idx] = bench, err

        # Strict all-or-nothing (mirrors the single-job path): any failed shard
        # means missing instances, so fail rather than score on a partial set.
        failed = [(i, jobs[i][2], errs[i]) for i in range(len(jobs))
                  if results[i] is None]
        if failed:
            # Shards compile the same code, so they usually fail with the
            # IDENTICAL error. Group by the error body (everything after the
            # per-shard "job <id> <status>" head) and show each distinct
            # error ONCE — the old per-shard dump printed the same cargo
            # errors N times per benchmark.
            groups: dict[str, list[str]] = {}
            heads: dict[str, str] = {}
            for idx, _label, err in failed:
                head, _, body = err.partition("\n")
                key = body.strip()
                groups.setdefault(key, []).append(str(idx + 1))
                heads.setdefault(key, head)
            parts = []
            for body, shard_ids in groups.items():
                where = f"shard(s) {','.join(shard_ids)}: {heads[body]}"
                parts.append(f"  {where}\n{body}" if body else f"  {where}")
            distinct = (f", {len(groups)} distinct error(s)"
                        if len(failed) > 1 else "")
            return None, (
                f"[C3] {len(failed)}/{len(jobs)} shard job(s) failed"
                f"{distinct}:\n" + "\n".join(parts)
            )

        return _merge_shard_benchmarks(results, challenge), ""


