#!/usr/bin/env python3
"""Standalone swarm optimization loop — no agent required.

Handles server communication, prompt construction, LLM-based code mutation,
benchmarking, and result publishing.  Works with any LLM provider (Anthropic,
OpenAI, Google) or any OpenAI-compatible endpoint via --api-base.

Usage:
    python scripts/run_loop.py --provider anthropic
    python scripts/run_loop.py --provider openai --model gpt-4o
    python scripts/run_loop.py --provider google --model gemini-2.5-pro
    python scripts/run_loop.py --provider openai --api-base https://api.together.xyz
    python scripts/run_loop.py --provider anthropic --compute c3 --hardware l40

    # Resume a previous agent
    python scripts/run_loop.py --provider anthropic --agent-id <id> --agent-name <name>

API keys are read from the environment: ANTHROPIC_API_KEY, OPENAI_API_KEY,
GOOGLE_API_KEY (or pass --api-key directly). C3 compute can use C3_API_KEY,
--c3-api-key, or existing `c3 login` credentials.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from llm_backends import DEFAULT_MODELS, call_llm

# ── Config & server helpers ─────────────────────────────────────────


def load_config() -> dict:
    cfg_path = ROOT / "swarm.config.json"
    if not cfg_path.exists():
        sys.exit("swarm.config.json not found. Run `python setup.py join <url>` first.")
    return json.loads(cfg_path.read_text())


def server_post(url: str, payload: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def server_get(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def register_agent(server: str) -> tuple[str, str]:
    data = server_post(f"{server}/api/agents/register", {"client_version": "1.0"})
    return data["agent_id"], data["agent_name"]


def get_state(server: str, agent_id: str) -> dict:
    return server_get(f"{server}/api/state?agent_id={agent_id}")


def send_heartbeat(server: str, agent_id: str) -> None:
    try:
        server_post(f"{server}/api/agents/{agent_id}/heartbeat", {"status": "working"}, timeout=5)
    except Exception:
        pass


def post_message(server: str, agent_name: str, agent_id: str, content: str) -> None:
    try:
        server_post(f"{server}/api/messages", {
            "agent_name": agent_name, "agent_id": agent_id,
            "content": content, "msg_type": "agent",
        }, timeout=5)
    except Exception:
        pass


# ── File I/O ────────────────────────────────────────────────────────


def is_stub_code(code: str) -> bool:
    """True when the algorithm is a placeholder that can't produce solutions."""
    if not code or not code.strip():
        return True
    return "unimplemented!" in code or "todo!" in code


def algo_path(config: dict) -> Path:
    return ROOT / config.get("algorithm_path", "src/knapsack/algorithm/mod.rs")


def kernel_path(config: dict) -> Path | None:
    kp = config.get("kernel_path")
    return ROOT / kp if kp else None


def write_algorithm(code: str, config: dict) -> None:
    p = algo_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code)


def write_kernel(code: str, config: dict) -> None:
    p = kernel_path(config)
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code)


def read_algorithm(config: dict) -> str:
    p = algo_path(config)
    return p.read_text() if p.exists() else ""


def read_kernel(config: dict) -> str:
    p = kernel_path(config)
    if p and p.exists():
        return p.read_text()
    return ""


def read_challenge_md() -> str:
    p = ROOT / "CHALLENGE.md"
    return p.read_text() if p.exists() else ""


def read_tacit_knowledge() -> str:
    p = ROOT / "tacit_knowledge_personal.md"
    return p.read_text() if p.exists() else ""


# ── Prompt construction ─────────────────────────────────────────────


DEFAULT_STRATEGY_TAGS = [
    "construction", "local_search", "metaheuristic",
    "constraint_relaxation", "decomposition", "hybrid",
    "data_structure", "other",
]


def get_strategy_tags(config: dict) -> list[str]:
    """Resolve strategy tags from swarm config, falling back to defaults."""
    challenge = config.get("challenge", "")
    available = config.get("available_challenges") or {}
    sub = available.get(challenge) or {}
    tags = sub.get("strategy_tags") or []
    return tags if tags else DEFAULT_STRATEGY_TAGS


def build_hypothesis_system_prompt(
    challenge_md: str, config: dict, *, is_bootstrap: bool = False,
) -> str:
    challenge = config.get("challenge", "unknown")
    tags = ", ".join(get_strategy_tags(config))
    if is_bootstrap:
        job = (
            "propose an initial algorithm strategy. The current code is a "
            "stub — you need a complete working approach, not a tweak."
        )
    else:
        job = "propose ONE specific change to try."
    return f"""\
You are planning an improvement to a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

Your job: {job} Do NOT write code — just describe the idea.

Respond in EXACTLY this format (4 lines, nothing else):

TITLE: <short title of what to change, under 80 chars>
DESCRIPTION: <2-3 sentence description of the change and reasoning>
STRATEGY_TAG: <one of: {tags}>
NOTES: <brief interpretation of your approach>"""


