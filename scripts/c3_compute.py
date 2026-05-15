"""C3 cloud compute integration for remote benchmarking.

Handles project bundling, .c3 config generation, job submission,
polling, and result extraction.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import time
import uuid

from challenge_files import ROOT, read_optional

_POLL_INTERVAL_SECS = 15


# ── Helpers ────────────────────────────────────────────────────────


def _yaml_quote(value: str) -> str:
    # JSON strings are a subset of YAML strings — json.dumps gives us correct
    # escaping for quotes/backslashes/newlines without pulling in a YAML lib.
    return json.dumps(value)


def _parse_walltime(c3_time: str) -> int:
    """Parse 'HH:MM:SS' (or 'MM:SS' or 'SS') to seconds. Returns 7200 on parse failure."""
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


def _script_write_file_b64(path: str, content: str) -> str:
    encoded = base64.b64encode(content.encode()).decode()
    return f"""\
python3 - <<'PY'
import base64
from pathlib import Path

path = Path({path!r})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(base64.b64decode({encoded!r}))
PY
"""


# ── Transient C3 project files ────────────────────────────────────


@contextmanager
def _temporary_c3_files(config: dict, server: str, c3_time: str):
    """Create the transient .c3 project + runner script C3 needs.

    C3 expects `.c3` to live at the project root, so we write it only for
    the duration of `c3 deploy` and restore any pre-existing file exactly.
    """
    run_id = uuid.uuid4().hex[:10]
    challenge = config.get("challenge", "unknown")
    is_gpu = bool(config.get("is_gpu"))
    dockerfile = "./scripts/c3/docker/Dockerfile.gpu" if is_gpu else "./scripts/c3/docker/Dockerfile.cpu"
    c3_path = ROOT / ".c3"
    script_name = f".c3-run-benchmark-{run_id}.sh"
    script_path = ROOT / script_name
    artifacts_dir = ROOT / "c3-artifacts"

    algo_p = ROOT / config.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs")
    kernel_cfg = config.get("kernel_path")
    kernel_p = ROOT / kernel_cfg if kernel_cfg else None

    # Multi-file algorithm support: ship every sibling source file under the
    # algorithm directory (mod.rs + helper *.rs modules) so seeds that
    # reference `mod foo;` etc. compile remotely. Single-file algorithms
    # collapse to just mod.rs as before.
    algo_dir = algo_p.parent
    algo_dir_rel = algo_dir.relative_to(ROOT).as_posix()
    sibling_files: dict[str, str] = {}
    if algo_dir.is_dir():
        for p in sorted(algo_dir.rglob("*.rs")):
            if not p.is_file():
                continue
            rel_to_dir = p.relative_to(algo_dir).as_posix()
            sibling_files[rel_to_dir] = p.read_text()

    old_c3 = c3_path.read_text() if c3_path.exists() else None

    c3_config = f"""\
project: tig-swarm-benchmark
script: {_yaml_quote(script_name)}
gpu: {_yaml_quote(config.get("c3_hardware", "l40"))}
time: {_yaml_quote(c3_time)}
job_name: {_yaml_quote(f"tig-{challenge}-{run_id}")}

docker:
  dockerfile: {dockerfile}
  context: ./scripts/c3/docker

output:
  - ./c3-artifacts
"""

    algorithm_code = read_optional(algo_p)
    kernel_code = read_optional(kernel_p)

    runner = f"""\
#!/bin/bash
set -u

cd "${{C3_JOB_WORKDIR:-/workspace}}"
mkdir -p "${{C3_ARTIFACTS_DIR}}" c3-artifacts

export TIG_IN_DOCKER=1
export TIG_SWARM_SERVER={_yaml_quote(server)}

