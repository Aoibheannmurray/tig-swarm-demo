#!/usr/bin/env python3
"""Run the active challenge's benchmark and emit JSON for publish.py.

Reads swarm-wide config from the local `.swarm-cache.json` snapshot
(written by `setup.py sync`). `setup.py sync` is the *only* moment a
host-side challenge switch is picked up — deliberately, so that an
in-flight edit→benchmark→publish iteration can finish on the challenge it
started on. After loading local config, an advisory probe of
`/api/swarm_config` warns if the host has rotated since the last sync; set
`TIG_NO_SERVER_PROBE=1` to skip the probe.

Benchmarking runs through the **TIG docker** backend (`run_tig_benchmark`):
the agent's algorithm is injected into a prebuilt, fuel-instrumented image,
compiled with `build_algorithm`, and scored per track by
`modified_test_algorithm` (real `tig-runtime` / `tig-verifier`). Bounding is
by **fuel** (`max_fuel_budget`), not wall-clock — deterministic and
hardware-independent. `_tig_adapter` reshapes the driver's combined JSON into
the `benchmark.json` below. (The former custom wall-clock generator/solver/
evaluator path — on-disk `datasets/` cache + per-instance timeout — has been
retired; fuel is the only bound.)

# Scoring

`tig-verifier` emits an absolute *quality* per nonce (higher is better).
Aggregation (in `_tig_adapter`):

    1. Per-track score = MEDIAN of the per-nonce qualities. Infeasible
       nonces sit at the infeasible floor (`-QUALITY_CLAMP`), so an
       infeasible run can never outscore a feasible one.
    2. Cross-track score = shifted geometric mean across the per-track
       medians (`_shifted_geomean`). The shift keeps every value strictly
       positive so the geometric mean is well-defined for any mix of
       negative and positive track scores — rewarding balanced performance.

Output JSON shape:

    {
      "challenge": "...",
      "score": 1234567.8,           # cross-track shifted geo mean of quality
      "feasible": true,
      "instances_solved": 25,
      "instances_feasible": 25,
      "instances_infeasible": 0,
      "track_scores": {"track_key": <median quality>, ...},
      "viz_data": null,             # per-challenge viz not reconstructed on the TIG path yet
    }
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
import math
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent

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

from swarm_client import resolve_server_url

# required=False: benchmark.py can run fully offline (the server probe is
# advisory), so "" is a valid resolution rather than a fatal error.
SERVER = resolve_server_url("benchmark.py", required=False)


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






# ── Per-instance run ───────────────────────────────────────────────






# ── Docker re-exec ────────────────────────────────────────────────


















# ── TIG-docker benchmark path (Option B) ─────────────────────────
#
# Runs the agent's algorithm through the real TIG toolchain (fuel-instrumented
# compile + tig-runtime/tig-verifier) inside the custom image, instead of the
# swarm's own solver/evaluator. Selected by config `benchmark_backend: "tig"`
# (or env TIG_BENCH_BACKEND=tig). See docs/tig_docker_plan.md.

DEFAULT_MAX_FUEL_BUDGET = 5_000_000_000_000  # per-challenge max_fuel_budget (5e12)


def _tig_backend(cfg: dict) -> bool:
    if os.environ.get("TIG_BENCH_BACKEND") == "tig":
        return True
    return cfg.get("benchmark_backend") == "tig" or bool(cfg.get("tig_native"))


def _tig_version() -> str:
    try:
        return json.loads((ROOT_DIR / "tig_pin.json").read_text())["tig_version"]
    except Exception:
        return "0.0.6"


def _tig_image(cfg: dict) -> str:
    return f"tig-custom-image-{cfg['challenge']}:{_tig_version()}"


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


def _ensure_tig_image(image: str, challenge: str) -> None:
    """Ensure the custom TIG image exists locally, building it if missing.
    (C3 pulls the image from the registry instead — handled in the C3 path.)"""
    if subprocess.run(["docker", "image", "inspect", image],
                      capture_output=True).returncode == 0:
        return
    print(f"TIG image '{image}' not found — building via build_bench_image.sh…",
          file=sys.stderr)
    build = subprocess.run(
        ["bash", str(ROOT_DIR / "scripts" / "build_bench_image.sh"), challenge],
    )
    if build.returncode != 0:
        print(f"error: TIG image build failed (exit {build.returncode}).",
              file=sys.stderr)
        sys.exit(build.returncode)


def _tig_adapter(combined: dict, cfg: dict) -> dict:
    """Reshape the driver's combined per-track JSON into benchmark.json.

    Scoring policy (v1): per-track score = MEDIAN of per-nonce quality, with
    infeasible nonces counted at the infeasible floor; overall = shifted
    geometric mean across track medians (same combiner as the custom path).
    """
    challenge = combined.get("challenge", cfg["challenge"])
    track_scores: dict[str, float] = {}
    total = feasible_total = infeasible_total = 0
    errors: list[str] = []

    for track_key, payload in (combined.get("tracks") or {}).items():
        if "error" in payload:
            errors.append(f"{track_key}: {payload['error']}")
            continue
        recs = payload.get("nonces") or []
        if not recs:
            continue
        qualities: list[float] = []
        for r in recs:
            total += 1
            if r.get("feasible"):
                feasible_total += 1
                q = r.get("quality")
                if q is None:
                    qualities.append(float(INFEASIBLE_QUALITY))
                else:
                    # The real TIG toolchain reports raw (unclamped) quality;
                    # clamp to the same ±QUALITY_CLAMP band the custom path's
                    # vendored evaluators enforce, so a very negative quality
                    # can't push the shifted geomean below its log() domain.
                    qualities.append(
                        max(-float(QUALITY_CLAMP), min(float(QUALITY_CLAMP), float(q)))
                    )
            else:
                infeasible_total += 1
                qualities.append(float(INFEASIBLE_QUALITY))
        track_scores[track_key] = statistics.median(qualities)

    overall = _shifted_geomean(list(track_scores.values())) if track_scores else 0.0
    return {
        "challenge": challenge,
        "score": overall,
        "feasible": infeasible_total == 0 and feasible_total > 0,
        "instances_solved": total,
        "instances_feasible": feasible_total,
        "instances_infeasible": infeasible_total,
        "track_scores": track_scores,
        "viz_data": None,  # v1: TIG path doesn't emit per-solution viz yet
        "errors": errors or None,
    }


def run_tig_benchmark(cfg: dict) -> int:
    """Host-side TIG benchmark: build+run the agent's algorithm in the custom
    image and print benchmark.json. Does NOT bind-mount the worktree over /app
    (the image already contains the pinned TIG source)."""
    challenge = cfg["challenge"]
    image = _tig_image(cfg)
    _ensure_docker_daemon()
    _ensure_tig_image(image, challenge)

    algo_path = ROOT_DIR / cfg.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs")
    if not algo_path.exists():
        print(f"error: algorithm file not found at {algo_path}", file=sys.stderr)
        return 2

    tracks = cfg.get("tracks") or {}
    seed = os.environ.get("TIG_BENCH_SEED") or str(tracks.get("seed", "test"))
    fuel = int(cfg.get("max_fuel_budget", DEFAULT_MAX_FUEL_BUDGET))
    hp = os.environ.get("TIG_HYPERPARAMETERS") or "null"
    driver = ROOT_DIR / "scripts" / "tig_bench_driver.py"
    # Mount the algorithm DIRECTORY over the slot so GPU challenges' kernels
    # (`*.cu`, required by build_ptx) ride along with mod.rs. CPU algorithm dirs
    # contain only mod.rs, so this is equivalent there.
    algo_dir = algo_path.parent
    slot_dir = f"/app/tig-algorithms/src/{challenge}/swarm_algo"
    track_counts = {k: v for k, v in tracks.items() if k != "seed"}

    gpu_flags = ["--gpus", "all"] if is_gpu_challenge(cfg) else []
    cmd = [
        "docker", "run", "--rm", *gpu_flags,
        "-v", f"{algo_dir}:{slot_dir}:ro",
        "-v", f"{driver}:/usr/local/bin/tig_bench_driver.py:ro",
        "-e", f"TIG_TRACKS={json.dumps(track_counts)}",
        "-e", f"TIG_SEED={seed}",
        "-e", f"TIG_FUEL={fuel}",
        "-e", f"TIG_HYPERPARAMETERS={hp}",
        image, "python3", "/usr/local/bin/tig_bench_driver.py",
    ]
    print(f"Benchmarking {challenge} via TIG docker ({image})…", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stderr:
        print(proc.stderr[-4000:], file=sys.stderr)
    if proc.returncode != 0:
        print(f"error: TIG benchmark container exited {proc.returncode}", file=sys.stderr)
        return proc.returncode
    # The driver prints exactly one JSON object on stdout (take last non-empty line).
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.strip()), "")
    try:
        combined = json.loads(line)
    except json.JSONDecodeError:
        print(f"error: driver output not JSON:\n{proc.stdout[-500:]}", file=sys.stderr)
        return 1
    out = _tig_adapter(combined, cfg)
    print(json.dumps(out, indent=2))
    return 0

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


def _is_per_track_hyperparameters(parsed: object) -> bool:
    """True if `parsed` is a per-track map {track_key: {param: value}} rather than
    a flat {param: value} config.

    Disambiguation is by value type: a flat config's values are scalars
    (int/float/str/bool — see hpo.sample_config), while a per-track map's values
    are all dicts. An empty dict is treated as flat (the default config).

    CANONICAL COPY — `hp_for`/`_is_per_track` in scripts/tig_bench_driver.py
    mirror this logic and must be kept in sync (the driver runs inside the
    benchmark image with nothing staged next to it, so it can't import us).
    """
    return (
        isinstance(parsed, dict)
        and len(parsed) > 0
        and all(isinstance(v, dict) for v in parsed.values())
    )


def _track_hyperparameters(raw: str | None, track_key: str) -> str | None:
    """Resolve the --hyperparameters JSON string to pass for one track.

    `raw` (the TIG_HYPERPARAMETERS value) is either a flat config applied to
    every track, or a per-track map {track_key: config}. The hyperparameter
    search runs flat configs uniformly; the final tuned-score benchmark passes a
    per-track map (a winner per track — see docs/hyperparameter-search-plan.md).
    For a per-track map this selects the track's own config (a missing track =>
    the default config {}). Flat configs and unparseable strings pass through.

    CANONICAL COPY — keep `hp_for` in scripts/tig_bench_driver.py in sync
    (it can't import us; it runs alone inside the benchmark image).
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw  # let the solver surface the parse error
    if _is_per_track_hyperparameters(parsed):
        return json.dumps(parsed.get(track_key, {}))
    return raw


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

    # Run identity — a stable reference to correlate a problematic run against
    # the user's logs. The benchmark id is fresh per run.
    print(f"User ID: {_resolve_user_id()}", file=sys.stderr)
    print(f"Benchmark ID: {uuid.uuid4().hex[:10]}", file=sys.stderr)

    # The fuel-instrumented TIG-docker backend is the ONLY benchmarking path.
    # The swarm's custom wall-clock generator/solver/evaluator path (on-disk
    # datasets + per-instance timeout) has been retired — fuel is the only bound.
    return run_tig_benchmark(cfg)


if __name__ == "__main__":
    sys.exit(main())