def build_hypothesis_user_prompt(state: dict, config: dict | None = None) -> str:
    parts: list[str] = []
    is_gpu = bool((config or {}).get("is_gpu"))

    code = state.get("best_algorithm_code") or ""
    if is_stub_code(code):
        parts.append(
            "No working algorithm yet — the current code is a stub. "
            "Propose an initial implementation strategy from scratch."
        )
    else:
        parts.append(f"Current algorithm (mod.rs):\n```rust\n{code}\n```")
        if is_gpu:
            kernel_code = state.get("best_kernel_code") or ""
            if kernel_code:
                parts.append(f"\nCurrent CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```")

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = [f"\n{len(prior)} strategies already tried on this code — try something STRUCTURALLY DIFFERENT:"]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            lines.append(f"  - [{tag}] {title}")
        parts.append("\n".join(lines))

    hint = state.get("stagnation_hint")
    if hint == "inspiration":
        insp = state.get("inspiration_code", "")
        if insp:
            parts.append(
                f"\nStudy this approach for ideas "
                f"(adapt ideas, do NOT copy wholesale):\n```rust\n{insp}\n```"
            )
            if is_gpu:
                insp_kernel = state.get("inspiration_kernel_code", "")
                if insp_kernel:
                    parts.append(f"\nInspiration CUDA kernels:\n```cuda\n{insp_kernel}\n```")
    elif hint == "tacit_knowledge":
        tk = read_tacit_knowledge().strip()
        if tk:
            parts.append(f"\nPersonal strategy hints:\n{tk}")
        else:
            insp = state.get("inspiration_code", "")
            if insp:
                parts.append(
                    f"\nStudy this approach for ideas:\n```rust\n{insp}\n```"
                )
                if is_gpu:
                    insp_kernel = state.get("inspiration_kernel_code", "")
                    if insp_kernel:
                        parts.append(f"\nInspiration CUDA kernels:\n```cuda\n{insp_kernel}\n```")

    reset = state.get("trajectory_reset")
    if reset:
        rtype = reset.get("type", "")
        if rtype == "adopted_inactive":
            parts.append(
                "\nYou are starting from another agent's previous algorithm. "
                "Study the code above and propose an improvement to build on it."
            )
        else:
            parts.append(
                "\nYou are starting from the template algorithm. "
                "Propose an initial strategy to improve it."
            )

    parts.append("\nPropose one specific improvement to try.")
    return "\n".join(parts)


def build_code_system_prompt(challenge_md: str, config: dict) -> str:
    challenge = config.get("challenge", "unknown")
    is_gpu = bool(config.get("is_gpu"))
    if is_gpu:
        return f"""\
You are optimizing a Rust+CUDA algorithm for the "{challenge}" GPU challenge.

{challenge_md}

IMPORTANT RULES:
- `use super::*;` must remain as the first import in the Rust file.
- `fn solve_challenge(` MUST appear in your Rust output — do NOT omit it.
- Return BOTH files: the complete Rust source AND the complete CUDA kernel source.
- Separate them with a line containing exactly: // --- kernels.cu ---
- The Rust file comes FIRST, then the separator, then the CUDA file.
- No explanation, no markdown fences — just the two raw source files with the separator.
- Kernel function names in mod.rs (module.get_function("...")) must match the extern "C" __global__ function names in kernels.cu."""
    return f"""\
You are optimizing a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

`use super::*;` must remain as the first import. Return the complete Rust source file — no explanation, no markdown fences."""


def build_code_user_prompt(state: dict, hypothesis: dict, config: dict | None = None) -> str:
    parts: list[str] = []
    is_gpu = bool((config or {}).get("is_gpu"))

    code = state.get("best_algorithm_code") or ""
    if is_stub_code(code):
        parts.append(
            "No working algorithm yet — write a complete solve_challenge "
            "implementation from scratch. Call save_solution() whenever you "
            "find an improved solution."
        )
    else:
        parts.append(f"Current algorithm (mod.rs):\n```rust\n{code}\n```")

    if is_gpu:
        kernel_code = state.get("best_kernel_code") or ""
        if kernel_code:
            parts.append(f"\nCurrent CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```")
        else:
            parts.append(
                "\nNo CUDA kernel file yet — write any custom GPU kernels you need."
            )
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nThe Rust file (with fn solve_challenge) comes first, then the separator, then the CUDA file."
        )

    title = hypothesis.get("title", "")
    description = hypothesis.get("description", "")
    if is_stub_code(code):
        parts.append(f"\nImplement this strategy:\n{title}\n{description}")
    else:
        parts.append(f"\nApply this change:\n{title}\n{description}")

    return "\n".join(parts)


def build_runtime_fix_prompt(code: str, bench: dict, kernel_code: str = "") -> str:
    """Build a prompt that feeds runtime errors back to the LLM for fixing."""
    errors = bench.get("errors") or []
    error_lines = "\n".join(f"  - {e}" for e in errors)
    score = bench.get("score", 0)
    feasible = bench.get("feasible", False)
    track_scores = bench.get("track_scores", {})
    track_summary = "\n".join(
        f"  - {track}: {s:.0f}" for track, s in track_scores.items()
    )
    parts = [f"Current algorithm (mod.rs):\n```rust\n{code}\n```\n"]
    if kernel_code:
        parts.append(f"Current CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```\n")
    parts.append(
        f"This code compiled successfully but failed at runtime.\n\n"
        f"Score: {score}  Feasible: {feasible}\n"
        f"Per-track scores:\n{track_summary}\n"
        f"Errors:\n{error_lines}\n\n"
        "How to interpret the errors:\n"
        "- 'no solution saved' = the code crashed, panicked, or returned Err() "
        "before ever calling save_solution(). Fix: save_solution() MUST be "
        "called before any fallible operation. Build a partial solution and "
        "save it first, then try to improve.\n"
        "- Any other error = the code saved a solution but the evaluator "
        "rejected it (constraint violation). Fix: check that your solution "
        "satisfies all feasibility constraints described in the challenge.\n\n"
        "Fix the runtime errors and return the complete source."
    )
    if kernel_code:
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nEnsure kernel function names match between mod.rs and kernels.cu."
        )
    return "\n".join(parts)


