"""C3 cloud compute integration for remote benchmarking.

The current C3 architecture runs a project described by a root `.c3` file.
For Docker jobs C3 pulls a public Docker Hub image, uploads the project
workspace, runs the configured bash script inside the container, then returns
files written to `$C3_ARTIFACTS_DIR` or configured `output:` directories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable

from challenge_files import ROOT, read_optional

_POLL_INTERVAL_SECS = 15
_DEFAULT_CPU_IMAGE = "rust:1-bookworm"
_DEFAULT_GPU_IMAGE = "nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04"


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
    if bool(config.get("is_gpu")):
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


def _create_workspace(stage: Path, config: dict, server: str) -> dict:
    """Create the minimal TIG workspace C3 should upload."""
    challenge = config.get("challenge", "unknown")
    staged_config = dict(config)
    staged_config["server_url"] = server

    for name in ("Cargo.toml", "Cargo.lock", "requirements.txt"):
        _copy_required(ROOT / name, stage / name)

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
) -> str:
    run_id = uuid.uuid4().hex[:10]
    challenge = config.get("challenge", "unknown")
    script_name = f"run-benchmark-{run_id}.sh"

    c3_config = f"""\
project: tig-swarm-benchmark
script: {_yaml_quote(script_name)}
gpu: {_yaml_quote(config.get("c3_hardware", "l40"))}
time: {_yaml_quote(c3_time)}
job_name: {_yaml_quote(f"tig-{challenge}-{run_id}")}

docker:
  image: {_yaml_quote(image)}

output:
  - ./c3-artifacts
"""
    _write_container_file(stage / ".c3", c3_config)

    gpu_check = ""
    if bool(config.get("is_gpu")):
        gpu_check = """
if ! command -v nvcc >/dev/null 2>&1; then
  echo "nvcc is required for GPU challenges; use a CUDA devel Docker image" >&2
  exit 127
fi
"""

    runner = f"""\
#!/bin/bash
set -euo pipefail

cd "${{C3_JOB_WORKDIR:-/workspace}}"
mkdir -p "${{C3_ARTIFACTS_DIR}}" c3-artifacts

export TIG_IN_DOCKER=1
export TIG_SWARM_SERVER={_yaml_quote(server)}
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


def _read_job_status(job_id: str, env: dict, cwd: Path) -> str | None:
    result = subprocess.run(
        ["c3", "squeue", "--json"],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    raw = (result.stdout or "").strip()
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

    table = subprocess.run(
        ["c3", "squeue", "-n", "50"],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    for line in (table.stdout or "").splitlines():
        if job_id not in line:
            continue
        upper = line.upper()
        for marker in _TERMINAL_OK + _TERMINAL_BAD + _NON_TERMINAL:
            if marker in upper:
                return marker
    return None


def _poll_c3_job(job_id: str, env: dict, cwd: Path, walltime_secs: int) -> str:
    """Poll a C3 job until it reaches a terminal state."""
    deadline = time.monotonic() + max(1800, walltime_secs + 2700)
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


def _read_logs(job_id: str, env: dict, cwd: Path) -> str:
    result = subprocess.run(
        ["c3", "logs", job_id],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    return (result.stdout or "") + (result.stderr or "")


def _check_c3_auth(env: dict) -> str:
    result = subprocess.run(
        ["c3", "whoami"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
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
    result = subprocess.run(
        ["c3", "pull", job_id],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    if result.returncode == 0:
        return (result.stdout or "") + (result.stderr or "")

    fallback = subprocess.run(
        ["c3", "squeue", "pull", job_id],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
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


def run_benchmark_c3(args: argparse.Namespace, config: dict, server: str) -> tuple[dict | None, str]:
    if shutil.which("c3") is None:
        return None, "[C3] c3 CLI not found. Install from https://cthree.cloud/install.sh"

    c3_key = args.c3_api_key or os.environ.get("C3_API_KEY", "")
    challenge = config.get("challenge", "unknown")

    cfg = dict(config)
    cfg["server_url"] = server
    cfg["c3_hardware"] = args.hardware.lower()

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

    print(f"    [C3] Staging project for {challenge} with image {image}...")

    with tempfile.TemporaryDirectory(prefix="tig-c3-") as tmp:
        stage = Path(tmp)
        try:
            _create_workspace(stage, cfg, server)
            _write_c3_project(stage, cfg, server, args.c3_time, image)
        except Exception as exc:
            return None, f"[C3] Failed to create staged project: {exc}"

        cmd = ["c3", "deploy"]
        provider = _arg_value(args, "c3_provider")
        if provider:
            cmd.extend(["-p", provider])

        print(f"    [C3] Running: {' '.join(cmd)}")
        print("    [C3] C3 will upload the staged workspace and pull the Docker Hub image")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=stage,
            env=env,
        )
        deploy_output_lines = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            deploy_output_lines.append(line)
            print(f"    [C3]   {line}")
        proc.wait()
        combined = "\n".join(deploy_output_lines)

        if proc.returncode != 0:
            err = f"[C3] c3 deploy failed ({proc.returncode}):\n{combined[-4000:]}"
            print(f"    {err}")
            return None, err

        job_id = _parse_c3_id(combined)
        if not job_id:
            err = f"[C3] Could not parse job ID from c3 deploy output:\n{combined[-2000:]}"
            print(f"    {err}")
            return None, err

        walltime_secs = _parse_walltime(args.c3_time)
        print(f"    [C3] Job submitted: {job_id} — polling for completion...")
        status = _poll_c3_job(job_id, env, stage, walltime_secs)
        logs_out = _read_logs(job_id, env, stage)

        if status != "completed":
            err = f"[C3] Job {job_id} {status}"
            print(f"    {err}")
            # Pull artifacts first: benchmark.py's real failure (cargo/rustc
            # errors) lands in benchmark.stderr, not the job's stdout, which
            # only carries runner noise (e.g. the rustup install banner).
            pull_output = _pull_artifacts(job_id, env, stage)
            bench_stderr = _read_benchmark_stderr(stage)
            if bench_stderr:
                print(f"    [C3] Last 4000 chars of benchmark.stderr:\n{bench_stderr[-4000:]}")
            elif logs_out:
                print(f"    [C3] Last 4000 chars of job logs:\n{logs_out[-4000:]}")
            _, parse_err = _load_benchmark_json(stage)
            details = "\n".join(
                part for part in (parse_err, pull_output[-2000:], logs_out[-2000:]) if part
            )
            return None, f"{err}\n{details}"

        print(f"    [C3] Job {job_id} completed — pulling artifacts...")
        pull_output = _pull_artifacts(job_id, env, stage)
        bench, parse_err = _load_benchmark_json(stage)
        if bench is not None:
            # benchmark.py writes the per-nonce log + timing summary to
            # stderr (stdout is reserved for the benchmark.json payload). On
            # failure we already surface stderr below; echo it on success too
            # so timing is visible on a practice bench, not just on a crash.
            bench_stderr = _read_benchmark_stderr(stage)
            if bench_stderr:
                print(f"    [C3] benchmark.stderr:\n{bench_stderr}")
            return bench, ""

        err = f"[C3] Job {job_id} completed but benchmark.json was not found or parseable"
        details = "\n".join(part for part in (parse_err, pull_output[-2000:], logs_out[-2000:]) if part)
        print(f"    {err}")
        return None, f"{err}\n{details}"
