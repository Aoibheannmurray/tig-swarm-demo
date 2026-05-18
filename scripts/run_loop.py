#!/usr/bin/env python3
"""Standalone swarm optimization loop — no agent required.

Handles server communication, prompt construction, LLM-based code mutation,
benchmarking, and result publishing.  Works with any LLM provider (Anthropic,
OpenAI, Google) or any OpenAI-compatible endpoint via --api-base.

Usage:
    python setup.py
    export ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY / GOOGLE_API_KEY
    python scripts/run_loop.py

    # Overrides still work:
    python scripts/run_loop.py --provider openai --model gpt-4o
    python scripts/run_loop.py --provider google --model gemini-2.5-pro
    python scripts/run_loop.py --provider openai --api-base https://api.together.xyz
    python scripts/run_loop.py --provider anthropic --compute c3 --hardware l40
    python scripts/run_loop.py --provider anthropic --compute c3 --env rust:1-bookworm
    python scripts/run_loop.py --provider claude-code --model claude-opus-4-7

    # Resume a specific previous agent
    python scripts/run_loop.py --agent-id <id> --agent-name <name>

Picking a model (--model):
    anthropic   claude-opus-4-7, claude-sonnet-4-6 (default),
                claude-haiku-4-5-20251001
    openai      gpt-4o (default), gpt-5, gpt-5-mini, o1, o3-mini
                (gpt-5* and o-series auto-switch to the Responses API)
    google      gemini-2.5-flash (default), gemini-2.5-pro
    claude-code uses your local `claude -p` session — pass any model ID that
                your Claude Code install accepts, or omit for its default

    --api-base lets you point --provider openai at any OpenAI-compatible
    endpoint (Together, Groq, DeepSeek, Ollama, vLLM, …); pass the host's
    model ID via --model.

Provider/model/compute defaults come from agent.config.json when present.
API keys are read from the environment: ANTHROPIC_API_KEY, OPENAI_API_KEY,
GOOGLE_API_KEY (or pass --api-key directly). C3 compute can use C3_API_KEY,
--c3-api-key, or existing `c3 login` credentials. C3 Docker jobs use public
Docker Hub images, configured with --env.

claude-code provider:
    Shells out to your local `claude -p` binary instead of hitting an HTTP
    API. Auth comes from your Claude Code login (OAuth / subscription) — no
    ANTHROPIC_API_KEY needed. Calls run from a temp directory so the CLI's
    CLAUDE.md auto-discovery does NOT pull this repo's docs into the system
    prompt — run_loop.py supplies its own. Token usage is not reported by
    the CLI, so the dashboard's per-agent cost column will read $0 for this
    provider.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_CONFIG_PATH = ROOT / "agent.config.json"
sys.path.insert(0, str(ROOT / "scripts"))

from llm_backends import DEFAULT_MODELS, call_llm, estimate_cost

from challenge_files import (
    ChallengeFiles,
    is_stub_code,
    read_challenge_md,
    validate_code,
)
from server import (
    agent_exists,
    get_state,
    post_message,
    publish_results,
    register_agent,
    send_heartbeat,
    server_get,
)
from prompts import (
    build_agentic_user_prompt,
    build_code_system_prompt,
    build_code_user_prompt,
    build_compile_fix_prompt,
    build_hypothesis_system_prompt,
    build_hypothesis_user_prompt,
    build_redescribe_hypothesis_prompt,
    build_redescribe_system_prompt,
    build_runtime_fix_prompt,
    parse_hypothesis,
)
import agentic_backends
import agentic_sandbox
from c3_compute import run_benchmark_c3

# Backoff after a recoverable iteration-level failure (state fetch, LLM error).
_ITERATION_BACKOFF_SECS = 5
# Skip the LLM re-describe call when the post-fix code is this similar to
# the pre-fix code — the fix was almost certainly cosmetic (bounds checks,
# error wrappers) and not worth a round-trip to confirm "no change".
_REDESCRIBE_SIMILARITY_THRESHOLD = 0.95

_PROMPT_LOG_DIR = ROOT / "prompts_log"


def _call_llm_logged(
    call_type: str, config: dict,
    provider: str, model: str, api_key: str,
    system: str, prompt: str, api_base: str | None = None,
) -> tuple[str, dict]:
    """Wrapper around call_llm that records the exchange when log_prompts is set.

    One markdown file per call in ./prompts_log/. No-op when the flag is off.
    """
    response, usage = call_llm(provider, model, api_key, system, prompt, api_base)
    if config.get("log_prompts"):
        try:
            _PROMPT_LOG_DIR.mkdir(exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1e6) % 1_000_000:06d}"
            path = _PROMPT_LOG_DIR / f"{ts}_{call_type}.md"
            path.write_text(
                f"# {call_type}\n\n"
                f"- timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"- provider: {provider}\n"
                f"- model: {model}\n"
                f"- input_tokens: {usage.get('input_tokens', 0)}\n"
                f"- output_tokens: {usage.get('output_tokens', 0)}\n\n"
                f"## SYSTEM\n\n{system}\n\n"
                f"## USER\n\n{prompt}\n\n"
                f"## RESPONSE\n\n{response}\n"
            )
        except Exception as e:
            print(f"  [LOG] Prompt log write failed: {e}", file=sys.stderr)
    return response, usage


# ── Config & sync ──────────────────────────────────────────────────


def load_config() -> dict:
    cfg_path = ROOT / ".swarm-cache.json"
    if not cfg_path.exists():
        sys.exit(
            ".swarm-cache.json not found. Run `python setup.py sync` first "
            "(scripts/run_loop.py normally calls it at the top of every iteration)."
        )
    return json.loads(cfg_path.read_text())


def load_agent_config() -> dict:
    if not AGENT_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(AGENT_CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_agent_config(config: dict) -> None:
    AGENT_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def sync_challenge() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "setup.py"), "sync"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[-500:]
        print(f"  [SYNC] WARNING: setup.py sync failed ({result.returncode}): {err}", file=sys.stderr)


# ── Benchmark dispatch ─────────────────────────────────────────────


def _run_benchmark_local() -> tuple[dict | None, str]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0:
        err = result.stderr or result.stdout or "Benchmark failed"
        print(f"  Benchmark failed:\n{err[-2000:]}", file=sys.stderr)
        return None, err
    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:
        print(f"  Benchmark output not valid JSON:\n{result.stdout[:300]}", file=sys.stderr)
        return None, "Benchmark output was not valid JSON"


def run_benchmark(args: argparse.Namespace, config: dict, server: str) -> tuple[dict | None, str]:
    if args.compute == "local":
        return _run_benchmark_local()
    if args.compute == "c3":
        return run_benchmark_c3(args, config, server)
    return None, f"Unknown compute provider: {args.compute}"


# ── Extracted iteration helpers ────────────────────────────────────


def _generate_code(
    args: argparse.Namespace, model: str, api_key: str,
    state: dict, hypothesis: dict, config: dict,
    challenge_md: str, files: ChallengeFiles,
) -> tuple[str | None, str | None, int, int]:
    """LLM code generation with retry on validation failure.

    Returns (code, kernel, input_tokens, output_tokens).
    """
    input_tokens = 0
    output_tokens = 0
    max_attempts = 3
    violation = ""

    for attempt in range(max_attempts):
        if attempt == 0:
            print(f"  [LLM] Generating code via {args.provider}/{model}…")
            user_prompt = build_code_user_prompt(state, hypothesis, config)
        else:
            print(f"  [LLM] Code retry {attempt}/{max_attempts - 1}: {violation}")
            user_prompt = (
                build_code_user_prompt(state, hypothesis, config)
                + f"\n\nYour previous response was rejected: {violation}\n"
                "Fix the issue and return the complete source."
                + files.separator_suffix()
            )
        try:
            code_response, usage = _call_llm_logged(
                "code", config,
                args.provider, model, api_key,
                build_code_system_prompt(challenge_md, config),
                user_prompt,
                args.api_base,
            )
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
        except Exception as e:
            print(f"  [LLM] CODE GENERATION FAILED: {e}")
            break

        parsed, parsed_kernel = files.parse_response(code_response)
        print(f"  [LLM] {files.describe_parse(parsed, parsed_kernel)}")
        if not parsed:
            print("  [LLM] Empty code response — skipping iteration")
            break

        violation = validate_code(parsed)
        if violation:
            print(f"  [LLM] Validation failed: {violation}")
            continue
        print(f"  [LLM] Code validated OK")
        return parsed, parsed_kernel, input_tokens, output_tokens

    return None, None, input_tokens, output_tokens


def _try_compile_fix(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, challenge_md: str,
    files: ChallengeFiles,
    build_err: str,
) -> tuple[bool, int, int]:
    """Ask the LLM to fix compiler errors, write the result.

    Returns (success, input_tokens, output_tokens).
    """
    code, kernel = files.read()
    fix_prompt = build_compile_fix_prompt(code, kernel, build_err, files.is_gpu)
    try:
        fix_response, usage = _call_llm_logged(
            "compile_fix", config,
            args.provider, model, api_key,
            build_code_system_prompt(challenge_md, config),
            fix_prompt,
            args.api_base,
        )
    except Exception as e:
        print(f"  Fix LLM call failed: {e}", file=sys.stderr)
        return False, 0, 0

    fixed, fixed_kernel = files.parse_response(fix_response)
    if not fixed:
        print("  Empty fix response — giving up")
        return False, usage["input_tokens"], usage["output_tokens"]

    violation = validate_code(fixed)
    if violation:
        print(f"  Fix failed validation: {violation}")
        return False, usage["input_tokens"], usage["output_tokens"]

    before_fix, _ = files.read()
    sim = difflib.SequenceMatcher(None, before_fix, fixed).ratio()
    print(f"  Fix similarity to broken code: {sim * 100:.0f}%")
    files.write(fixed, fixed_kernel)
    return True, usage["input_tokens"], usage["output_tokens"]


def _benchmark_with_compile_fix(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, server: str, challenge_md: str,
    files: ChallengeFiles,
) -> tuple[dict | None, str, bool, int, int]:
    """Run benchmark, retrying with LLM compile fixes on failure.

    Returns (bench, build_err, code_changed, input_tokens, output_tokens).
    """
    max_retries = 2
    input_tokens = 0
    output_tokens = 0
    code_changed = False

    for attempt in range(1 + max_retries):
        bench, build_err = run_benchmark(args, config, server)
        if bench is not None:
            return bench, "", code_changed, input_tokens, output_tokens

        infra_markers = ["401", "API Error", "c3 CLI not found", "Docker image",
                         "Could not parse job ID", "timeout", "403", "500"]
        if any(m in build_err for m in infra_markers):
            print(f"  [BENCH] INFRASTRUCTURE ERROR (not a code problem):")
            print(f"          {build_err[:300]}")
            return None, build_err, code_changed, input_tokens, output_tokens

        if attempt >= max_retries:
            break

        print(f"  [BENCH] Build retry {attempt + 1}/{max_retries} — asking LLM to fix…")
        ok, it, ot = _try_compile_fix(
            args, model, api_key, config, challenge_md,
            files, build_err,
        )
        input_tokens += it
        output_tokens += ot
        if not ok:
            break
        code_changed = True

    return None, build_err, code_changed, input_tokens, output_tokens


def _fix_runtime_errors(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, server: str, agent_id: str, challenge_md: str,
    files: ChallengeFiles, bench: dict,
    best_code: str, best_kernel: str,
) -> tuple[dict | None, bool, int, int]:
    """Retry runtime errors by asking the LLM to fix and re-benchmarking.

    Returns (bench, code_changed, input_tokens, output_tokens).
    Returns bench=None when the runtime fix exhausts retries with the bench
    in a broken state; the previous best is restored to disk so the next
    iteration starts from a working algorithm.
    """
    max_retries = 2
    input_tokens = 0
    output_tokens = 0
    code_changed = False

    def restore_and_fail() -> tuple[dict | None, bool, int, int]:
        if best_code:
            files.write(best_code, best_kernel)
        return None, code_changed, input_tokens, output_tokens

    for rt_attempt in range(max_retries):
        runtime_errors = bench.get("errors") or []
        if not runtime_errors or bench.get("feasible"):
            break

        print(f"  Runtime retry {rt_attempt + 1}/{max_retries} — asking LLM to fix ...")
        print(f"  Errors: {runtime_errors}")
        current_code, current_kernel = files.read()
        try:
            fix_response, usage = _call_llm_logged(
                "runtime_fix", config,
                args.provider, model, api_key,
                build_code_system_prompt(challenge_md, config),
                build_runtime_fix_prompt(current_code, bench, current_kernel, config.get("timeout", 30)),
                args.api_base,
            )
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
        except Exception as e:
            print(f"  Runtime fix LLM call failed: {e}", file=sys.stderr)
            return restore_and_fail()

        fixed, fixed_kernel = files.parse_response(fix_response)
        if not fixed:
            print("  Empty fix response — giving up")
            return restore_and_fail()

        violation = validate_code(fixed)
        if violation:
            print(f"  Fix failed validation: {violation}")
            return restore_and_fail()

        sim = difflib.SequenceMatcher(None, current_code, fixed).ratio()
        print(f"  Fix similarity: {sim * 100:.0f}%")
        files.write(fixed, fixed_kernel)
        code_changed = True

        print("  Re-running benchmark ...")
        send_heartbeat(server, agent_id)
        bench_result, build_err = run_benchmark(args, config, server)

        if bench_result is None:
            print(f"  Runtime fix caused compile error — asking LLM to fix ...")
            ok, it, ot = _try_compile_fix(
                args, model, api_key, config, challenge_md,
                files, build_err,
            )
            input_tokens += it
            output_tokens += ot
            if not ok:
                return restore_and_fail()

            bench_result, build_err = run_benchmark(args, config, server)
            if bench_result is None:
                print("  Still won't compile — restoring and continuing")
                return restore_and_fail()

        bench = bench_result
        print(f"  Score: {bench.get('score', 0):.0f}  Feasible: {bench.get('feasible', False)}")

    return bench, code_changed, input_tokens, output_tokens


# ── Agentic (mode 2) iteration ─────────────────────────────────────


_AGENTIC_HEARTBEAT_INTERVAL_S = 60


def _start_heartbeat_thread(server: str, agent_id: str) -> threading.Event:
    """Send a heartbeat every minute while the agentic call is running.

    Mode-2 iterations can run 10+ minutes inside a single `claude -p`
    subprocess. Without a background heartbeat the agent would drop from
    the server's inspiration pool mid-iteration. Returns a stop event the
    caller must set when the agentic call exits.
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(_AGENTIC_HEARTBEAT_INTERVAL_S):
            try:
                send_heartbeat(server, agent_id)
            except Exception as e:
                print(f"  [HEARTBEAT] background beat failed: {e}", file=sys.stderr)

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    return stop