def build_redescribe_hypothesis_prompt(
    original_code: str, final_code: str, original_hypothesis: dict,
) -> str:
    """Ask the LLM to re-describe what the final code actually does,
    since error recovery may have changed it from the original plan."""
    orig_title = original_hypothesis.get("title", "")
    orig_desc = original_hypothesis.get("description", "")
    orig_tag = original_hypothesis.get("strategy_tag", "other")
    return (
        f"The original hypothesis was:\n"
        f"  TITLE: {orig_title}\n"
        f"  DESCRIPTION: {orig_desc}\n"
        f"  STRATEGY_TAG: {orig_tag}\n\n"
        f"Original code (before):\n```rust\n{original_code}\n```\n\n"
        f"Final code (after fixing runtime errors):\n```rust\n{final_code}\n```\n\n"
        "The code was modified to fix runtime errors. Compare the original "
        "hypothesis against the final code. If the error recovery changed the "
        "core approach (e.g. replaced the construction heuristic, added a "
        "fundamentally different fallback strategy, restructured the solver), "
        "update the TITLE, DESCRIPTION, and STRATEGY_TAG to accurately reflect "
        "what the final code actually does. If the fixes were minor (e.g. "
        "bounds checks, error handling wrappers) and the core approach is "
        "unchanged, keep the original hypothesis as-is.\n\n"
        "Respond with the corrected hypothesis."
    )


# ── Response parsing ────────────────────────────────────────────────


_META_DEFAULTS = {
    "title": "LLM mutation",
    "description": "Automated code improvement",
    "strategy_tag": "other",
    "notes": "",
}


def parse_hypothesis(text: str) -> dict:
    """Extract metadata fields from the hypothesis LLM response."""
    meta = dict(_META_DEFAULTS)
    for line in text.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if key in meta and value:
                meta[key] = value
    return meta


def _strip_fences(text: str) -> str:
    """Remove optional markdown fences from a code block."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


_KERNEL_SEPARATOR = "// --- kernels.cu ---"


def parse_code(text: str) -> str:
    """Extract Rust code from the code LLM response (non-GPU)."""
    return _strip_fences(text)


def parse_gpu_code(text: str) -> tuple[str, str]:
    """Extract Rust + CUDA code from a GPU two-file LLM response.

    Returns (rust_code, cuda_code). If the separator is missing,
    returns the whole text as rust_code and empty cuda_code.
    """
    text = _strip_fences(text)
    sep_idx = text.find(_KERNEL_SEPARATOR)
    if sep_idx == -1:
        # Try alternate separators the LLM might use
        for alt in ["// --- kernels.cu", "// ---kernels.cu---", "// -- kernels.cu --",
                     "/* --- kernels.cu --- */", "// ===== kernels.cu =====",
                     "// kernels.cu"]:
            idx = text.find(alt)
            if idx != -1:
                sep_idx = idx
                # Find end of separator line
                nl = text.find("\n", sep_idx)
                rust = text[:sep_idx].strip()
                cuda = text[nl + 1:].strip() if nl != -1 else ""
                return _strip_fences(rust), _strip_fences(cuda)
        return text.strip(), ""
    rust = text[:sep_idx].strip()
    cuda = text[sep_idx + len(_KERNEL_SEPARATOR):].strip()
    return _strip_fences(rust), _strip_fences(cuda)


def validate_code(original: str, modified: str) -> str | None:
    """Basic sanity check on LLM-generated code.

    Returns None if valid, or an error description."""
    if "use super::*;" not in modified:
        return "`use super::*;` is missing — it must remain as the first import."
    if "fn solve_challenge(" not in modified:
        return "`fn solve_challenge(` not found — the function signature must not change."
    if "unimplemented!" in modified or "todo!" in modified:
        return (
            "Code still contains `unimplemented!()` or `todo!()` — "
            "you must provide a complete working implementation."
        )
    return None


# ── Benchmark & publish ─────────────────────────────────────────────


def _run_benchmark_local() -> tuple[dict | None, str]:
    """Run benchmark. Returns (result_dict, error_text).

    On success, result_dict is the parsed JSON and error_text is empty.
    On failure, result_dict is None and error_text contains the stderr."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0:
        err = result.stderr[-2000:]
        print(f"  Benchmark failed:\n{err}", file=sys.stderr)
        return None, err
    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:
        print(f"  Benchmark output not valid JSON:\n{result.stdout[:300]}", file=sys.stderr)
        return None, "Benchmark output was not valid JSON"