{_script_write_file_b64(config.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs"), algorithm_code)}
"""
    # Replicate sibling *.rs modules under the same algorithm directory so
    # multi-file seeds compile in the C3 sandbox. mod.rs is already covered
    # by the algorithm_path write above, so skip it to avoid double-write.
    for rel, body in sibling_files.items():
        if rel == "mod.rs":
            continue
        runner += _script_write_file_b64(f"{algo_dir_rel}/{rel}", body)
    if kernel_cfg:
        runner += _script_write_file_b64(kernel_cfg, kernel_code)
    runner += f"""\

cat > swarm.config.json <<'JSON'
{json.dumps(config, indent=2, sort_keys=True)}
JSON

status=0
python3 scripts/benchmark.py \
  > "${{C3_ARTIFACTS_DIR}}/benchmark.json" \
  2> "${{C3_ARTIFACTS_DIR}}/benchmark.stderr" || status=$?

exit "$status"
"""

    try:
        artifacts_dir.mkdir(exist_ok=True)
        c3_path.write_text(c3_config)
        script_path.write_text(runner)
        script_path.chmod(0o755)
        yield
    finally:
        if old_c3 is None:
            try:
                c3_path.unlink()
            except FileNotFoundError:
                pass
        else:
            c3_path.write_text(old_c3)
        try:
            script_path.unlink()
        except FileNotFoundError:
            pass


# ── Job ID parsing ─────────────────────────────────────────────────


def _parse_c3_id(text: str) -> str | None:
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


# ── Job polling ────────────────────────────────────────────────────


_TERMINAL_OK = ("COMPLETED", "SYNCED", "SUCCEEDED", "SUCCESS")
_TERMINAL_BAD = ("FAILED", "CANCELLED", "CANCELED", "ERROR")


def _read_job_status(job_id: str, env: dict) -> str | None:
    """Return uppercased status from `c3 jobs get`, or None if unknown.

    Prefers `-o json` for a clean parse; falls back to scraping the table.
    """
    result = subprocess.run(
        ["c3", "jobs", "get", job_id, "-o", "json"],
        capture_output=True, text=True, cwd=ROOT, env=env,
    )
    raw = (result.stdout or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            status = data.get("status") or data.get("state") or ""
            if status:
                return str(status).upper()
        except json.JSONDecodeError:
            pass
    # Fallback: plain table
    result = subprocess.run(
        ["c3", "jobs", "get", job_id],
        capture_output=True, text=True, cwd=ROOT, env=env,
    )
    out = (result.stdout or "").upper()
    if not out.strip():
        return None
    for marker in _TERMINAL_OK + _TERMINAL_BAD + ("RUNNING", "PENDING", "QUEUED"):
        if marker in out:
            return marker
    return None


def _poll_c3_job(job_id: str, env: dict, walltime_secs: int) -> str:
    """Poll a c3 job until terminal state.

    Deadline is derived from the job's own walltime plus a 50% buffer for
    queueing/teardown (min 30 min). Polls `c3 jobs get` — never trusts a
    transient empty squeue as "completed".
    """
    deadline = time.monotonic() + max(1800, int(walltime_secs * 1.5))
    poll = 0
    while time.monotonic() < deadline:
        status = _read_job_status(job_id, env)
        if status:
            if any(s in status for s in _TERMINAL_OK):
                return "completed"
            if any(s in status for s in _TERMINAL_BAD):
                return "failed"
            if poll % 4 == 0:
                print(f"    [C3] {job_id}: {status}")
        else:
            if poll % 4 == 0:
                print(f"    [C3] Still waiting on {job_id} (poll {poll + 1})…")
        poll += 1
        time.sleep(_POLL_INTERVAL_SECS)
    return "timeout"


# ── Run benchmark on C3 ───────────────────────────────────────────


def run_benchmark_c3(args: argparse.Namespace, config: dict, server: str) -> tuple[dict | None, str]:
    if shutil.which("c3") is None:
        return None, "[C3] c3 CLI not found. Install from https://cthree.cloud/install.sh"

    c3_key = args.c3_api_key or os.environ.get("C3_API_KEY", "")
    challenge = config.get("challenge", "unknown")

    cfg = dict(config)
    cfg["server_url"] = server
    cfg["c3_hardware"] = args.hardware.lower()

    env = os.environ.copy()
    if c3_key and not c3_key.startswith("your_"):
        env["C3_API_KEY"] = c3_key

    print(f"    [C3] Bundling project for {challenge}…")

    with _temporary_c3_files(cfg, server, args.c3_time):
        cmd = ["c3", "deploy"]
        if args.c3_cloud_provider:
            cmd.extend(["-p", args.c3_cloud_provider])
        if args.c3_no_build:
            cmd.append("--no-build")

        print(f"    [C3] Running: {' '.join(cmd)}")
        print(f"    [C3] (streaming deploy output — first run uploads ~10GB Docker image)")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=ROOT, env=env,
        )
        deploy_output_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            deploy_output_lines.append(line)
            print(f"    [C3]   {line}")
        proc.wait()
        result = subprocess.CompletedProcess(
            cmd, proc.returncode,
            stdout="\n".join(deploy_output_lines), stderr="",
        )

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    print(f"    [C3] Deploy output: {combined[:300]}")

    job_id = _parse_c3_id(combined)
    if not job_id:
        err = f"[C3] Could not parse job ID from c3 deploy output:\n{combined[-2000:]}"
        print(f"    {err}")
        return None, err

    walltime_secs = _parse_walltime(args.c3_time)
    print(f"    [C3] Job submitted: {job_id} — polling for completion (walltime {walltime_secs}s)…")
    status = _poll_c3_job(job_id, env, walltime_secs)

    logs_result = subprocess.run(
        ["c3", "logs", job_id],
        capture_output=True, text=True, cwd=ROOT, env=env,
    )
    logs_out = logs_result.stdout or ""

    if status != "completed":
        err = f"[C3] Job {job_id} {status}"
        print(f"    {err}")
        if logs_out:
            print(f"    [C3] Last 500 chars of logs:\n{logs_out[-500:]}")
        return None, f"{err}:\n{logs_out[-4000:]}"

    print(f"    [C3] Job {job_id} completed — pulling results…")

    subprocess.run(
        ["c3", "pull", job_id], capture_output=True, text=True, cwd=ROOT, env=env,
    )

    for artifact_dir in [ROOT / job_id / "artifacts", ROOT / job_id / "c3-artifacts",
                         ROOT / "c3-artifacts"]:
        artifact_json = artifact_dir / "benchmark.json"
        if artifact_json.exists() and artifact_json.stat().st_size > 0:
            try:
                bench = json.loads(artifact_json.read_text())
                print(f"    [C3] Results extracted from {artifact_json}")
                return bench, ""
            except json.JSONDecodeError:
                continue

    for line in reversed(logs_out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                bench = json.loads(line)
                if "score" in bench or "challenge" in bench:
                    print(f"    [C3] Results extracted from logs")
                    return bench, ""
            except json.JSONDecodeError:
                continue

    json_blocks = re.findall(r'\{[^{}]*"score"[^{}]*\}', logs_out)
    if json_blocks:
        try:
            bench = json.loads(json_blocks[-1])
            print(f"    [C3] Results extracted via regex fallback")
            return bench, ""
        except json.JSONDecodeError:
            pass

    err = f"[C3] Job {job_id} completed but could not parse benchmark JSON"
    print(f"    {err}")
    if logs_out:
        print(f"    [C3] Last 500 chars of logs:\n{logs_out[-500:]}")
    return None, err