def _seed_worktree_files(
    workdir: Path, state: dict, files: ChallengeFiles, config: dict,
) -> None:
    """Drop the agent's current best into the worktree as its starting point.

    The worktree is gitignored at `src/<challenge>/algorithm/mod.rs` so on a
    fresh worktree there's no mod.rs at all — the loop has to put one
    there before the agent runs. Same for kernels.cu on GPU challenges.
    Also copies .swarm-cache.json across (benchmark.py reads it).
    """
    best_code = state.get("best_algorithm_code") or ""
    best_kernel = state.get("best_kernel_code") or ""
    algo_rel = config["algorithm_path"]
    algo_path = workdir / algo_rel
    algo_path.parent.mkdir(parents=True, exist_ok=True)
    if best_code:
        algo_path.write_text(best_code)

    kernel_rel = config.get("kernel_path")
    if files.is_gpu and kernel_rel and best_kernel:
        kp = workdir / kernel_rel
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text(best_kernel)

    agentic_sandbox.seed_worktree_config(workdir)


def _read_worktree_files(
    workdir: Path, files: ChallengeFiles, config: dict,
) -> tuple[str, str]:
    """Read whatever the agent left on disk in the worktree."""
    algo_path = workdir / config["algorithm_path"]
    code = algo_path.read_text() if algo_path.exists() else ""
    kernel = ""
    if files.is_gpu and config.get("kernel_path"):
        kp = workdir / config["kernel_path"]
        if kp.exists():
            kernel = kp.read_text()
    return code, kernel