def _yaml_quote(value: str) -> str:
    return json.dumps(value)


def _read_optional(path: Path | None) -> str:
    if path and path.exists():
        return path.read_text()
    return ""


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


@contextmanager
def _temporary_c3_files(config: dict, server: str, c3_time: str):
    """Create the transient .c3 project + runner script C3 needs.

    C3 expects `.c3` to live at the project root, so we write it only for
    the duration of `c3 deploy` and restore any pre-existing file exactly.
    The runner script embeds the current candidate code directly so the
    remote job does not depend on whether gitignored algorithm files are
    included in C3's uploaded workspace.
    """
    run_id = uuid.uuid4().hex[:10]
    challenge = config.get("challenge", "unknown")
    is_gpu = bool(config.get("is_gpu"))
    dockerfile = "./scripts/c3/docker/Dockerfile.gpu" if is_gpu else "./scripts/c3/docker/Dockerfile.cpu"
    c3_path = ROOT / ".c3"
    script_name = f".c3-run-benchmark-{run_id}.sh"
    script_path = ROOT / script_name
    artifacts_dir = ROOT / "c3-artifacts"

    algo_path = ROOT / config.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs")
    kernel_cfg = config.get("kernel_path")
    kernel_path = ROOT / kernel_cfg if kernel_cfg else None

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

    algorithm_code = _read_optional(algo_path)
    kernel_code = _read_optional(kernel_path)

    runner = f"""\
#!/bin/bash
set -u

cd "${{C3_JOB_WORKDIR:-/workspace}}"
mkdir -p "${{C3_ARTIFACTS_DIR}}" c3-artifacts

export TIG_IN_DOCKER=1
export TIG_SWARM_SERVER={_yaml_quote(server)}

{_script_write_file_b64(config.get("algorithm_path", f"src/{challenge}/algorithm/mod.rs"), algorithm_code)}
"""
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


def _parse_c3_id(text: str) -> str | None:
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


def _poll_c3_job(job_id: str, env: dict, poll_interval: int = 15, max_polls: int = 480) -> str:
    """Poll `c3 squeue` until the job reaches a terminal state."""
    for i in range(max_polls):
        result = subprocess.run(
            ["c3", "squeue"], capture_output=True, text=True, cwd=ROOT, env=env,
        )
        out = result.stdout or ""
        for line in out.splitlines():
            if job_id in line:
                upper = line.upper()
                if "COMPLETED" in upper or "SYNCED" in upper or "SUCCEEDED" in upper:
                    return "completed"
                if "FAILED" in upper or "CANCELLED" in upper or "ERROR" in upper:
                    return "failed"
                # Job found but still running
                if i % 4 == 0:
                    # Show the status from the squeue line
                    print(f"    [C3] {job_id}: {line.strip()}")
                break
        else:
            # Job not in squeue at all — might have completed and dropped off
            if i > 2:
                return "completed"
        if i % 4 == 0 and job_id not in out:
            print(f"    [C3] Still waiting on {job_id} (poll {i + 1})…")
        time.sleep(poll_interval)
    return "timeout"


def _run_benchmark_c3(args: argparse.Namespace, config: dict, server: str) -> tuple[dict | None, str]:
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
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    print(f"    [C3] Deploy output: {combined[:300]}")

    job_id = _parse_c3_id(combined)
    if not job_id:
        err = f"[C3] Could not parse job ID from c3 deploy output:\n{combined[-2000:]}"
        print(f"    {err}")
        return None, err

    print(f"    [C3] Job submitted: {job_id} — polling for completion…")
    status = _poll_c3_job(job_id, env)

    # Fetch logs
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

    # Pull artifacts
    subprocess.run(
        ["c3", "pull", job_id], capture_output=True, text=True, cwd=ROOT, env=env,
    )

    # Check pulled artifacts first
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

    # Fallback: parse from logs
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


def run_benchmark(args: argparse.Namespace, config: dict, server: str) -> tuple[dict | None, str]:
    if args.compute == "local":
        return _run_benchmark_local()
    if args.compute == "c3":
        return _run_benchmark_c3(args, config, server)
    return None, f"Unknown compute provider: {args.compute}"


def publish_results(
    server: str, agent_id: str, bench: dict, mutation: dict, config: dict,
) -> dict:
    code = read_algorithm(config)
    kernel_code = ""
    kernel_path = config.get("kernel_path")
    if kernel_path:
        kernel_code = _read_optional(ROOT / kernel_path)
    payload = {
        "agent_id": agent_id,
        "title": mutation.get("title", ""),
        "description": mutation.get("description", ""),
        "strategy_tag": mutation.get("strategy_tag", "other"),
        "algorithm_code": code,
        "score": bench["score"],
        "feasible": bench["feasible"],
        "notes": mutation.get("notes", ""),
        "solution_data": bench.get("viz_data"),
        "track_scores": bench.get("track_scores"),
        "challenge": bench.get("challenge"),
    }
    if kernel_code:
        payload["kernel_code"] = kernel_code
    # VRP-only fields; forward only when benchmark.py actually populated
    # them (i.e. challenge is vehicle_routing).
    if bench.get("num_vehicles") is not None:
        payload["num_vehicles"] = bench["num_vehicles"]
    if bench.get("total_distance") is not None:
        payload["total_distance"] = bench["total_distance"]
    return server_post(f"{server}/api/iterations", payload)


# ── Sync ────────────────────────────────────────────────────────────


def sync_challenge() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "setup.py"), "sync"],
        cwd=ROOT, capture_output=True,
    )


# ── CLI ─────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Standalone swarm optimization loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--provider", required=True,
        choices=["anthropic", "openai", "google", "claude-code"],
        help="LLM provider (claude-code uses 'claude -p' headless mode, no API key needed)",
    )
    p.add_argument("--model", help="Model ID (default: per-provider sensible default)")
    p.add_argument("--api-key", help="API key (default: from env var)")
    p.add_argument("--api-base", help="Base URL for OpenAI-compatible endpoints")
    p.add_argument(
        "--compute", choices=["local", "c3"], default="local",
        help="Where to run each benchmark job (default: local)",
    )
    p.add_argument(
        "--hardware", default="l40",
        help="C3 GPU profile for --compute c3 (default: l40)",
    )
    p.add_argument(
        "--c3-api-key",
        help=(
            "C3 API key for --compute c3. Defaults to C3_API_KEY when set; "
            "otherwise the c3 CLI can use existing `c3 login` credentials."
        ),
    )
    p.add_argument(
        "--c3-time", default="02:00:00",
        help="C3 job walltime for each benchmark job (default: 02:00:00)",
    )
    p.add_argument(
        "--c3-cloud-provider",
        help="Optional C3 CLI cloud provider passed as `c3 deploy -p ...`",
    )
    p.add_argument(
        "--c3-no-build", action="store_true",
        help="Pass --no-build to c3 deploy, requiring a cached Docker image",
    )
    p.add_argument("--max-iterations", type=int, default=0, help="Stop after N iterations (0=unlimited)")
    p.add_argument("--agent-id", help="Resume with an existing agent ID")
    p.add_argument("--agent-name", help="Agent name (used with --agent-id)")
    return p.parse_args()


def resolve_api_key(args: argparse.Namespace) -> str:
    if args.provider == "claude-code":
        return ""
    if args.api_key:
        return args.api_key
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    key = os.environ.get(env_map[args.provider], "")
    if not key:
        sys.exit(f"No API key. Set ${env_map[args.provider]} or pass --api-key.")
    return key


# ── Main loop ───────────────────────────────────────────────────────