def _run_agentic_iteration(
    args: argparse.Namespace,
    state: dict, config: dict, server: str,
    agent_id: str, agent_name: str,
    workdir: Path, backend: agentic_backends.AgenticBackend,
    challenge_md: str, files: ChallengeFiles,
) -> tuple[dict, str, str, agentic_backends.AgenticResult]:
    """One tooled-agent iteration. Returns (hypothesis, code, kernel, result).

    Hypothesis is always non-None: when the agent forgot to write
    `.swarm/hypothesis.json` the caller gets a synthesized fallback so the
    iteration can still publish. Code/kernel are whatever's on disk in the
    worktree when the agent exits; the caller validates and benchmarks.
    """
    backend.prepare(workdir, challenge_md, config)
    _seed_worktree_files(workdir, state, files, config)
    agentic_sandbox.reset_iteration_state(workdir)

    user_prompt = build_agentic_user_prompt(state, config)
    print(f"  [AGENTIC] Launching {backend.name} in {workdir} (timeout {args.agentic_timeout}s)…")

    stop = _start_heartbeat_thread(server, agent_id)
    try:
        result = backend.iterate(
            workdir, user_prompt,
            model=args.model, timeout_s=args.agentic_timeout,
        )
    finally:
        stop.set()

    if result.timed_out:
        print(f"  [AGENTIC] TIMED OUT after {result.duration_s:.0f}s")
    else:
        print(f"  [AGENTIC] Exit {result.exit_code}  duration {result.duration_s:.0f}s")
    if result.exit_code != 0 and not result.timed_out:
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        if tail:
            print(f"  [AGENTIC] tail: {tail}")

    hypothesis = agentic_sandbox.read_agent_hypothesis(workdir)
    if hypothesis is None:
        print("  [AGENTIC] No .swarm/hypothesis.json — synthesizing from stdout")
        hypothesis = agentic_sandbox.synthesize_hypothesis_from_stdout(result.stdout)

    code, kernel = _read_worktree_files(workdir, files, config)
    return hypothesis, code, kernel, result


# ── CLI ────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Standalone swarm optimization loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--provider",
        choices=[
            "anthropic", "openai", "google",
            "claude-code", "claude-code-agentic", "codex-agentic",
        ],
        help=(
            "LLM provider (default: agent.config.json, then anthropic). "
            "`claude-code` = headless one-shot completion via the local CLI; "
            "`claude-code-agentic` = headless Claude Code agent mode in a "
            "sandboxed worktree with file-edit tools; `codex-agentic` = same "
            "shape via the local `codex exec` CLI. Agentic modes are "
            "subscription-only (auth via the respective CLI's login) and "
            "burn ~5–20× tokens per iteration vs single-shot."
        ),
    )
    default_hint = ", ".join(f"{prov}={mid}" for prov, mid in DEFAULT_MODELS.items())
    p.add_argument(
        "--model",
        help=(
            f"Model ID. Defaults: {default_hint}. "
            "See the examples below for common alternatives per provider."
        ),
    )
    p.add_argument("--api-key", help="API key (default: from env var)")
    p.add_argument("--api-base", help="Base URL for OpenAI-compatible endpoints")
    p.add_argument(
        "--compute", choices=["local", "c3"],
        help="Where to run each benchmark job (default: agent.config.json, then local)",
    )
    p.add_argument(
        "--hardware",
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
        "--c3-time",
        help="C3 job walltime for each benchmark job (default: 02:00:00)",
    )
    p.add_argument(
        "--c3-provider",
        help="Optional C3 CLI provider passed as `c3 deploy -p ...`",
    )
    p.add_argument(
        "--env",
        help="Docker Hub environment image for C3 jobs; overrides built-in defaults",
    )
    p.add_argument("--env-image", dest="env", help=argparse.SUPPRESS)
    p.add_argument("--c3-image", dest="env", help=argparse.SUPPRESS)
    p.add_argument("--env-cpu", dest="env", help=argparse.SUPPRESS)
    p.add_argument("--c3-cpu-image", dest="env", help=argparse.SUPPRESS)
    p.add_argument("--env-gpu", dest="env", help=argparse.SUPPRESS)
    p.add_argument("--c3-gpu-image", dest="env", help=argparse.SUPPRESS)
    p.add_argument("--max-iterations", type=int, default=0, help="Stop after N iterations (0=unlimited)")
    p.add_argument(
        "--agentic-timeout", type=int, default=900,
        help=(
            "Wall-clock timeout in seconds for one agentic iteration "
            "(claude-code-agentic only). Default 900 (15 min). The claude "
            "CLI has no --max-turns flag, so this is the only ceiling."
        ),
    )
    p.add_argument("--agent-id", help="Resume with an existing agent ID")
    p.add_argument("--agent-name", help="Agent name (used with --agent-id)")
    p.add_argument("--new-agent", action="store_true", help="Register a new agent even if agent.config.json has one.")
    return p.parse_args()