def main() -> int:
    args = parse_args()
    api_key = resolve_api_key(args)
    model = args.model or DEFAULT_MODELS[args.provider]

    config = load_config()
    server = config.get("server_url", "").rstrip("/")
    if not server:
        sys.exit("No server_url in swarm.config.json. Run setup.py first.")
    if args.compute == "c3":
        if shutil.which("c3") is None:
            sys.exit("c3 CLI not found. Install it from https://docs.cthree.cloud/.")

    # Register or resume
    if args.agent_id:
        agent_id = args.agent_id
        agent_name = args.agent_name or f"script-{agent_id[:8]}"
        print(f"Resuming agent: {agent_name} ({agent_id})")
    else:
        agent_id, agent_name = register_agent(server)
        print(f"Registered as: {agent_name} ({agent_id})")

    challenge_md = read_challenge_md()

    print(f"Provider: {args.provider}  Model: {model}")
    compute_desc = args.compute
    if args.compute == "c3":
        compute_desc = f"c3/{args.hardware.lower()}"
    print(f"Compute: {compute_desc}")
    print(f"Challenge: {config.get('challenge', '?')}")
    print(f"Server: {server}")
    print()

    iteration = 0
    while args.max_iterations == 0 or iteration < args.max_iterations:
        iteration += 1
        t_start = time.time()
        print(f"\n{'=' * 60}")
        print(f"  Iteration {iteration}  ({time.strftime('%H:%M:%S')})")
        print(f"{'=' * 60}")

        # ── Step 0: sync challenge ──────────────────────────────
        print("  [SYNC] Syncing challenge with server…")
        sync_challenge()
        config = load_config()
        challenge_md = read_challenge_md()
        print(f"  [SYNC] Challenge: {config.get('challenge', '?')}  GPU: {config.get('is_gpu', False)}")

        # Fetch server-side swarm config for dynamic fields (strategy_tags).
        try:
            swarm_cfg = server_get(f"{server}/api/swarm_config")
            config["available_challenges"] = swarm_cfg.get("available_challenges", {})
        except Exception:
            pass

        # ── Step 1: get state ───────────────────────────────────
        print("  [STATE] Fetching agent state…")
        try:
            state = get_state(server, agent_id)
        except Exception as e:
            print(f"  [STATE] FAILED: {e}")
            time.sleep(5)
            continue

        my_score = state.get("my_best_score")
        global_best = state.get("best_score")
        stagnation = state.get("my_runs_since_improvement", 0)
        runs = state.get("my_runs", 0)
        improvements = state.get("my_improvements", 0)
        print(f"  [STATE] My best: {my_score}  Global best: {global_best}")
        print(f"  [STATE] Runs: {runs}  Improvements: {improvements}  Stagnation: {stagnation}")

        reset = state.get("trajectory_reset")
        if reset:
            print(f"  [STATE] ** TRAJECTORY RESET — {reset.get('type')} **")
            post_message(server, agent_name, agent_id,
                         f"Trajectory reset: {reset.get('type')}")

        # ── Step 2: write current best to mod.rs (+ kernels.cu) ─
        best_code = state.get("best_algorithm_code") or ""
        best_kernel = state.get("best_kernel_code") or ""
        is_gpu = bool(config.get("is_gpu"))
        bootstrap = is_stub_code(best_code)
        if best_code and not bootstrap:
            write_algorithm(best_code, config)
            print(f"  [FILES] Wrote mod.rs ({len(best_code)} chars)")
        if is_gpu and best_kernel:
            write_kernel(best_kernel, config)
            print(f"  [FILES] Wrote kernels.cu ({len(best_kernel)} chars)")
        elif is_gpu:
            print(f"  [FILES] No kernel code from server — using local kernels.cu")

        if bootstrap:
            print("  [FILES] Starting from stub — will ask LLM to write initial implementation")

        # ── Step 3a: LLM hypothesis ────────────────────────────
        hint = state.get("stagnation_hint")
        if hint:
            print(f"  [LLM] Stagnation hint: {hint}")
        if state.get("inspiration_code"):
            print(f"  [LLM] Inspiration available from {state.get('inspiration_agent_name', '?')}")

        prior = state.get("prior_hypotheses") or []
        if prior:
            print(f"  [LLM] {len(prior)} prior failed hypotheses on this program")

        print(f"  [LLM] Generating hypothesis via {args.provider}/{model}…")
        try:
            hyp_response = call_llm(
                args.provider, model, api_key,
                build_hypothesis_system_prompt(challenge_md, config, is_bootstrap=bootstrap),
                build_hypothesis_user_prompt(state, config),
                args.api_base,
            )
        except Exception as e:
            print(f"  [LLM] HYPOTHESIS FAILED: {e}")
            post_message(server, agent_name, agent_id,
                         f"LLM call failed: {type(e).__name__}")
            time.sleep(5)
            continue

        hypothesis = parse_hypothesis(hyp_response)
        tag = hypothesis.get("strategy_tag", "?")
        title = hypothesis.get("title", "?")
        desc = hypothesis.get("description", "")
        print(f"  [LLM] Hypothesis: [{tag}] {title}")
        if desc:
            print(f"         {desc[:120]}")

        # ── Step 3b: LLM code (with retry on validation failure) ─
        original_code = best_code
        original_kernel = best_kernel if is_gpu else ""
        code = None
        new_kernel = None
        max_code_attempts = 3
        retry_suffix = (
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            if is_gpu else ""
        )
        for attempt in range(max_code_attempts):
            if attempt == 0:
                print(f"  [LLM] Generating code via {args.provider}/{model}…")
                user_prompt = build_code_user_prompt(state, hypothesis, config)
            else:
                print(f"  [LLM] Code retry {attempt}/{max_code_attempts - 1}: {violation}")
                user_prompt = (
                    build_code_user_prompt(state, hypothesis, config)
                    + f"\n\nYour previous response was rejected: {violation}\n"
                    "Fix the issue and return the complete source." + retry_suffix
                )
            try:
                code_response = call_llm(
                    args.provider, model, api_key,
                    build_code_system_prompt(challenge_md, config),
                    user_prompt,
                    args.api_base,
                )
            except Exception as e:
                print(f"  [LLM] CODE GENERATION FAILED: {e}")
                post_message(server, agent_name, agent_id,
                             f"LLM call failed: {type(e).__name__}")
                time.sleep(5)
                break

            if is_gpu:
                parsed, parsed_kernel = parse_gpu_code(code_response)
                if parsed_kernel:
                    print(f"  [LLM] Got two-file response (rust: {len(parsed)} chars, cuda: {len(parsed_kernel)} chars)")
                else:
                    print(f"  [LLM] WARNING: No kernel separator found — got rust only ({len(parsed)} chars)")
            else:
                parsed = parse_code(code_response)
                parsed_kernel = ""
            if not parsed:
                print("  [LLM] Empty code response — skipping iteration")
                break

            violation = validate_code(original_code, parsed)
            if violation:
                print(f"  [LLM] Validation failed: {violation}")
                continue
            code = parsed
            new_kernel = parsed_kernel
            print(f"  [LLM] Code validated OK")
            break

        if not code:
            print(f"  [SKIP] No valid code produced — skipping to next iteration")
            continue

        # ── Code similarity check ──────────────────────────────
        if original_code:
            sim = difflib.SequenceMatcher(None, original_code, code).ratio()
            pct = sim * 100
            if pct < 30:
                label = "likely full rewrite"
            elif pct < 60:
                label = "major rewrite"
            elif pct < 85:
                label = "moderate edit"
            else:
                label = "incremental edit"
            print(f"  [FILES] Code similarity: {pct:.0f}% ({label})")
        else:
            print("  [FILES] First algorithm (no prior code)")

        write_algorithm(code, config)
        if is_gpu and new_kernel:
            write_kernel(new_kernel, config)
            print(f"  [FILES] Wrote both mod.rs + kernels.cu")
        elif is_gpu:
            print(f"  [FILES] Wrote mod.rs only (no kernel changes)")

        # ── Step 4: benchmark (with build-error retry) ─────────
        compute_label = f"C3/{args.hardware}" if args.compute == "c3" else "local Docker"
        print(f"  [BENCH] Running benchmark on {compute_label}…")
        post_message(server, agent_name, agent_id,
                     f"Trying [{tag}] {title}")

        send_heartbeat(server, agent_id)

        max_build_retries = 2
        bench = None
        code_changed_by_fix = False
        for build_attempt in range(1 + max_build_retries):
            bench, build_err = run_benchmark(args, config, server)
            if bench is not None:
                break
            # Check if this is an infrastructure error (C3 auth, Docker, etc.)
            # vs a code compilation error — only retry code fixes for the latter
            infra_markers = ["401", "API Error", "c3 CLI not found", "Docker image",
                             "Could not parse job ID", "timeout", "403", "500"]
            is_infra_error = any(m in build_err for m in infra_markers)
            if is_infra_error:
                print(f"  [BENCH] INFRASTRUCTURE ERROR (not a code problem):")
                print(f"          {build_err[:300]}")
                break
            if build_attempt >= max_build_retries:
                break
            # Feed compiler errors back to the LLM for a fix
            print(f"  [BENCH] Build retry {build_attempt + 1}/{max_build_retries} — asking LLM to fix…")
            compiler_errors = build_err[-1500:]
            fix_parts = [f"Current algorithm (mod.rs):\n```rust\n{read_algorithm(config)}\n```\n"]
            if is_gpu:
                cur_kernel = read_kernel(config)
                if cur_kernel:
                    fix_parts.append(f"Current CUDA kernels (kernels.cu):\n```cuda\n{cur_kernel}\n```\n")
            fix_parts.append(
                f"This code failed to compile. Here are the errors:\n```\n{compiler_errors}\n```\n\n"
                "Fix the compile errors and return the complete source."
            )
            if is_gpu:
                fix_parts.append(
                    "\nReturn BOTH files separated by: // --- kernels.cu ---"
                    "\nEnsure kernel function names match between mod.rs and kernels.cu."
                )
            fix_prompt = "\n".join(fix_parts)
            try:
                fix_response = call_llm(
                    args.provider, model, api_key,
                    build_code_system_prompt(challenge_md, config),
                    fix_prompt,
                    args.api_base,
                )
            except Exception as e:
                print(f"  Fix LLM call failed: {e}", file=sys.stderr)
                break
            if is_gpu:
                fixed, fixed_kernel = parse_gpu_code(fix_response)
            else:
                fixed = parse_code(fix_response)
                fixed_kernel = ""
            if not fixed:
                print("  Empty fix response — giving up")
                break
            fix_violation = validate_code(original_code, fixed)
            if fix_violation:
                print(f"  Fix failed validation: {fix_violation}")
                break
            before_fix = read_algorithm(config)
            fix_sim = difflib.SequenceMatcher(None, before_fix, fixed).ratio()
            print(f"  Fix similarity to broken code: {fix_sim * 100:.0f}%")
            write_algorithm(fixed, config)
            if is_gpu and fixed_kernel:
                write_kernel(fixed_kernel, config)
            code_changed_by_fix = True

        if bench is None:
            print(f"  [BENCH] FAILED — build_err: {build_err[:300]}")
            print(f"  [BENCH] Restoring previous code and continuing")
            if best_code:
                write_algorithm(best_code, config)
            if is_gpu and best_kernel:
                write_kernel(best_kernel, config)
            post_message(server, agent_name, agent_id,
                         f"[{tag}] {title} — benchmark failed (build error?)")
            continue

        track_scores = bench.get("track_scores", {})
        errors = bench.get("errors") or []
        print(f"  [BENCH] Score: {bench['score']:.0f}  Feasible: {bench['feasible']}")
        if track_scores:
            for tk, ts in track_scores.items():
                print(f"          Track {tk}: {ts:.0f}")
        if errors:
            print(f"  [BENCH] Errors ({len(errors)}):")
            for e in errors[:5]:
                print(f"          {e}")

        # ── Step 4b: runtime error retry ───────────────────────
        max_runtime_retries = 2
        runtime_errors = bench.get("errors") or []
        if runtime_errors and not bench["feasible"]:
            for rt_attempt in range(max_runtime_retries):
                print(f"  Runtime retry {rt_attempt + 1}/{max_runtime_retries} — asking LLM to fix ...")
                print(f"  Errors: {runtime_errors}")
                current_code = read_algorithm(config)
                current_kernel = read_kernel(config) if is_gpu else ""
                try:
                    fix_response = call_llm(
                        args.provider, model, api_key,
                        build_code_system_prompt(challenge_md, config),
                        build_runtime_fix_prompt(current_code, bench, current_kernel),
                        args.api_base,
                    )
                except Exception as e:
                    print(f"  Runtime fix LLM call failed: {e}", file=sys.stderr)
                    break
                if is_gpu:
                    fixed, fixed_kernel = parse_gpu_code(fix_response)
                else:
                    fixed = parse_code(fix_response)
                    fixed_kernel = ""
                if not fixed:
                    print("  Empty fix response — giving up")
                    break
                fix_violation = validate_code(original_code, fixed)
                if fix_violation:
                    print(f"  Fix failed validation: {fix_violation}")
                    break
                fix_sim = difflib.SequenceMatcher(None, current_code, fixed).ratio()
                print(f"  Fix similarity: {fix_sim * 100:.0f}%")
                write_algorithm(fixed, config)
                if is_gpu and fixed_kernel:
                    write_kernel(fixed_kernel, config)
                code_changed_by_fix = True

                print("  Re-running benchmark ...")
                send_heartbeat(server, agent_id)
                bench, build_err = run_benchmark(args, config, server)
                if bench is None:
                    # Runtime fix introduced a compile error — one retry
                    print(f"  Runtime fix caused compile error — asking LLM to fix ...")
                    cfp_parts = [f"Current algorithm (mod.rs):\n```rust\n{read_algorithm(config)}\n```\n"]
                    if is_gpu:
                        ck = read_kernel(config)
                        if ck:
                            cfp_parts.append(f"Current CUDA kernels (kernels.cu):\n```cuda\n{ck}\n```\n")
                    cfp_parts.append(
                        f"This code failed to compile. Here are the errors:\n```\n{build_err[-1500:]}\n```\n\n"
                        "Fix the compile errors and return the complete source."
                    )
                    if is_gpu:
                        cfp_parts.append("\nReturn BOTH files separated by: // --- kernels.cu ---")
                    compile_fix_prompt = "\n".join(cfp_parts)
                    try:
                        compile_fix_resp = call_llm(
                            args.provider, model, api_key,
                            build_code_system_prompt(challenge_md, config),
                            compile_fix_prompt,
                            args.api_base,
                        )
                    except Exception as e:
                        print(f"  Compile fix LLM call failed: {e}", file=sys.stderr)
                        if best_code:
                            write_algorithm(best_code, config)
                        if is_gpu and best_kernel:
                            write_kernel(best_kernel, config)
                        break
                    if is_gpu:
                        compile_fixed, compile_fixed_kernel = parse_gpu_code(compile_fix_resp)
                    else:
                        compile_fixed = parse_code(compile_fix_resp)
                        compile_fixed_kernel = ""
                    if not compile_fixed or validate_code(original_code, compile_fixed):
                        print("  Compile fix failed validation — restoring and continuing")
                        if best_code:
                            write_algorithm(best_code, config)
                        if is_gpu and best_kernel:
                            write_kernel(best_kernel, config)
                        break
                    write_algorithm(compile_fixed, config)
                    if is_gpu and compile_fixed_kernel:
                        write_kernel(compile_fixed_kernel, config)
                    bench, build_err = run_benchmark(args, config, server)
                    if bench is None:
                        print("  Still won't compile — restoring and continuing")
                        if best_code:
                            write_algorithm(best_code, config)
                        if is_gpu and best_kernel:
                            write_kernel(best_kernel, config)
                        break
                print(f"  Score: {bench['score']}  Feasible: {bench['feasible']}")
                runtime_errors = bench.get("errors") or []
                if not runtime_errors or bench["feasible"]:
                    break

        if bench is None:
            post_message(server, agent_name, agent_id,
                         f"[{tag}] {title} — benchmark failed after runtime fix")
            continue

        # ── Step 4c: re-describe hypothesis if code changed ────
        if code_changed_by_fix:
            print("  Code changed during error recovery — re-describing hypothesis ...")
            final_code = read_algorithm(config)
            try:
                redesc_response = call_llm(
                    args.provider, model, api_key,
                    build_hypothesis_system_prompt(challenge_md, config),
                    build_redescribe_hypothesis_prompt(
                        original_code or best_code or "", final_code, hypothesis,
                    ),
                    args.api_base,
                )
                updated = parse_hypothesis(redesc_response)
                print(f"  Updated hypothesis: [{updated.get('strategy_tag', '?')}] {updated.get('title', '?')}")
                hypothesis = updated
                tag = hypothesis.get("strategy_tag", "?")
                title = hypothesis.get("title", "?")
            except Exception as e:
                print(f"  Re-describe failed: {e} — using original hypothesis", file=sys.stderr)

        # ── Step 5: publish ─────────────────────────────────────
        print(f"  [PUBLISH] Publishing results to server…")
        is_new_best = False
        try:
            result = publish_results(server, agent_id, bench, hypothesis, config)
            is_new_best = result.get("is_new_best", False)
            if is_new_best:
                print("  [PUBLISH] ** NEW PERSONAL BEST! **")
            else:
                print(f"  [PUBLISH] Recorded (not a new best)")
        except Exception as e:
            print(f"  [PUBLISH] FAILED: {e}")

        # ── chat + heartbeat ────────────────────────────────────
        status = "NEW BEST!" if is_new_best else f"score {bench['score']:.0f}"
        feasible_str = "" if bench["feasible"] else " (INFEASIBLE)"
        post_message(server, agent_name, agent_id,
                     f"[{tag}] {title} → {status}{feasible_str}")
        send_heartbeat(server, agent_id)

        elapsed = time.time() - t_start
        print(f"  [DONE] Iteration {iteration} finished in {elapsed:.0f}s")
        print()

    print("Loop complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