def resolve_api_key(provider: str, api_key: str | None) -> str:
    if provider in ("claude-code", "claude-code-agentic", "codex-agentic"):
        return ""
    if api_key:
        return api_key
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    key = os.environ.get(env_map[provider], "")
    if not key:
        sys.exit(f"No API key. Set ${env_map[provider]} or pass --api-key.")
    return key


# ── Main loop ──────────────────────────────────────────────────────


def main() -> int:
    args = parse_args()
    config = load_config()
    agent_config = load_agent_config()
    # `setup.py sync` (called at the top of every iteration) rebuilds
    # .swarm-cache.json from a server-field whitelist, so log_prompts can't
    # live there. Read it from agent.config.json once and re-apply it after
    # each load_config() inside the loop.
    log_prompts = bool(agent_config.get("log_prompts"))
    config["log_prompts"] = log_prompts

    args.provider = args.provider or agent_config.get("provider") or "anthropic"
    valid_providers = set(DEFAULT_MODELS) | {
        "claude-code", "claude-code-agentic", "codex-agentic",
    }
    if args.provider not in valid_providers:
        sys.exit(f"Unknown provider: {args.provider}")
    is_agentic = args.provider in ("claude-code-agentic", "codex-agentic")
    args.model = args.model or agent_config.get("model")
    args.api_base = args.api_base or agent_config.get("api_base")
    args.compute = args.compute or agent_config.get("compute") or "local"
    args.hardware = args.hardware or agent_config.get("c3_hardware") or agent_config.get("hardware") or "l40"
    args.c3_time = args.c3_time or agent_config.get("c3_time") or "02:00:00"
    args.c3_provider = args.c3_provider or agent_config.get("c3_provider")
    args.env = args.env or agent_config.get("env")
    if args.env is None:
        args.env = agent_config.get("env_image") or agent_config.get("c3_image")
    if args.env is None:
        args.env = (
            agent_config.get("env_gpu") or agent_config.get("c3_gpu_image")
            if bool(config.get("is_gpu"))
            else agent_config.get("env_cpu") or agent_config.get("c3_cpu_image")
        )
    if args.compute not in ("local", "c3"):
        sys.exit(f"Unknown compute provider: {args.compute}")

    api_key = resolve_api_key(args.provider, args.api_key)
    model = args.model or DEFAULT_MODELS.get(args.provider, "")

    # server_url is materialized into agent.config.json by run_fleet from the
    # top-level fleet.config.json entry.
    server = (agent_config.get("server_url") or "").rstrip("/")
    if not server:
        sys.exit(
            "No server_url in agent.config.json. Did run_fleet.py spawn this "
            "worktree, or was agent.config.json hand-edited?"
        )
    if args.compute == "c3":
        if shutil.which("c3") is None:
            sys.exit("c3 CLI not found. Install it from https://docs.cthree.cloud/.")

    # Register or resume. agent.config.json is local-only, so it is safe to
    # persist the swarm agent id there for automatic restarts.
    configured_agent_id = agent_config.get("agent_id")
    configured_agent_name = agent_config.get("agent_name")
    if args.new_agent:
        configured_agent_id = None
        configured_agent_name = None

    if args.agent_id or configured_agent_id:
        agent_id = args.agent_id or configured_agent_id
        agent_name = args.agent_name or configured_agent_name or f"script-{agent_id[:8]}"
        # Validate before resuming. If the server doesn't have a row for
        # this id (DB reset/redeploy, switched swarms, or a first-run
        # interruption left a stale id locally), re-register with the
        # same display name so the contributor keeps their identity.
        # Multi-agent coordination keys off agent_id only — renaming or
        # re-registering one contributor is invisible to everyone else.
        if agent_exists(server, agent_id):
            print(f"Resuming agent: {agent_name} ({agent_id})")
        else:
            print(
                f"  [REGISTER] Stored agent_id {agent_id} not on server; "
                f"re-registering as {agent_name!r}…"
            )
            agent_id, agent_name = register_agent(
                server, provider=args.provider, model=model,
                requested_name=agent_name,
                name=agent_config.get("name"),
            )
            print(f"Re-registered as: {agent_name} ({agent_id})")
    else:
        agent_id, agent_name = register_agent(
            server, provider=args.provider, model=model,
            name=agent_config.get("name"),
        )
        print(f"Registered as: {agent_name} ({agent_id})")

    updated_agent_config = dict(agent_config)
    updated_agent_config.pop("c3_cloud_provider", None)
    updated_agent_config.pop("c3_no_build", None)
    updated_agent_config.pop("c3_image", None)
    updated_agent_config.pop("c3_cpu_image", None)
    updated_agent_config.pop("c3_gpu_image", None)
    updated_agent_config.pop("env_image", None)
    updated_agent_config.pop("env_cpu", None)
    updated_agent_config.pop("env_gpu", None)
    runtime_defaults = {
        "provider": args.provider,
        "model": args.model,
        "api_base": args.api_base,
        "compute": args.compute,
        "c3_hardware": args.hardware,
        "c3_time": args.c3_time,
        "c3_provider": args.c3_provider,
        "env": args.env,
    }
    for key, value in runtime_defaults.items():
        updated_agent_config.setdefault(key, value)
    updated_agent_config.update({
        "agent_id": agent_id,
        "agent_name": agent_name,
    })
    write_agent_config(updated_agent_config)

    challenge_md = read_challenge_md()

    # Agentic mode (claude-code-agentic): tooled headless Claude Code inside a
    # gitignored worktree, edits restricted by sandbox-settings.json. The
    # worktree persists across iterations (and across run_loop restarts) so
    # the cargo build cache survives. Set up once; the per-iteration
    # `backend.prepare(...)` refreshes CLAUDE.md / settings.json.
    backend: agentic_backends.AgenticBackend | None = None
    workdir: Path | None = None
    if is_agentic:
        backend = agentic_backends.get_backend(args.provider)
        workdir = agentic_sandbox.resolve_workdir(agent_id, agent_name)
        print(f"Agentic worktree: {workdir}")
        if shutil.which(backend.cli_name) is None:
            sys.exit(
                f"{backend.cli_name} CLI not found on PATH. Install it, or "
                f"switch to a non-agentic provider (e.g. --provider claude-code "
                f"for one-shot mode)."
            )

    print(f"Provider: {args.provider}  Model: {model}")
    compute_desc = f"c3/{args.hardware.lower()}" if args.compute == "c3" else args.compute
    if args.compute == "c3" and args.env:
        compute_desc += f" image={args.env}"
    print(f"Compute: {compute_desc}")
    print(f"Challenge: {config.get('challenge', '?')}")
    print(f"Server: {server}")
    print()

    iteration = 0
    while args.max_iterations == 0 or iteration < args.max_iterations:
        iteration += 1
        t_start = time.time()
        iter_input_tokens = 0
        iter_output_tokens = 0
        print(f"\n{'=' * 60}")
        print(f"  Iteration {iteration}  ({time.strftime('%H:%M:%S')})")
        print(f"{'=' * 60}")

        # ── Sync challenge ─────────────────────────────────────
        print("  [SYNC] Syncing challenge with server…")
        sync_challenge()
        config = load_config()
        config["log_prompts"] = log_prompts
        challenge_md = read_challenge_md()
        print(f"  [SYNC] Challenge: {config.get('challenge', '?')}  GPU: {config.get('is_gpu', False)}")

        try:
            swarm_cfg = server_get(f"{server}/api/swarm_config")
            config["available_challenges"] = swarm_cfg.get("available_challenges", {})
        except Exception:
            pass

        # ── Get state ──────────────────────────────────────────
        print("  [STATE] Fetching agent state…")
        try:
            state = get_state(server, agent_id)
        except Exception as e:
            print(f"  [STATE] FAILED: {e}")
            time.sleep(_ITERATION_BACKOFF_SECS)
            continue

        # If the agent's local `name` (from agent.config.json, materialized
        # from fleet.config.json) differs from the server's agents.name, POST
        # a rename. Cheap: piggybacks on the state we already fetched.
        try:
            from sync_identity import sync_identity_with_state
            renamed = sync_identity_with_state(server, agent_id, state)
            if renamed:
                agent_name = renamed
                print(f"  [IDENT] renamed to {agent_name!r}")
        except Exception as e:
            print(f"  [IDENT] sync skipped: {e}")

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

        # ── Write current best to disk ─────────────────────────
        best_code = state.get("best_algorithm_code") or ""
        best_kernel = state.get("best_kernel_code") or ""
        files = ChallengeFiles(config)
        bootstrap = is_stub_code(best_code)
        if best_code and not bootstrap:
            files.write(best_code, best_kernel)
            print(f"  [FILES] {files.describe_write(best_code, best_kernel)}")
            if files.is_gpu and not best_kernel:
                print(f"  [FILES] No kernel code from server — using local kernels.cu")

        if bootstrap:
            print("  [FILES] Starting from stub — will ask LLM to write initial implementation")

        if is_agentic:
            # ── Mode 2: tooled agent in sandboxed worktree ─────
            # Single tooled `claude -p` invocation replaces the entire
            # mode-1 sequence (hypothesis → code → compile-fix → runtime-fix
            # → redescribe). The agent decides its own hypothesis, edits the
            # algorithm file directly in the worktree, runs `cargo check`
            # itself, and writes .swarm/hypothesis.json before stopping.
            # Tokens aren't surfaced by the CLI so usage stays 0.
            assert backend is not None and workdir is not None
            hypothesis, code, new_kernel, _agentic_result = _run_agentic_iteration(
                args, state, config, server, agent_id, agent_name,
                workdir, backend, challenge_md, files,
            )
            tag = hypothesis.get("strategy_tag", "other")
            title = hypothesis.get("title", "untitled")
            print(f"  [AGENTIC] Hypothesis: [{tag}] {title}")

            if not code:
                print("  [AGENTIC] Agent left no algorithm file — restoring best")
                if best_code:
                    files.write(best_code, best_kernel)
                post_message(server, agent_name, agent_id,
                             f"[{tag}] {title} — agent produced no code")
                continue

            violation = validate_code(code)
            if violation:
                print(f"  [AGENTIC] Validation failed: {violation} — restoring best")
                if best_code:
                    files.write(best_code, best_kernel)
                post_message(server, agent_name, agent_id,
                             f"[{tag}] {title} — validation failed: {violation}")
                continue

            # Copy the worktree's edited code into the main checkout so the
            # official benchmark sees it. No compile-fix retry: the agent
            # ran `cargo check` itself before stopping. If the official
            # build still fails (e.g. feature-flag mismatch the agent
            # missed), we restore and continue without escalating.
            files.write(code, new_kernel)
            print(f"  [FILES] {files.describe_write(code, new_kernel)}")

            compute_label = f"C3/{args.hardware}" if args.compute == "c3" else "local Docker"
            print(f"  [BENCH] Running benchmark on {compute_label}…")
            send_heartbeat(server, agent_id)
            bench, build_err = run_benchmark(args, config, server)

            if bench is None:
                print(f"  [BENCH] FAILED — build_err: {build_err[:300]}")
                print(f"  [BENCH] Restoring previous code and continuing")
                if best_code:
                    files.write(best_code, best_kernel)
                post_message(server, agent_name, agent_id,
                             f"[{tag}] {title} — benchmark failed (build error?)")
                continue

            track_scores = bench.get("track_scores", {})
            errors = bench.get("errors") or []
            print(f"  [BENCH] Score: {bench.get('score', 0):.0f}  Feasible: {bench.get('feasible', False)}")
            if track_scores:
                for tk, ts in track_scores.items():
                    print(f"          Track {tk}: {ts:.0f}")
            if errors:
                print(f"  [BENCH] Errors ({len(errors)}):")
                for e in errors[:5]:
                    print(f"          {e}")
        else:
            # ── Mode 1: single-shot LLM completion ─────────────
            # ── LLM hypothesis ─────────────────────────────────
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
                hyp_response, hyp_usage = _call_llm_logged(
                    "hypothesis", config,
                    args.provider, model, api_key,
                    build_hypothesis_system_prompt(challenge_md, config, is_bootstrap=bootstrap),
                    build_hypothesis_user_prompt(state, config),
                    args.api_base,
                )
                iter_input_tokens += hyp_usage["input_tokens"]
                iter_output_tokens += hyp_usage["output_tokens"]
            except Exception as e:
                print(f"  [LLM] HYPOTHESIS FAILED: {e}")
                post_message(server, agent_name, agent_id,
                             f"LLM call failed: {type(e).__name__}")
                time.sleep(_ITERATION_BACKOFF_SECS)
                continue

            hypothesis = parse_hypothesis(hyp_response)
            tag = hypothesis.get("strategy_tag", "?")
            title = hypothesis.get("title", "?")
            desc = hypothesis.get("description", "")
            print(f"  [LLM] Hypothesis: [{tag}] {title}")
            if desc:
                print(f"         {desc[:120]}")

            # ── LLM code generation ────────────────────────────
            code, new_kernel, gen_in, gen_out = _generate_code(
                args, model, api_key, state, hypothesis, config,
                challenge_md, files,
            )
            iter_input_tokens += gen_in
            iter_output_tokens += gen_out

            if not code:
                print(f"  [SKIP] No valid code produced — skipping to next iteration")
                continue

            # ── Code similarity check ──────────────────────────
            if best_code:
                sim = difflib.SequenceMatcher(None, best_code, code).ratio()
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

            files.write(code, new_kernel)
            print(f"  [FILES] {files.describe_write(code, new_kernel)}")

            # ── Benchmark with compile-error retry ─────────────
            compute_label = f"C3/{args.hardware}" if args.compute == "c3" else "local Docker"
            print(f"  [BENCH] Running benchmark on {compute_label}…")
            post_message(server, agent_name, agent_id, f"Trying [{tag}] {title}")
            send_heartbeat(server, agent_id)

            bench, build_err, code_changed, fix_in, fix_out = _benchmark_with_compile_fix(
                args, model, api_key, config, server, challenge_md,
                files,
            )
            iter_input_tokens += fix_in
            iter_output_tokens += fix_out

            if bench is None:
                print(f"  [BENCH] FAILED — build_err: {build_err[:300]}")
                print(f"  [BENCH] Restoring previous code and continuing")
                if best_code:
                    files.write(best_code, best_kernel)
                post_message(server, agent_name, agent_id,
                             f"[{tag}] {title} — benchmark failed (build error?)")
                continue

            track_scores = bench.get("track_scores", {})
            errors = bench.get("errors") or []
            print(f"  [BENCH] Score: {bench.get('score', 0):.0f}  Feasible: {bench.get('feasible', False)}")
            if track_scores:
                for tk, ts in track_scores.items():
                    print(f"          Track {tk}: {ts:.0f}")
            if errors:
                print(f"  [BENCH] Errors ({len(errors)}):")
                for e in errors[:5]:
                    print(f"          {e}")

            # ── Runtime error retry ────────────────────────────
            runtime_errors = bench.get("errors") or []
            if runtime_errors and not bench.get("feasible"):
                bench, rt_changed, rt_in, rt_out = _fix_runtime_errors(
                    args, model, api_key, config, server, agent_id, challenge_md,
                    files, bench, best_code, best_kernel,
                )
                iter_input_tokens += rt_in
                iter_output_tokens += rt_out
                code_changed = code_changed or rt_changed

            if bench is None:
                post_message(server, agent_name, agent_id,
                             f"[{tag}] {title} — benchmark failed after runtime fix")
                continue

            # ── Re-describe hypothesis if code changed ─────────
            # Skip when the post-recovery code is nearly identical to what
            # we originally proposed — the recovery was almost certainly
            # cosmetic and not worth a round-trip to confirm "no change".
            final_code, final_kernel = files.read()
            post_fix_similarity = difflib.SequenceMatcher(None, code, final_code).ratio()
            if code_changed and post_fix_similarity < _REDESCRIBE_SIMILARITY_THRESHOLD:
                print(
                    f"  Code changed during error recovery "
                    f"(post-fix similarity {post_fix_similarity * 100:.0f}%) — re-describing hypothesis ..."
                )
                try:
                    redesc_response, redesc_usage = _call_llm_logged(
                        "redescribe", config,
                        args.provider, model, api_key,
                        build_redescribe_system_prompt(config),
                        build_redescribe_hypothesis_prompt(
                            best_code or "", final_code, hypothesis,
                            original_kernel=best_kernel or "",
                            final_kernel=final_kernel,
                        ),
                        args.api_base,
                    )
                    iter_input_tokens += redesc_usage["input_tokens"]
                    iter_output_tokens += redesc_usage["output_tokens"]
                    updated = parse_hypothesis(redesc_response)
                    print(f"  Updated hypothesis: [{updated.get('strategy_tag', '?')}] {updated.get('title', '?')}")
                    hypothesis = updated
                    tag = hypothesis.get("strategy_tag", "?")
                    title = hypothesis.get("title", "?")
                except Exception as e:
                    print(f"  Re-describe failed: {e} — using original hypothesis", file=sys.stderr)

        # ── Publish ────────────────────────────────────────────
        iter_cost = estimate_cost(model, {
            "input_tokens": iter_input_tokens,
            "output_tokens": iter_output_tokens,
        })
        print(f"  [TOKENS] in={iter_input_tokens:,}  out={iter_output_tokens:,}  est=${iter_cost:.4f}")
        print(f"  [PUBLISH] Publishing results to server…")
        is_new_best = False
        try:
            result = publish_results(
                server, agent_id, bench, hypothesis, config,
                input_tokens=iter_input_tokens,
                output_tokens=iter_output_tokens,
                estimated_cost=iter_cost,
            )
            is_new_best = result.get("is_new_best", False)
            if is_new_best:
                print("  [PUBLISH] ** NEW PERSONAL BEST! **")
            else:
                print(f"  [PUBLISH] Recorded (not a new best)")
        except Exception as e:
            print(f"  [PUBLISH] FAILED: {e}")

        status = "NEW BEST!" if is_new_best else f"score {bench.get('score', 0):.0f}"
        feasible_str = "" if bench.get("feasible") else " (INFEASIBLE)"
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
