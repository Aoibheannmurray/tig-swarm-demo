#!/usr/bin/env python3
"""Standalone swarm optimization loop — no agent required.

Handles server communication, prompt construction, LLM-based code mutation,
benchmarking, and result publishing.  Works with any LLM provider (Anthropic,
OpenAI, Google) or any OpenAI-compatible endpoint via --api-base.

Usage:
    python setup.py
    export ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY / GOOGLE_API_KEY
    # Windows: set ANTHROPIC_API_KEY=sk-...  (cmd)  /  $env:ANTHROPIC_API_KEY="sk-..."  (PowerShell)
    python scripts/run_loop.py

    # Overrides still work:
    python scripts/run_loop.py --provider openai --model gpt-4o
    python scripts/run_loop.py --provider google --model gemini-2.5-pro
    python scripts/run_loop.py --provider openai --api-base https://api.together.xyz
    python scripts/run_loop.py --provider anthropic --compute c3 --hardware auto
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
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_CONFIG_PATH = ROOT / "agent.config.json"
sys.path.insert(0, str(ROOT / "scripts"))

# Windows console crashes on box-drawing / ellipsis characters this script
# prints when the active code page isn't UTF-8. Force UTF-8 with replacement
# so contributors don't have to remember `python -X utf8`.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _read_json(path: Path) -> dict:
    """Tolerant read of a JSON config file.

    `utf-8-sig` strips a UTF-8 BOM if PowerShell `Set-Content` wrote one — a
    common pitfall on Windows where strict `json.loads` then fails with
    "Expecting value: line 1 column 1"."""
    return json.loads(path.read_text(encoding="utf-8-sig"))

from llm_backends import DEFAULT_MODELS, call_llm, estimate_cost

import challenge_files
import cleaner_prepass
from challenge_files import (
    ChallengeFiles,
    ensure_challenge_import,
    ensure_common_imports,
    is_stub_code,
    read_challenge_md,
    validate_code,
)
from swarm_client import (
    AgentTokenRevoked,
    agent_exists,
    get_state,
    post_message,
    publish_results,
    register_agent,
    send_heartbeat,
    server_get,
    validate_agent_token,
)
from prompts import (
    build_agentic_user_prompt,
    build_code_system_prompt,
    build_code_user_prompt,
    build_compile_fix_prompt,
    build_compile_fix_system_prompt,
    build_hypothesis_system_prompt,
    build_hypothesis_user_prompt,
    build_redescribe_hypothesis_prompt,
    build_redescribe_system_prompt,
    build_runtime_fix_prompt,
    build_search_replace_repair_prompt,
    build_search_replace_system_prompt,
    build_search_replace_user_prompt,
    build_tacit_distillation_prompts,
    parse_hypothesis,
    parse_tacit_distillation,
)
import prompts as _prompts
import agentic_backends
import agentic_sandbox
import search_replace
import hpo
from c3_compute import run_benchmark_c3

# Backoff after a recoverable iteration-level failure (state fetch, LLM error).
_ITERATION_BACKOFF_SECS = 5


def _validate_entry(entry_code: str, config: dict, files) -> str | None:
    """`validate_code` with the algorithm's full file bundle in view.

    A multi-file algorithm may define `solve_challenge` in a submodule and
    re-export it from the entry (mainnet's shape for large algorithms). Judged
    on the entry file alone that reads as "solve_challenge missing", which
    would reject every edit to an otherwise valid algorithm. The on-disk
    bundle supplies the sibling modules; `entry_code` is the candidate entry,
    which may not be written yet."""
    try:
        bundle = dict(files.read_files())
    except Exception:
        bundle = {}
    bundle[files.entry_name] = entry_code
    return validate_code(entry_code, config, files=bundle)


def _normalize_role(value: object) -> str:
    """Map a config/state `role` value to 'exploiter' or 'explorer' (default).

    Role is contributor-owned (an optional field in fleet.config.json / the
    worktree's agent.config.json) and re-read every iteration so edits take
    effect live. Anything unrecognized is an explorer — today's behavior."""
    return "exploiter" if str(value or "").strip().lower() == "exploiter" else "explorer"


def _normalize_seeded_start(value: object) -> bool | None:
    """Map a config `seeded_start` value to True / False / None (= auto).

    Contributor-owned like `role` (fleet.config.json, hot-reloads) and re-read
    every iteration. True forces fresh trajectories to start from working code
    (seed pool → best peer → stub), False forces the bare stub, absent/None
    leaves the server's tier/role policy in charge. JSON booleans arrive as
    bool; be forgiving about "true"/"false" strings too."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None
# Skip the LLM re-describe call when the post-fix code is this similar to
# the pre-fix code — the fix was almost certainly cosmetic (bounds checks,
# error wrappers) and not worth a round-trip to confirm "no change".
_REDESCRIBE_SIMILARITY_THRESHOLD = 0.95

# Tacit-knowledge distillation
# ────────────────────────────
# The driver fires a distillation LLM call after the iteration that's
# about to trigger a trajectory reset (stagnation == stagnation_limit - 1,
# attempt didn't improve). The single switch for whether this runs for
# agentic providers lives in `prompts.DRIVER_DISTILL_FOR_AGENTIC`; flip
# it there and both the in-band prompt block (built in
# `build_agentic_user_prompt`) and the driver-side gate below stay in
# sync automatically.
_AGENTIC_PROVIDERS = ("claude-code-agentic", "codex-agentic")


def tacit_write_enabled(config: dict) -> bool:
    """Per-agent kill switch for tacit-knowledge writing. Defaults to True
    (write enabled) when `tacit_write` is absent, so existing configs keep
    their current behavior. Accepts JSON booleans as well as the string
    forms "false"/"0"/"no"/"off" (case-insensitive) for env-style configs."""
    value = config.get("tacit_write", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)

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
                f"## RESPONSE\n\n{response}\n",
                encoding="utf-8",
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
    return _read_json(cfg_path)


def load_agent_config() -> dict:
    if not AGENT_CONFIG_PATH.exists():
        return {}
    try:
        data = _read_json(AGENT_CONFIG_PATH)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # Malformed JSON (hand-edit typo, torn write): warn loudly but keep
        # the loop alive on defaults — identity/provider fall back to the
        # registration flow rather than crashing mid-run.
        print(
            f"  [WARN] {AGENT_CONFIG_PATH} is unreadable ({e}) — "
            f"ignoring it and continuing with defaults. Fix the JSON to "
            f"restore the persisted identity/provider settings.",
            file=sys.stderr,
        )
        return {}


def write_agent_config(config: dict) -> None:
    # Atomic tmp-file + os.replace: agent.config.json is also read/written by
    # run_fleet's hot-reload monitor, so a plain write_text could be observed
    # torn (or interleave with the monitor's own write).
    tmp = AGENT_CONFIG_PATH.with_name(AGENT_CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(tmp, AGENT_CONFIG_PATH)


def _one_line_identity_part(value: object) -> str:
    return " ".join(str(value or "").split())


def _compose_tig_user_id(username: str, agent_id: str) -> str:
    username = _one_line_identity_part(username)
    agent_id = _one_line_identity_part(agent_id)
    if username and agent_id:
        return f"{username} (agent {agent_id})"
    if username:
        return username
    if agent_id:
        return f"agent {agent_id}"
    return "unknown"


def _attach_benchmark_identity(
    config: dict, username: str, agent_id: str,
    agent_token: str | None = None,
) -> str:
    """Stamp the agent's identity onto `config` for the benchmark path.

    `config` comes from .swarm-cache.json (load_config), which carries NO
    identity — but run_benchmark reads `agent_id`/`agent_token` off it to
    start the mid-benchmark heartbeat thread. Before these two keys were
    stamped here, that guard was never true and long benchmarks ran silent:
    the server's inactive_minutes sweep reaped the trajectory mid-benchmark
    and every publish landed on a fresh one (the vrp-swarm fable004 churn).
    """
    tig_user_id = _compose_tig_user_id(username, agent_id)
    config["tig_user_id"] = tig_user_id
    config["agent_id"] = agent_id
    if agent_token:
        config["agent_token"] = agent_token
    os.environ["TIG_USER_ID"] = tig_user_id
    return tig_user_id


def sync_challenge() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "setup.py"), "sync"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[-500:]
        print(f"  [SYNC] WARNING: setup.py sync failed ({result.returncode}): {err}", file=sys.stderr)


# ── Benchmark dispatch ─────────────────────────────────────────────


def _run_benchmark_local(
    seed: str | None = None, hyperparameters: str | None = None, job_slots=None,
) -> tuple[dict | None, str]:
    env = os.environ.copy()
    if seed is not None:
        env["TIG_BENCH_SEED"] = seed
    if hyperparameters is not None:
        env["TIG_HYPERPARAMETERS"] = hyperparameters
    if job_slots is not None:
        job_slots.acquire()
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "benchmark.py")],
            capture_output=True, text=True, cwd=ROOT,
            encoding="utf-8", errors="replace",
            env=env,
        )
    finally:
        if job_slots is not None:
            job_slots.release()
    if result.returncode != 0:
        err = result.stderr or result.stdout or "Benchmark failed"
        print(f"  Benchmark failed:\n{err[-2000:]}", file=sys.stderr)
        return None, err
    # The GPU toolchain image (nvidia/cuda) prints a "== CUDA ==" banner to
    # stdout via its entrypoint, ahead of benchmark.py's JSON. Try a clean
    # parse first, then fall back to slicing out the JSON object (first "{"
    # to last "}") so that banner — which contains no braces — is ignored.
    raw = result.stdout or ""
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1]), ""
        except json.JSONDecodeError:
            pass
    print(f"  Benchmark output not valid JSON:\n{raw[:300]}", file=sys.stderr)
    return None, "Benchmark output was not valid JSON"


_BENCH_HEARTBEAT_INTERVAL_S = 300


def run_benchmark(
    args: argparse.Namespace, config: dict, server: str,
    seed: str | None = None, hyperparameters: str | None = None, job_slots=None,
) -> tuple[dict | None, str]:
    """Run a benchmark on the configured compute provider.

    `seed` overrides the instance seed (the hyperparameter search uses a
    non-test seed). `hyperparameters` is a JSON string forwarded to the solver
    as --hyperparameters; None means the solver uses its in-code defaults (the
    "default score").

    A benchmark can block for well past the server's `inactive_minutes`
    trajectory TTL (many instances x a multi-minute timeout each), so a
    background heartbeat keeps `last_active_at` fresh for the duration —
    otherwise the server reaps the trajectory mid-benchmark and the publish
    lands on a fresh one.
    """
    agent_id = config.get("agent_id")
    agent_token = config.get("agent_token")
    hb_stop = None
    if agent_id and agent_token:
        hb_stop = _start_heartbeat_thread(
            server, agent_id, agent_token,
            interval_s=_BENCH_HEARTBEAT_INTERVAL_S, label="BENCH",
        )
    else:
        # _attach_benchmark_identity stamps both keys every iteration; if
        # they're missing the trajectory WILL be reaped once the benchmark
        # outlives inactive_minutes. Say so instead of failing silently.
        print("  [BENCH] WARNING: no agent identity on config — heartbeats "
              "disabled for this benchmark (trajectory may be reaped)")
    try:
        if args.compute == "local":
            return _run_benchmark_local(seed, hyperparameters, job_slots=job_slots)
        if args.compute == "c3":
            # C3 self-gates on the fleet-wide FCFS slot pool (c3_pool.py), so it
            # ignores the local-only job_slots semaphore.
            return run_benchmark_c3(
                args, config, server, seed=seed, hyperparameters=hyperparameters,
            )
        return None, f"Unknown compute provider: {args.compute}"
    finally:
        if hb_stop is not None:
            hb_stop.set()


# ── Extracted iteration helpers ────────────────────────────────────


def _use_search_replace(role: str, file_map: dict, config: dict) -> bool:
    """Whether this API-mode iteration should edit via search/replace rather
    than full-file replacement. True when any of:
      - exploiter role (exploiters always make localized search/replace edits),
      - the algorithm spans multiple files (whole-file rewrites are wasteful /
        often impossible within token limits),
      - the agent opted in via `edit_mode: search_replace` in its config.
    Single-file explorers default to full-file replacement (edit_mode 'full').

    Never used when there's nothing concrete to edit (empty / stub algorithm):
    a bootstrap must full-write a complete implementation, not patch a stub.
    """
    if not isinstance(file_map, dict) or not file_map:
        return False
    # A lone stub file can't be search/replaced — that's a bootstrap.
    if len(file_map) == 1 and is_stub_code(next(iter(file_map.values()))):
        return False
    if role == "exploiter":
        return True
    if len(file_map) > 1:
        return True
    return str(config.get("edit_mode") or "").strip().lower() == "search_replace"


# Bounded LLM repair rounds for search/replace blocks that don't match. After
# these, any still-unmatched blocks are skipped (the compile-fix loop catches
# anything that breaks the build).
_SR_REPAIR_ROUNDS = 2

# Consecutive no-edit search/replace skips before the loop forces a full rewrite
# to break the stall (see the main loop). Keeps occasional S/R misses cheap while
# stopping a plateaued agent from spinning forever without ever publishing.
_SR_SKIP_FALLBACK = 3

# Char budget for the algorithm files inlined into a search/replace prompt.
# Multi-file algorithms can grow far past any model's context window (a 2.5MB
# six-file map produced a ~1M-token prompt that every provider rejects, which
# then stalls the agent into the risky full-rewrite fallback). When the map
# exceeds the budget, show the entry file plus the files the hypothesis
# actually targets and name the rest without contents. ~600k chars ≈ 150k
# tokens — safely inside claude-code's 1M-token request limit even with its
# ~400k tokens of system/tool overhead, and inside a 200k-token API context.
_SR_PROMPT_CHAR_BUDGET = 600_000


# ── Cleaner: deterministic bloat reduction (docs/cleaner-agent-plan.md) ──
#
# When the trajectory best outgrows `cleaner_trigger_chars`, one iteration is
# spent running the Tier-0 pre-pass (cleaner_prepass.py — duplicate-file merge
# + unreachable-file removal, no LLM), benchmarking the result, and — if the
# score sits within `cleaner_score_delta_pct` of the parent and the size
# dropped to ≤ `cleaner_target_pct` of the original — publishing it as an
# `iteration_type="refactor"`: the server swaps in the lean code but keeps the
# parent's score (no ratchet erosion), counting neither improvement nor
# stagnation. All knobs are host-tunable via fleet.config.json.
_CLEANER_TRIGGER_CHARS = 500_000
_CLEANER_TARGET_PCT = 60
_CLEANER_SCORE_DELTA_PCT = 2.0
_CLEANER_COOLDOWN_ITERS = 15
# Above this fraction of the trigger, prompts get a "prefer size-reducing
# edits" warning — bloat prevented is a benchmark never spent.
_CLEANER_WARN_FRACTION = 0.8


# ── Benchmark-failure freeze guard ──
#
# Consecutive iterations that spent LLM/agent effort but ended WITHOUT a
# successful benchmark before the agent freezes (posts one feed message and
# exits). Catches a broken benchmark path — C3 outage, exhausted C3 credit,
# broken Docker — that would otherwise burn API tokens every iteration
# producing code that never gets scored. Only token-spending failures count:
# pure no-token skips (server unreachable, LLM transport errors, an exploiter
# idling for a seed) don't, so a brief server or provider outage can't kill
# the fleet. The counter also resets on a challenge switch. Host-tunable via
# `no_benchmark_freeze_limit` in fleet.config.json (top-level = fleet-wide,
# per-agent overrides); 0 disables. Unfreeze = fix the benchmark path and
# restart the fleet/agent.
_NO_BENCHMARK_FREEZE_LIMIT = 10


def _freeze_limit(config: dict) -> int:
    """Resolve `no_benchmark_freeze_limit` from config (0 disables)."""
    try:
        return max(0, int(config.get("no_benchmark_freeze_limit",
                                     _NO_BENCHMARK_FREEZE_LIMIT)))
    except (TypeError, ValueError):
        return _NO_BENCHMARK_FREEZE_LIMIT


def _cleaner_size_warning(file_map: dict, config: dict) -> str:
    """One-line prompt warning when the algorithm nears the cleaner trigger."""
    total = sum(len(v) for v in file_map.values())
    trigger = int(config.get("cleaner_trigger_chars", _CLEANER_TRIGGER_CHARS))
    if total <= trigger * _CLEANER_WARN_FRACTION:
        return ""
    return (
        f"\nNOTE: this algorithm is {total} chars of source — approaching the "
        f"size limit ({trigger}). Prefer edits that REDUCE duplication; never "
        f"clone a module or file to create a variant."
    )


def _score_within_delta(
    direction: str, score: float, parent: float, delta_pct: float,
) -> bool:
    """Direction-aware 'refactor kept the score' check. The delta is a noise
    allowance for time-budgeted anytime solvers, not a quality budget."""
    d = abs(delta_pct) / 100.0
    if direction == "min":
        return score <= parent * (1 + d) if parent >= 0 else score <= parent * (1 - d)
    return score >= parent * (1 - d) if parent >= 0 else score >= parent * (1 + d)


def _run_cleaner_iteration(
    args: argparse.Namespace, config: dict, server: str,
    files: ChallengeFiles, state: dict,
    agent_id: str, agent_token: str | None, role: str,
) -> bool:
    """One cleaner iteration: pre-pass → benchmark → delta gate → publish.

    Returns True if a refactor was published (the caller `continue`s); False
    when there was nothing to clean or the gate rejected — the original files
    are restored and the caller proceeds with a normal iteration.
    """
    file_map = files.read_files()
    old_size = cleaner_prepass.total_chars(file_map)
    entry = challenge_files.entry_name(config)
    new_map, actions = cleaner_prepass.run_prepass(file_map, entry)
    new_size = cleaner_prepass.total_chars(new_map)
    target_pct = float(config.get("cleaner_target_pct", _CLEANER_TARGET_PCT))
    if not actions:
        print("  [CLEANER] pre-pass found nothing mechanical to remove — skipping")
        return False
    if new_size > old_size * target_pct / 100.0:
        print(f"  [CLEANER] pre-pass only reached {new_size}/{old_size} chars "
              f"(target ≤{target_pct:.0f}%) — not worth a benchmark, skipping")
        return False

    parent = _clean_score(state.get("current_trajectory_best"))
    if parent is None:
        print("  [CLEANER] no parent trajectory score to compare against — skipping")
        return False

    print(f"  [CLEANER] pre-pass: {old_size} → {new_size} chars "
          f"({len(actions)} action(s)):")
    for a in actions:
        print(f"  [CLEANER]   - {a}")
    files.write_files(new_map)
    print("  [CLEANER] benchmarking the lean code…")
    bench, err = run_benchmark(args, config, server)

    delta_pct = float(config.get("cleaner_score_delta_pct", _CLEANER_SCORE_DELTA_PCT))
    direction = str(config.get("scoring_direction", "max"))
    score = (bench or {}).get("score")
    ok = (
        bench is not None
        and bench.get("feasible", False)
        and score is not None
        and _score_within_delta(direction, score, parent, delta_pct)
    )
    if not ok:
        why = (f"benchmark failed: {err[:200]}" if bench is None
               else f"score {score} outside ±{delta_pct}% of parent {parent} "
                    f"(or infeasible)")
        print(f"  [CLEANER] REJECTED — {why}; restoring original files")
        files.write_files(file_map)
        return False

    print(f"  [CLEANER] ACCEPTED — score {score:.0f} within {delta_pct}% of "
          f"parent {parent:.0f}; publishing refactor")
    hyp = {
        "title": f"refactor: bloat reduction {old_size//1000}k → {new_size//1000}k chars",
        "description": "Deterministic cleaner pre-pass (no LLM): " + "; ".join(actions),
        "strategy_tag": "other",
        "notes": "behavior-preserving; server keeps the parent score",
    }
    try:
        publish_results(
            server, agent_id, bench, hyp, config,
            agent_token=agent_token, role=role, iteration_type="refactor",
        )
    except Exception as e:
        print(f"  [CLEANER] publish FAILED: {e} — restoring original files")
        files.write_files(file_map)
        return False
    return True


def _sr_prompt_file_subset(
    file_map: dict, hypothesis: dict, config: dict,
) -> tuple[dict, list[str]]:
    """Choose which files to inline in the S/R prompt, under a char budget.

    Returns (subset_map, omitted_names). The whole map is returned when it
    fits. Otherwise: the entry file is always shown; files whose name (or
    "t<NN>" shorthand, e.g. "T48" for track_t48.rs) appears in the hypothesis
    are shown next — at least one even if it alone busts the budget, since the
    model cannot edit a file it cannot see; any remaining room is filled
    smallest-file-first.
    """
    budget = int(config.get("sr_prompt_char_budget", _SR_PROMPT_CHAR_BUDGET))
    if sum(len(v) for v in file_map.values()) <= budget:
        return dict(file_map), []

    hyp_text = (
        f"{hypothesis.get('title', '')} {hypothesis.get('description', '')}"
    ).lower()

    def _mentioned(name: str) -> bool:
        stem = Path(name).stem.lower()
        if stem in hyp_text:
            return True
        m = re.search(r"(\d+)$", stem)
        return bool(m) and bool(re.search(rf"\bt{m.group(1)}\b", hyp_text))

    entry = challenge_files.entry_name(config)
    keep: dict = {}
    used = 0
    if entry in file_map:
        keep[entry] = file_map[entry]
        used += len(file_map[entry])
    for name in sorted(n for n in file_map if n not in keep and _mentioned(n)):
        # Guarantee at least one hypothesis-targeted file is visible.
        if used + len(file_map[name]) <= budget or len(keep) <= 1:
            keep[name] = file_map[name]
            used += len(file_map[name])
    for name, code in sorted(file_map.items(), key=lambda kv: len(kv[1])):
        if name not in keep and used + len(code) <= budget:
            keep[name] = code
            used += len(code)
    return keep, [n for n in file_map if n not in keep]


def _generate_code_search_replace(
    args: argparse.Namespace, model: str, api_key: str,
    state: dict, hypothesis: dict, config: dict,
    challenge_md: str, files: ChallengeFiles,
    *, role: str,
) -> tuple[str | None, str | None, int, int]:
    """Mutate the algorithm with soft SEARCH/REPLACE edits.

    Reads the current files-map from disk (seeded with the best at loop top),
    asks the model for blocks, applies them (fuzzy match), runs a bounded repair
    pass on misses, skips whatever still won't match, then writes the edited map
    back to disk. Returns (entry_code, kernel, input_tokens, output_tokens).
    """
    input_tokens = output_tokens = 0
    file_map = files.read_files()
    if not file_map:
        print("  [SR] No files on disk to edit — skipping")
        return None, None, 0, 0

    prompt_map, omitted = _sr_prompt_file_subset(file_map, hypothesis, config)
    if omitted:
        shown_chars = sum(len(v) for v in prompt_map.values())
        print(f"  [SR] prompt budget: inlining {len(prompt_map)}/{len(file_map)} "
              f"files ({shown_chars} chars); omitted: {', '.join(omitted)}")

    system = build_search_replace_system_prompt(challenge_md, config, role=role)
    user = build_search_replace_user_prompt(
        prompt_map, hypothesis, config, role=role, omitted=omitted,
    ) + _cleaner_size_warning(file_map, config)
    print(f"  [SR] Generating search/replace edits via {args.provider}/{model}…")

    applied_any = False
    for round_i in range(_SR_REPAIR_ROUNDS + 1):
        try:
            response, usage = _call_llm_logged(
                "code", config, args.provider, model, api_key, system, user, args.api_base,
            )
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
        except Exception as e:
            print(f"  [SR] generation failed: {e}")
            break

        blocks = search_replace.parse_blocks(response)
        if not blocks:
            print("  [SR] model returned no search/replace blocks")
            break

        file_map, misses = search_replace.apply_blocks(file_map, blocks)
        applied = len(blocks) - len(misses)
        applied_any = applied_any or applied > 0
        print(f"  [SR] applied {applied}/{len(blocks)} blocks"
              + (f", {len(misses)} unmatched" if misses else ""))
        if not misses:
            break
        if round_i < _SR_REPAIR_ROUNDS:
            print(f"  [SR] repair round {round_i + 1}/{_SR_REPAIR_ROUNDS}…")
            # Same budget subset, re-read from the post-apply map so the
            # repair sees applied edits without re-inlining omitted files.
            user = build_search_replace_repair_prompt(
                {k: file_map[k] for k in prompt_map if k in file_map},
                search_replace.format_misses(misses), config
            )
        else:
            print(f"  [SR] skipping {len(misses)} still-unmatched block(s)")

    if not applied_any:
        print("  [SR] no edits applied — skipping iteration")
        return None, None, input_tokens, output_tokens

    entry_code = ensure_common_imports(ensure_challenge_import(
        file_map.get(files.entry_name, ""), config["challenge"]
    ))
    file_map[files.entry_name] = entry_code
    violation = validate_code(entry_code, config, files=file_map)
    if violation:
        print(f"  [SR] validation failed after edits: {violation} — skipping")
        return None, None, input_tokens, output_tokens

    files.write_files(file_map)
    kernel_name = Path(config["kernel_path"]).name if config.get("kernel_path") else ""
    kernel = file_map.get(kernel_name, "") if kernel_name else ""
    print(f"  [SR] wrote {len(file_map)} file(s)")
    return entry_code, kernel, input_tokens, output_tokens


def _generate_code(
    args: argparse.Namespace, model: str, api_key: str,
    state: dict, hypothesis: dict, config: dict,
    challenge_md: str, files: ChallengeFiles,
    *, role: str = "explorer", force_full: bool = False,
) -> tuple[str | None, str | None, int, int]:
    """LLM code generation with retry on validation failure.

    Role only steers the prompt guidance (explorer vs exploiter); it no longer
    gates the candidate on similarity to the starting code. Exploiters,
    multi-file algorithms, and `edit_mode: search_replace` agents go through the
    soft search/replace path; everyone else does full-file replacement.

    `force_full=True` bypasses the search/replace path and always does a full
    rewrite — the loop uses this to break out of a run of no-edit S/R skips (the
    model kept returning no blocks), so the agent produces *something*, publishes,
    and advances the server-side stagnation reset instead of spinning forever.

    Returns (code, kernel, input_tokens, output_tokens).
    """
    if not force_full and _use_search_replace(role, files.read_files(), config):
        return _generate_code_search_replace(
            args, model, api_key, state, hypothesis, config,
            challenge_md, files, role=role,
        )

    input_tokens = 0
    output_tokens = 0
    max_attempts = 3
    violation = ""

    for attempt in range(max_attempts):
        if attempt == 0:
            print(f"  [LLM] Generating code via {args.provider}/{model}…")
            user_prompt = build_code_user_prompt(state, hypothesis, config, role=role)
        else:
            print(f"  [LLM] Code retry {attempt}/{max_attempts - 1}: {violation}")
            user_prompt = (
                build_code_user_prompt(state, hypothesis, config, role=role)
                + f"\n\nYour previous response was rejected: {violation}\n"
                "Fix the issue and return the complete source."
                + files.separator_suffix()
            )
        try:
            code_response, usage = _call_llm_logged(
                "code", config,
                args.provider, model, api_key,
                build_code_system_prompt(challenge_md, config, role=role),
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

        violation = _validate_entry(parsed, config, files)
        if violation:
            print(f"  [LLM] Validation failed: {violation}")
            continue

        print(f"  [LLM] Code validated OK")
        return (ensure_common_imports(parsed), parsed_kernel,
                input_tokens, output_tokens)

    return None, None, input_tokens, output_tokens


def _clean_score(x):
    """Snap floating-point noise near zero to a clean 0.0 for display.

    A baseline-matching solution should score 0, but the shifted-geomean
    round-trip (and any value the server persisted before that was fixed) can
    land it at ~-3.7e-09. Anything below the meaningful integer-scaled
    precision is noise; also normalises -0.0 -> 0.0 so `:.0f` prints "0",
    never "-0". Passes None through unchanged (score not yet known).
    """
    if x is None:
        return x
    return 0.0 if abs(x) < 1e-6 else x


def _print_bench_result(bench: dict, indent: str = "  ") -> None:
    """Print the benchmark score with per-track context.

    A failed/infeasible track injects a large fixed penalty into a shifted
    geometric mean, so one bad track can drag the aggregate negative. Tracks
    below baseline are flagged inline; what a negative aggregate means is
    documented once in README.md ("Reading the score") rather than reprinted
    every iteration. ASCII-only on purpose so the line itself can't trip a
    non-UTF-8 Windows console.
    """
    score = _clean_score(bench.get("score", 0))
    feasible = bench.get("feasible", False)
    track_scores = bench.get("track_scores", {})
    errors = bench.get("errors") or []
    print(f"{indent}[BENCH] Score: {score:.0f}  Feasible: {feasible}")
    if track_scores:
        for tk, ts in track_scores.items():
            ts = _clean_score(ts)
            note = "  (below baseline)" if ts < 0 else ""
            print(f"{indent}        Track {tk}: {ts:.0f}{note}")
    if errors:
        print(f"{indent}[BENCH] Errors ({len(errors)}):")
        for e in errors[:5]:
            print(f"{indent}        {e}")


def _try_compile_fix(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict,
    files: ChallengeFiles,
    build_err: str,
) -> tuple[bool, int, int]:
    """Ask the LLM to fix compiler errors, write the result.

    Uses a focused fix prompt (minimal-edit instruction + distilled errors)
    rather than the full code-generation prompt — see prompts.py.

    Returns (success, input_tokens, output_tokens).
    """
    code, kernel = files.read()
    fix_prompt = build_compile_fix_prompt(code, kernel, build_err, files.is_gpu)
    try:
        fix_response, usage = _call_llm_logged(
            "compile_fix", config,
            args.provider, model, api_key,
            build_compile_fix_system_prompt(config),
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

    violation = _validate_entry(fixed, config, files)
    if violation:
        print(f"  Fix failed validation: {violation}")
        return False, usage["input_tokens"], usage["output_tokens"]

    before_fix, before_kernel = files.read()
    # Abort ONLY on a byte-identical echo (the model returned the broken code
    # unchanged). A ratio threshold would mis-fire here: a legitimate one-line
    # fix in a long file is ~99.7% similar, so `sim >= 0.99` used to reject
    # exactly the small, correct fixes we want. The similarity is kept purely
    # as a diagnostic readout.
    if fixed == before_fix and (fixed_kernel or "") == (before_kernel or ""):
        print("  Fix returned the broken code unchanged (no-op) — aborting retry.")
        return False, usage["input_tokens"], usage["output_tokens"]
    sim = difflib.SequenceMatcher(None, before_fix, fixed).ratio()
    print(f"  Fix changed the code (similarity to broken: {sim * 100:.1f}%) — re-benchmarking.")
    files.write(fixed, fixed_kernel)
    return True, usage["input_tokens"], usage["output_tokens"]


def _benchmark_with_compile_fix(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, server: str,
    files: ChallengeFiles,
    seed: str | None = None, hyperparameters: str | None = None,
) -> tuple[dict | None, str, bool, int, int]:
    """Run benchmark, retrying with LLM compile fixes on failure.

    `seed` / `hyperparameters` are forwarded to run_benchmark (used by the
    hyperparameter search to build + smoke-test a variant on the non-test seed).

    Returns (bench, build_err, code_changed, input_tokens, output_tokens).
    """
    max_retries = 2
    input_tokens = 0
    output_tokens = 0
    code_changed = False

    for attempt in range(1 + max_retries):
        bench, build_err = run_benchmark(args, config, server, seed=seed, hyperparameters=hyperparameters)
        if bench is not None:
            return bench, "", code_changed, input_tokens, output_tokens

        # Rustc/cargo output means a CODE problem no matter what else the log
        # contains — decide that FIRST. The bare-number infra markers below
        # used to be substring-matched against the whole error, so scores and
        # track names (e.g. `n_h_edges=50000` ⊃ "500") misrouted every
        # compile error into this branch, silently skipping the LLM compile
        # fix. Numeric HTTP codes now only match as standalone words.
        is_code_error = "error[" in build_err or (
            "error:" in build_err and "Compiling" in build_err)
        infra_markers = ["API Error", "c3 CLI not found",
                         "c3 CLI is out of date", "Docker image",
                         "Could not parse job ID", "timeout"]
        is_infra = not is_code_error and (
            any(m in build_err for m in infra_markers)
            or re.search(r"\b(401|403|500)\b", build_err)
        )
        if is_infra:
            # One line only — the caller's "[BENCH] FAILED — build_err: …"
            # already shows the error head; printing it here too doubled it.
            print(f"  [BENCH] Infrastructure error (not a code problem) — "
                  f"skipping the LLM compile fix")
            return None, build_err, code_changed, input_tokens, output_tokens

        if attempt >= max_retries:
            break

        print(f"  [BENCH] Build retry {attempt + 1}/{max_retries} — asking LLM to fix…")
        ok, it, ot = _try_compile_fix(
            args, model, api_key, config,
            files, build_err,
        )
        input_tokens += it
        output_tokens += ot
        if not ok:
            break
        code_changed = True

    return None, build_err, code_changed, input_tokens, output_tokens


def _hpo_gate_open(
    config: dict, default_bench: dict, improvement_scores: list[float],
    has_tuned: bool,
) -> bool:
    """Should this candidate be hyperparameter-tuned? (see the plan doc)

    Available to BOTH roles. The candidate must be feasible. Then:
      - the FIRST time a trajectory is eligible (`has_tuned` is False), it must
        have had >= first_tune_improvements improvements (a higher bar, so the
        trajectory is well established before any HPO budget is spent); the
        gate then opens automatically — the band check is skipped that once;
      - thereafter the trajectory needs only >= min_improvements improvements,
        and the candidate's default score must fall strictly inside the
        tune band: better than the min_improvements-th-previous improvement
        (`improvement_scores[-min_improvements]`, the floor) AND worse than the
        parent (`improvement_scores[-1]`, the latest improvement). The point is
        to spend HPO budget only on "near-miss" candidates — ones that made real
        progress but haven't beaten the parent — where tuning might push them
        over. A candidate already at/above the parent is a win on its own; one
        at/below the floor has regressed too far.
    "Better"/"worse" respect `scoring_direction` (max: higher is better; min:
    lower is better), so the band is correct for both. Both CPU and GPU solver
    paths accept --hyperparameters, so GPU tunes too.
    """
    min_improvements = int(config.get("hpo_min_improvements", 4))
    first_tune_improvements = int(config.get("hpo_first_tune_improvements", 10))
    direction = str(config.get("scoring_direction", "max"))
    score = default_bench.get("score")
    if score is None or not default_bench.get("feasible", False):
        return False
    if not has_tuned:
        if len(improvement_scores) < first_tune_improvements:
            return False
        print(f"  [HPO] gate open: first tune for this trajectory "
              f"({len(improvement_scores)} improvements) — band check waived")
        return True
    if len(improvement_scores) < min_improvements:
        return False

    def _better(a: float, b: float) -> bool:  # a strictly better than b
        return a < b if direction == "min" else a > b

    parent_score = improvement_scores[-1]
    band_floor = improvement_scores[-min_improvements]
    if not (_better(score, band_floor) and _better(parent_score, score)):
        print(f"  [HPO] gate closed: default score {score:.0f} outside tune band "
              f"(must be better than floor {band_floor:.0f} and worse than parent "
              f"{parent_score:.0f}; direction={direction})")
        return False
    print(f"  [HPO] gate open: score {score:.0f} in tune band "
          f"(floor {band_floor:.0f}, parent {parent_score:.0f}; direction={direction})")
    return True


def _extract_hyperparameters_api(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, challenge_md: str, file_map: dict,
    parent_hyperparameters: dict | None, num_suggested: int,
) -> tuple[dict | None, int, int]:
    """Extraction via a single structured completion (API / CLI providers).

    Reads ALL algorithm files, asks for a spec + (optional) SEARCH/REPLACE edits,
    and applies the edits over the files-map. Returns (parsed | None, in, out)
    where parsed has hyperparameters / suggested_configs / algorithm_files (the
    edited map; == input map for the spec-only Case 0).
    """
    try:
        response, usage = _call_llm_logged(
            "hyperparameter_extract", config,
            args.provider, model, api_key,
            _prompts.build_hyperparameter_system_prompt(challenge_md, config),
            _prompts.build_hyperparameter_user_prompt(
                file_map, config, parent_hyperparameters, num_suggested,
            ),
            args.api_base,
        )
    except Exception as e:
        print(f"  [HPO] extraction LLM call failed: {e}")
        return None, 0, 0
    parsed = _prompts.parse_hyperparameter_response(response)
    if not parsed["ok"]:
        print(f"  [HPO] extraction parse failed: {parsed['error']}")
        return None, usage["input_tokens"], usage["output_tokens"]

    new_map = dict(file_map)
    edits_text = parsed.get("edits_text", "")
    if edits_text:
        blocks = search_replace.parse_blocks(edits_text)
        if not blocks:
            print("  [HPO] extraction emitted edits but no parseable blocks — skipping")
            return None, usage["input_tokens"], usage["output_tokens"]
        new_map, misses = search_replace.apply_blocks(new_map, blocks)
        if misses:
            # A partial apply can break the empty-Map==default invariant, so a
            # miss is fatal for the tune (the build/score guard would otherwise
            # accept a half-rewritten variant).
            print(f"  [HPO] {len(misses)} extraction edit(s) did not match — skipping tune")
            return None, usage["input_tokens"], usage["output_tokens"]
    else:
        print("  [HPO] spec-only extraction (no code edits — config already Map-aware)")

    return {
        "ok": True, "error": "",
        "hyperparameters": parsed["hyperparameters"],
        "suggested_configs": parsed["suggested_configs"],
        "algorithm_files": new_map,
    }, usage["input_tokens"], usage["output_tokens"]


def _extract_hyperparameters_agentic(
    args: argparse.Namespace,
    backend: "agentic_backends.AgenticBackend | None", workdir: "Path | None",
    files: ChallengeFiles, config: dict, challenge_md: str,
    parent_hyperparameters: dict | None, num_suggested: int,
) -> tuple[dict | None, int, int]:
    """Extraction via a second agentic pass (Fix 1).

    The agent edits the worktree's algorithm file in place and writes the spec to
    `.swarm/hyperparameters.json`; we read both back. Token usage isn't reported
    by the CLI backends, so the counts are 0. Returns (parsed | None, 0, 0) with
    parsed shaped like `parse_hyperparameter_response`.
    """
    if backend is None or workdir is None:
        print("  [HPO] agentic extraction unavailable (no backend/worktree)")
        return None, 0, 0
    # Seed the worktree with the EXACT algorithm that produced default_bench
    # (the main checkout) before the agent edits it — ALL files, multi-file
    # aware. The worktree copy can be stale (e.g. a runtime-error fix this
    # iteration was applied to the main checkout, not the worktree), so without
    # this the variant would be built from pre-fix code.
    challenge_files.write_files(files.read_files(), config, base=workdir)
    agentic_sandbox.reset_hyperparameter_spec(workdir)
    backend.prepare(workdir, challenge_md, config, extraction=True)
    prompt = _prompts.build_hyperparameter_agentic_prompt(
        config, parent_hyperparameters, num_suggested,
    )
    print(f"  [HPO] launching {backend.name} for extraction (timeout {args.agentic_timeout}s)…")
    try:
        result = backend.iterate(
            workdir, prompt, model=args.model, timeout_s=args.agentic_timeout,
        )
    except Exception as e:
        print(f"  [HPO] agentic extraction failed: {e}")
        return None, 0, 0
    if result.timed_out:
        print("  [HPO] agentic extraction timed out — skipping tune")
        return None, 0, 0
    spec = agentic_sandbox.read_hyperparameter_spec(workdir)
    if spec is None:
        print("  [HPO] no .swarm/hyperparameters.json written — skipping tune")
        return None, 0, 0
    err = _prompts._validate_hyperparameter_spec(spec)
    if err:
        print(f"  [HPO] agentic spec invalid: {err} — skipping tune")
        return None, 0, 0
    new_map = _read_worktree_map(workdir, config)
    entry = new_map.get(challenge_files.entry_name(config), "")
    # Accept the mainnet anchor (current) or legacy `use super::*;` (pre-parity
    # trajectories not yet migrated by ensure_challenge_import).
    _anchor = f"use tig_challenges::{config['challenge']}::*;"
    if not entry or (_anchor not in entry and "use super::*;" not in entry):
        print("  [HPO] worktree variant missing/invalid — skipping tune")
        return None, 0, 0
    return {
        "ok": True, "error": "",
        "hyperparameters": spec["hyperparameters"],
        "suggested_configs": spec.get("suggested_configs", []),
        "algorithm_files": new_map,
    }, 0, 0


def _maybe_tune_hyperparameters(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, server: str,
    files: ChallengeFiles, challenge_md: str,
    default_bench: dict, improvement_scores: list[float],
    parent_hyperparameters: dict | None,
    has_tuned: bool = False,
    backend: "agentic_backends.AgenticBackend | None" = None,
    workdir: "Path | None" = None,
) -> tuple[dict, dict | None, int, int]:
    """Run a gated hyperparameter search for the just-benchmarked candidate.

    Returns (bench, winning_configs, input_tokens, output_tokens), where
    winning_configs is a per-track map {track_key: config} (a winner per track):
      - if the gate is closed or anything fails, returns the unchanged
        default_bench with winning_configs=None and the original algorithm left
        on disk, so publish proceeds exactly as before;
      - if tuning beats the default on the test seed, returns the tuned bench
        (test seed) and the per-track config map, with the hyperparameter-enabled
        variant left on disk for publish to read.
    """
    in_tok = 0
    out_tok = 0
    if not _hpo_gate_open(config, default_bench, improvement_scores, has_tuned):
        return default_bench, None, in_tok, out_tok

    num_suggested = int(config.get("hpo_num_suggested_configs", 5))
    n = int(config.get("hpo_search_budget", 13))
    hpo_seed = str(config.get("hpo_seed", "hpo"))
    default_score = default_bench.get("score")
    print(f"  [HPO] gate open — tuning (N={n}, suggested={num_suggested}, seed='{hpo_seed}')")

    # Snapshot ALL algorithm files so any failure path restores the exact
    # pre-tuning state (multi-file aware).
    original_map = files.read_files()

    # 1. Extraction: which constants become hyperparameters (with ranges +
    #    suggested configs) and a behaviour-preserving variant (the full
    #    files-map, multi-file aware). Agentic providers do this as a second
    #    agent pass (Fix 1); everyone else via a single structured completion.
    if args.provider in _AGENTIC_PROVIDERS:
        parsed, ei, eo = _extract_hyperparameters_agentic(
            args, backend, workdir, files, config, challenge_md,
            parent_hyperparameters, num_suggested,
        )
    else:
        parsed, ei, eo = _extract_hyperparameters_api(
            args, model, api_key, config, challenge_md,
            original_map, parent_hyperparameters, num_suggested,
        )
    in_tok += ei
    out_tok += eo
    if parsed is None:
        print("  [HPO] extraction produced nothing usable — skipping tune")
        return default_bench, None, in_tok, out_tok
    print(f"  [HPO] hyperparameters: {[h['name'] for h in parsed['hyperparameters']]}")

    # 2. Write the variant (full map) and build/smoke-test it on the HPO seed
    #    (default config), with LLM compile-fix retries.
    variant_map = parsed["algorithm_files"]
    files.write_files(variant_map)
    entry_code = variant_map.get(files.entry_name, "")
    if validate_code(entry_code, config, files=variant_map):
        print("  [HPO] variant failed validation — restoring, skipping tune")
        files.write_files(original_map)
        return default_bench, None, in_tok, out_tok
    compile_bench, build_err, _changed, ci, co = _benchmark_with_compile_fix(
        args, model, api_key, config, server, files,
        seed=hpo_seed, hyperparameters="{}",
    )
    in_tok += ci
    out_tok += co
    if compile_bench is None:
        print(f"  [HPO] variant build failed: {build_err[:200]} — restoring, skipping tune")
        files.write_files(original_map)
        return default_bench, None, in_tok, out_tok

    # 3. Random search on the (non-test) HPO seed.
    # Config-parallel HPO: evaluate all candidate configs concurrently. On C3 the
    # fleet-wide FCFS pool (c3_pool.py) is the real cap, so job_slots stays None
    # and c3_compute self-gates. On local compute there is no shared pool, so a
    # per-process semaphore of c3_max_parallel_jobs bounds concurrent docker runs.
    job_slots = None
    if args.compute != "c3":
        job_slots = threading.Semaphore(max(1, int(config.get("c3_max_parallel_jobs", 3))))

    def benchmark_fn(seed: str, hp_json: str) -> tuple[dict | None, str]:
        return run_benchmark(args, config, server, seed=seed,
                             hyperparameters=hp_json, job_slots=job_slots)

    result = hpo.search(
        benchmark_fn, parsed["hyperparameters"], parsed["suggested_configs"],
        n=n, num_suggested=num_suggested, hpo_seed=hpo_seed, log=print,
    )

    winning = result["winning_configs"]  # {track_key: config} — a winner per track
    if not winning:
        print("  [HPO] search produced no per-track winners — keeping default")
        files.write_files(original_map)
        return default_bench, None, in_tok, out_tok

    # 4. Adopt the per-track winning map as the trajectory's new default
    #    hyperparameters and score the variant on the TEST seed under that map
    #    (benchmark.py selects each track's config per instance). The tuned score
    #    is published UNCONDITIONALLY — no "must beat the default" revert. The
    #    only safety is feasibility: an infeasible/missing tuned result can't be
    #    published (it would tank the trajectory), so we fall back to the default
    #    in that case. Because the default config {} is in every track's search
    #    set, each track's winner is >= default on the HPO seed; a tuned score
    #    below the untuned default can only arise from the test-vs-HPO seed
    #    mismatch, and per the design we accept that.
    tuned_bench, terr = run_benchmark(
        args, config, server, hyperparameters=json.dumps(winning),
    )
    if tuned_bench is None:
        print(f"  [HPO] final test-seed benchmark failed: {terr[:200]} — restoring, using default")
        files.write_files(original_map)
        return default_bench, None, in_tok, out_tok

    tuned_score = tuned_bench.get("score")
    tuned_feasible = bool(tuned_bench.get("feasible", False))
    if tuned_score is None or not tuned_feasible:
        print(f"  [HPO] tuned result infeasible/missing on the test seed "
              "— restoring, using default")
        files.write_files(original_map)
        return default_bench, None, in_tok, out_tok

    delta = tuned_score - default_score
    print(f"  [HPO] tuned score {tuned_score:.0f} (default {default_score:.0f}, "
          f"{'+' if delta >= 0 else ''}{delta:.0f}) — publishing variant + per-track "
          f"configs {json.dumps(winning)}")
    return tuned_bench, winning, in_tok, out_tok


def _fix_runtime_errors(
    args: argparse.Namespace, model: str, api_key: str,
    config: dict, server: str, agent_token: str, agent_id: str, challenge_md: str,
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
                build_runtime_fix_prompt(
                    current_code, bench, current_kernel,
                    timeout=int(config.get("timeout", 30)),
                ),
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

        violation = _validate_entry(fixed, config, files)
        if violation:
            print(f"  Fix failed validation: {violation}")
            return restore_and_fail()

        # Abort ONLY on a byte-identical echo — same rationale as the
        # compile-fix path above: a legitimate one-line runtime fix in a long
        # file is ~99.7% similar, so a `sim >= 0.99` threshold rejected
        # exactly the small, correct fixes we want. Similarity is kept purely
        # as a diagnostic readout.
        if fixed == current_code and (fixed_kernel or "") == (current_kernel or ""):
            print("  Fix returned the broken code unchanged (no-op) — restoring previous best.")
            return restore_and_fail()
        sim = difflib.SequenceMatcher(None, current_code, fixed).ratio()
        print(f"  Fix changed the code (similarity to broken: {sim * 100:.1f}%) — re-benchmarking.")
        files.write(fixed, fixed_kernel)
        code_changed = True

        print("  Re-running benchmark ...")
        send_heartbeat(server, agent_id, agent_token=agent_token)
        bench_result, build_err = run_benchmark(args, config, server)

        if bench_result is None:
            print(f"  Runtime fix caused compile error — asking LLM to fix ...")
            ok, it, ot = _try_compile_fix(
                args, model, api_key, config,
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
        _print_bench_result(bench)

    return bench, code_changed, input_tokens, output_tokens


# ── Agentic (mode 2) iteration ─────────────────────────────────────


_AGENTIC_HEARTBEAT_INTERVAL_S = 60


def _start_heartbeat_thread(
    server: str, agent_id: str, agent_token: str,
    timeout_s: int | None = None,
    interval_s: int = _AGENTIC_HEARTBEAT_INTERVAL_S,
    label: str = "AGENTIC",
) -> threading.Event:
    """Send a heartbeat every `interval_s` seconds while a long call runs.

    Mode-2 iterations can run 10+ minutes inside a single `claude -p`
    subprocess, and a local benchmark can block for hours. Without a
    background heartbeat the agent would drop from the server's
    inspiration pool mid-iteration — and worse, the server's
    `inactive_minutes` sweep would deactivate its trajectory, so the next
    publish lands on a fresh one and progress never compounds. The same
    loop also prints a periodic elapsed-time line to the terminal so the
    silent capture doesn't look like a hang ("is it frozen?"). Returns a
    stop event the caller must set when the wrapped call exits.
    """
    stop = threading.Event()
    started = time.monotonic()

    def _beat() -> None:
        while not stop.wait(interval_s):
            elapsed = int(time.monotonic() - started)
            budget = f" / {timeout_s}s budget" if timeout_s else ""
            print(f"  [{label}] …still working ({elapsed}s elapsed{budget})")
            try:
                send_heartbeat(server, agent_id, agent_token=agent_token)
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
    # Prefer the multi-file map; fall back to the legacy single-file (+kernel)
    # fields so a server/state that predates files-map still seeds correctly.
    file_map = _state_files_map(state, config)
    if file_map:
        challenge_files.write_files(file_map, config, base=workdir)
    else:
        best_code = state.get("best_algorithm_code") or ""
        algo_path = workdir / config["algorithm_path"]
        algo_path.parent.mkdir(parents=True, exist_ok=True)
        if best_code:
            algo_path.write_text(best_code, encoding="utf-8")
        kernel_rel = config.get("kernel_path")
        best_kernel = state.get("best_kernel_code") or ""
        if files.is_gpu and kernel_rel and best_kernel:
            kp = workdir / kernel_rel
            kp.parent.mkdir(parents=True, exist_ok=True)
            kp.write_text(best_kernel, encoding="utf-8")

    agentic_sandbox.seed_worktree_config(workdir)


def _should_distill_tacit(
    state: dict, config: dict, is_new_best: bool, provider: str,
) -> bool:
    """Predicate for firing the tacit-knowledge distillation step.
    Triggers on the last iteration before a trajectory reset would
    happen, gated on the attempt not having saved the trajectory:

      - my_runs_since_improvement == stagnation_limit - 1
      - this attempt did not improve (else stagnation resets to 0 anyway)
      - stagnation_limit >= 3 so there are >= 2 failures to distill from

    For agentic providers we currently rely on the in-band prompt in
    `build_agentic_user_prompt` instead, unless
    prompts.DRIVER_DISTILL_FOR_AGENTIC is flipped on.

    Gated by the per-agent `tacit_write` config flag (default True)."""
    if not tacit_write_enabled(config):
        return False
    if is_new_best:
        return False
    limit = int(config.get("stagnation_limit") or 0)
    if limit < 3:
        return False
    if state.get("my_runs_since_improvement", 0) != limit - 1:
        return False
    if provider in _AGENTIC_PROVIDERS and not _prompts.DRIVER_DISTILL_FOR_AGENTIC:
        return False
    return True


def _append_tacit_line(line: str) -> None:
    """Append a single `- LLM:` bullet to the worktree's tacit file. The
    file is created on first use (matching the convention used by
    `setup.py` and `run.py`). The fleet's sync-back hook will carry the
    new line back to the source `tacit_knowledge.md` on shutdown."""
    path = ROOT / "tacit_knowledge_personal.md"
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
        if line in existing:
            return  # already present — keep things idempotent within a run
        if not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + line + "\n", encoding="utf-8")
    else:
        path.write_text(
            "# Personal tacit knowledge (worktree copy)\n\n"
            "## Strategies\n\n"
            + line + "\n",
            encoding="utf-8",
        )


def _distill_tacit_if_due(
    state: dict, config: dict, is_new_best: bool, provider: str,
    model: str, api_key: str | None, api_base: str | None,
    files: ChallengeFiles,
) -> tuple[int, int]:
    """If the trigger fires, run one LLM call to distill a transferable
    failure-lesson from this trajectory and append it to the worktree's
    tacit file. Returns the (input_tokens, output_tokens) used (zero
    when the trigger doesn't fire or the model returned SKIP)."""
    if not _should_distill_tacit(state, config, is_new_best, provider):
        return 0, 0

    current_code, _ = files.read()
    tacit_path = ROOT / "tacit_knowledge_personal.md"
    existing_tacit = tacit_path.read_text(encoding="utf-8", errors="replace") if tacit_path.exists() else ""

    system_prompt, user_prompt = build_tacit_distillation_prompts(
        state, config, current_code, existing_tacit,
    )

    print("  [TACIT] Trajectory about to reset — distilling failure lesson…")
    try:
        response, usage = _call_llm_logged(
            "tacit_distill", config,
            provider, model, api_key,
            system_prompt, user_prompt, api_base,
        )
    except Exception as e:
        print(f"  [TACIT] distillation call failed: {e}")
        return 0, 0

    line = parse_tacit_distillation(response)
    if line is None:
        print("  [TACIT] model returned SKIP (or unparseable) — no entry added")
    else:
        _append_tacit_line(line)
        print(f"  [TACIT] appended: {line[:120]}")
    return usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _read_worktree_files(
    workdir: Path, files: ChallengeFiles, config: dict,
) -> tuple[str, str]:
    """Read whatever the agent left on disk in the worktree (entry file +
    optional kernel). For multi-file algorithms use `_read_worktree_map`."""
    algo_path = workdir / config["algorithm_path"]
    code = algo_path.read_text(encoding="utf-8", errors="replace") if algo_path.exists() else ""
    kernel = ""
    if files.is_gpu and config.get("kernel_path"):
        kp = workdir / config["kernel_path"]
        if kp.exists():
            kernel = kp.read_text(encoding="utf-8", errors="replace")
    return code, kernel


def _read_worktree_map(workdir: Path, config: dict) -> dict[str, str]:
    """Read the full {relpath: content} algorithm map the agent left on disk."""
    return challenge_files.read_files(config, base=workdir)


def _state_files_map(state: dict, config: dict) -> dict[str, str]:
    """The algorithm files-map from server state, with single-file fallback.

    Prefers `best_algorithm_files` (the multi-file map). Falls back to the
    legacy single-file (`best_algorithm_code`) + optional kernel
    (`best_kernel_code`) so older server state still works."""
    fm = state.get("best_algorithm_files")
    if isinstance(fm, dict) and fm:
        return dict(fm)
    out: dict[str, str] = {}
    code = state.get("best_algorithm_code") or ""
    if code:
        out[challenge_files.entry_name(config)] = code
    kernel = state.get("best_kernel_code") or ""
    if kernel and config.get("kernel_path"):
        out[Path(config["kernel_path"]).name] = kernel
    return out


def _run_agentic_iteration(
    args: argparse.Namespace,
    state: dict, config: dict, server: str, agent_token: str,
    agent_id: str, agent_name: str,
    workdir: Path, backend: agentic_backends.AgenticBackend,
    challenge_md: str, files: ChallengeFiles,
    *, role: str = "explorer", assigned_tag: str | None = None,
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

    user_prompt = build_agentic_user_prompt(state, config, role=role, assigned_tag=assigned_tag)
    print(f"  [AGENTIC] Launching {backend.name} in {workdir} (timeout {args.agentic_timeout}s)…")
    # Heads-up so the contributor's terminal doesn't look frozen. The
    # subprocess is run with capture_output=True (we need the trace for
    # fallback hypothesis synthesis), so stdout doesn't stream live — the
    # backend can run for the full --agentic-timeout before printing anything
    # else. Docker stays idle too: benchmark.py only runs *after* this returns.
    print(
        f"  [AGENTIC] Output is captured; the agent runs silently (up to "
        f"{args.agentic_timeout}s) with a heartbeat every "
        f"{_AGENTIC_HEARTBEAT_INTERVAL_S}s so you can see it's alive. "
        f"Docker stays idle until then."
    )

    stop = _start_heartbeat_thread(
        server, agent_id, agent_token, timeout_s=args.agentic_timeout,
    )
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
            "anthropic", "openai", "google", "openrouter", "venice",
            "claude-code", "claude-code-agentic", "codex-agentic",
        ],
        help=(
            "LLM provider (default: agent.config.json, then anthropic). "
            "`claude-code` = headless single-shot completion via the local CLI; "
            "`claude-code-agentic` = headless Claude Code agentic mode in a "
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
        help=(
            "C3 hardware for --compute c3. Use 'auto' to choose "
            "cpu-d3-4vcpu-16gb for CPU challenges and l40 for GPU "
            "challenges (default: auto)."
        ),
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
        "--c3-max-parallel-jobs", type=int,
        help=(
            "Fleet-wide C3 concurrent-job cap (default 3, the basic C3 plan cap). "
            "Also the number of balanced shards each benchmark fans out to. All "
            "agents sharing one C3 key draw from this pool FCFS; extra shards "
            "queue and run as slots free up."
        ),
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
        "--agentic-timeout", type=int, default=1800,
        help=(
            "Wall-clock timeout in seconds for one agentic iteration "
            "(claude-code-agentic only). Default 1800 (30 min). The claude "
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
        "openrouter": "OPENROUTER_API_KEY",
        "venice": "VENICE_API_KEY",
    }
    env_var = env_map.get(provider)
    if env_var is None:
        sys.exit(
            f"Unknown provider {provider!r}. "
            f"Known: {', '.join(sorted(env_map))} (plus the CLI-auth "
            "providers claude-code, claude-code-agentic, codex-agentic)."
        )
    key = os.environ.get(env_var, "")
    if not key:
        sys.exit(f"No API key. Set ${env_var} or pass --api-key.")
    return key


def preflight_llm_check(
    provider: str, model: str, api_key: str, api_base: str | None,
) -> None:
    """Verify the LLM endpoint is reachable with the provided key/model BEFORE
    we register the agent on the swarm.

    Without this, an agent with a bad API key (revoked, misspelt env var,
    wrong provider for the key, model not enabled on the account, hard rate
    limit) registers fine, broadcasts agent_joined to every dashboard, then
    fails every iteration in a tight retry loop. The dashboard ends up
    showing ghost agents that have never published anything — confusing for
    swarm hosts trying to read the AGENTS counter against the leaderboard.

    Skipped for providers that don't go through call_llm (claude-code uses
    a CLI + subscription auth; the agentic providers run their backend's
    CLI directly and surface auth errors at first invocation)."""
    if provider in ("claude-code", "claude-code-agentic", "codex-agentic"):
        return
    print(f"  [LLM] Pre-flight check via {provider}/{model}…")
    try:
        call_llm(
            provider, model, api_key,
            "You are a smoke test responder.",
            "Reply with the single word OK.",
            api_base,
        )
    except Exception as e:
        sys.exit(
            f"LLM pre-flight failed for {provider}/{model}: {e}\n"
            f"Fix the API key / model / provider settings and try again. "
            f"The agent has NOT been registered, so nothing was posted to "
            f"the swarm dashboard."
        )
    print("  [LLM] Pre-flight OK")


# ── Main loop ──────────────────────────────────────────────────────


def main() -> int:
    args = parse_args()
    config = load_config()
    agent_config = load_agent_config()
    # `setup.py sync` (called at the top of every iteration) rebuilds
    # .swarm-cache.json from a server-field whitelist, so these agent-local
    # flags can't live there. Read them from agent.config.json once and
    # re-apply them after each load_config() inside the loop.
    log_prompts = bool(agent_config.get("log_prompts"))
    config["log_prompts"] = log_prompts
    # Opt-in stricter, rule-based Rust prompt for smaller/cheaper models whose
    # raw output tends not to compile (see _rust_rules_block in prompts.py).
    detailed_prompts = bool(agent_config.get("detailed_prompts"))
    config["detailed_prompts"] = detailed_prompts

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
    args.hardware = args.hardware or agent_config.get("c3_hardware") or agent_config.get("hardware") or "auto"
    args.c3_time = args.c3_time or agent_config.get("c3_time") or "02:00:00"
    args.c3_provider = args.c3_provider or agent_config.get("c3_provider")
    # Distributed C3 benchmarking (default-on when compute=c3). CLI flag wins,
    # else per-agent agent.config.json, else default (3 = the basic C3 plan cap).
    # This is both the fleet-wide FCFS pool size and the balanced shard count.
    if args.c3_max_parallel_jobs is None:
        args.c3_max_parallel_jobs = agent_config.get("c3_max_parallel_jobs", 3)
    # Per-agent C3 key from agent.config.json (forwarded by run_fleet from the
    # agent entry or the fleet-wide default). The --c3-api-key flag still wins;
    # if both are empty, c3_compute falls back to C3_API_KEY / `c3 login`.
    args.c3_api_key = args.c3_api_key or agent_config.get("c3_api_key")
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
    swarm_password = (agent_config.get("swarm_password") or "").strip()
    username = (agent_config.get("username") or "").strip()
    if not swarm_password or not username:
        sys.exit(
            "Missing username or swarm_password in agent.config.json. "
            "Both are required to register — ask the host to run "
            "`python setup.py invite <your-name>` and paste both into "
            "fleet.config.json, then respawn the fleet."
        )
    if args.compute == "c3":
        if shutil.which("c3") is None:
            sys.exit("c3 CLI not found. Install it from https://docs.cthree.cloud/.")

    # Pre-flight the LLM BEFORE touching the swarm. If this fails, the script
    # exits without calling register_agent — no agent_joined broadcast, no
    # phantom row in the dashboard's AGENTS counter.
    preflight_llm_check(args.provider, model, api_key, args.api_base)

    # Register or resume. agent.config.json is local-only, so it is safe to
    # persist the swarm agent id + token there for automatic restarts.
    # The token is the per-agent secret returned by /api/agents/register;
    # it gates every non-register write call via the X-Agent-Token header.
    configured_agent_id = agent_config.get("agent_id")
    configured_agent_name = agent_config.get("agent_name")
    configured_agent_token = agent_config.get("agent_token")
    if args.new_agent:
        configured_agent_id = None
        configured_agent_name = None
        configured_agent_token = None

    if (args.agent_id or configured_agent_id) and configured_agent_token:
        agent_id = args.agent_id or configured_agent_id
        agent_name = args.agent_name or configured_agent_name or f"script-{agent_id[:8]}"
        agent_token = configured_agent_token
        # Validate before resuming. If the server doesn't have a row for
        # this id (DB reset/redeploy, switched swarms, or a first-run
        # interruption left a stale id locally), re-register with the
        # same display name so the contributor keeps their identity.
        # Multi-agent coordination keys off agent_id only — renaming or
        # re-registering one contributor is invisible to everyone else.
        if agent_exists(server, agent_id, agent_token):
            # Authenticated probe before the loop spends an LLM call: a
            # revoked worker still satisfies agent_exists (the row is
            # preserved for dashboard history; only token + status change),
            # so without this check the first 403 wouldn't surface until
            # post_message/heartbeat in iteration 1.
            try:
                validate_agent_token(server, agent_id, agent_token)
            except AgentTokenRevoked as e:
                sys.exit(
                    f"This agent's access has been revoked by the swarm host "
                    f"(server: {server}).\n"
                    f"  agent_id: {agent_id}\n"
                    f"  agent_name: {agent_name}\n"
                    f"  server detail: {e}\n"
                    f"Ask the host to re-invite you, then re-run setup with "
                    f"the new swarm_password."
                )
            print(f"Resuming agent: {agent_name} ({agent_id})")
        else:
            print(
                f"  [REGISTER] Stored agent_id {agent_id} not on server; "
                f"re-registering as {agent_name!r}…"
            )
            agent_id, agent_name, agent_token = register_agent(
                server, provider=args.provider, model=model,
                requested_name=agent_name,
                name=agent_config.get("name"),
                username=username,
                swarm_password=swarm_password,
            )
            print(f"Re-registered as: {agent_name} ({agent_id})")
    else:
        # No persisted token (fresh install, upgrade from pre-token version,
        # or --new-agent) — register fresh.
        agent_id, agent_name, agent_token = register_agent(
            server, provider=args.provider, model=model,
            name=agent_config.get("name"),
            username=username,
            swarm_password=swarm_password,
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
        "c3_max_parallel_jobs": args.c3_max_parallel_jobs,
        "env": args.env,
    }
    for key, value in runtime_defaults.items():
        updated_agent_config.setdefault(key, value)
    updated_agent_config.update({
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_token": agent_token,
    })
    write_agent_config(updated_agent_config)
    tig_user_id = _compose_tig_user_id(username, agent_id)
    os.environ["TIG_USER_ID"] = tig_user_id

    # Refresh .swarm-cache.json + CHALLENGE.md against the live server before
    # the start-up banner prints `Challenge: ...`. Without this, a worktree
    # whose cache predates a host-side `setup.py switch` would announce the
    # old challenge until the first iteration's sync runs — confusing to read
    # and easy to mis-trust. The per-iteration sync below still handles
    # mid-run challenge switches.
    print("  [SYNC] Syncing challenge with server…")
    sync_challenge()
    config = load_config()
    config["log_prompts"] = log_prompts
    config["detailed_prompts"] = detailed_prompts
    _attach_benchmark_identity(config, username, agent_id, agent_token)
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
        # Use the backend's resolver instead of a bare shutil.which so the
        # precheck honors the same env overrides (CODEX_CLI / CLAUDE_CLI) and
        # Windows .cmd fallbacks the live call does. Without this, npm-installed
        # Codex would pass at the backend layer but get rejected here.
        if backend.resolve_cli() is None:
            override_hint = ""
            if sys.platform == "win32" and backend.cli_name == "codex":
                override_hint = (
                    "\nTip: if you installed via `npm install -g @openai/codex`, "
                    "export CODEX_CLI=%APPDATA%\\npm\\codex.cmd before launching."
                )
            sys.exit(
                f"{backend.cli_name} CLI not found on PATH. Install it, or "
                f"switch to a non-agentic provider (e.g. --provider claude-code "
                f"for single-shot mode).{override_hint}"
            )

    print(f"Provider: {args.provider}  Model: {model}")
    compute_desc = f"c3/{args.hardware.lower()}" if args.compute == "c3" else args.compute
    if args.compute == "c3" and args.env:
        compute_desc += f" image={args.env}"
    print(f"Compute: {compute_desc}")
    print(f"Challenge: {config.get('challenge', '?')}")
    print(f"Server: {server}")
    print()

    # Contributor-owned role, re-read from agent.config.json every iteration so
    # an edit to fleet.config.json (propagated into the worktree by run_fleet)
    # takes effect on the next loop. Defaults to 'explorer'.
    role = _normalize_role(agent_config.get("role"))
    print(f"Role: {role}")

    # Contributor-owned seeding override, same lifecycle as role (hot-reloads
    # from fleet.config.json via run_fleet's sync). None = server auto policy.
    seeded_start = _normalize_seeded_start(agent_config.get("seeded_start"))
    if seeded_start is not None:
        print(f"Seeded start: {seeded_start}")

    # The hyperparameter-search gate's inputs (improvement history + parent
    # config) come from /api/state each iteration — keyed by trajectory_id, so
    # they survive restarts and adoption out of the inactive pool. See
    # docs/hyperparameter-search-plan.md.
    iteration = 0
    consecutive_sr_skips = 0  # no-edit S/R skips in a row (see the fallback below)
    # Token-spending iterations in a row that ended without a successful
    # benchmark, and the challenge they accrued on (see the freeze guard).
    bench_failures = 0
    bench_fail_challenge: str | None = None
    # Iteration of the last cleaner attempt (accepted or rejected) — enforces
    # cleaner_cooldown_iters so a rejected clean can't burn a benchmark every
    # single iteration on unchanged code.
    cleaner_last_attempt = -(10 ** 9)
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
        config["detailed_prompts"] = detailed_prompts
        _attach_benchmark_identity(config, username, agent_id, agent_token)
        challenge_md = read_challenge_md()
        # Pin the iteration's challenge here so chat messages and any other
        # follow-up writes stay attributed to it even if the host runs
        # `setup.py switch` while this iteration is running.
        iter_challenge = config.get("challenge")
        print(f"  [SYNC] Challenge: {iter_challenge or '?'}  GPU: {config.get('is_gpu', False)}")

        # Re-read the contributor-owned role (run_fleet patches fleet.config.json
        # edits into this worktree's agent.config.json live). Log on change only.
        _agent_cfg = load_agent_config()
        live_role = _normalize_role(_agent_cfg.get("role"))
        if live_role != role:
            print(f"  [ROLE] role changed: {role} -> {live_role}")
            role = live_role
        live_seeded = _normalize_seeded_start(_agent_cfg.get("seeded_start"))
        if live_seeded != seeded_start:
            print(f"  [SEED] seeded_start changed: {seeded_start} -> {live_seeded}")
            seeded_start = live_seeded

        # Surface host-tunable HPO + cleaner + freeze knobs and the C3
        # warm-image opt-in (materialized into agent.config.json from
        # fleet.config.json) onto `config`, which the gate/search/cleaner,
        # the freeze guard, and c3_compute read.
        # Absent keys fall back to the defaults baked into
        # _maybe_tune_hyperparameters / the _CLEANER_* constants /
        # c3_compute._warm_c3_image (unset = full-source staging).
        for _hpo_key in ("hpo_min_improvements", "hpo_first_tune_improvements",
                         "hpo_num_suggested_configs",
                         "hpo_search_budget", "hpo_seed",
                         "cleaner_trigger_chars", "cleaner_target_pct",
                         "cleaner_score_delta_pct", "cleaner_cooldown_iters",
                         "no_benchmark_freeze_limit",
                         "c3_warm_images", "c3_warm_image", "tig_dockerhub"):
            if _hpo_key in _agent_cfg:
                config[_hpo_key] = _agent_cfg[_hpo_key]

        try:
            swarm_cfg = server_get(f"{server}/api/swarm_config")
            config["available_challenges"] = swarm_cfg.get("available_challenges", {})
        except Exception:
            pass

        # ── Benchmark-failure freeze guard ─────────────────────
        # (see _NO_BENCHMARK_FREEZE_LIMIT). Checked before any state fetch or
        # LLM call so a frozen agent's exit iteration spends nothing. The
        # counter resets on a challenge switch — the failures belonged to the
        # old challenge's benchmark path.
        freeze_limit = _freeze_limit(config)
        if iter_challenge != bench_fail_challenge:
            if bench_failures:
                print(f"  [FREEZE] challenge switched — resetting the "
                      f"no-benchmark counter ({bench_failures} → 0)")
            bench_failures = 0
            bench_fail_challenge = iter_challenge
        if freeze_limit and bench_failures >= freeze_limit:
            freeze_msg = (
                f"frozen: {bench_failures} consecutive iterations spent LLM "
                f"effort without a successful benchmark "
                f"(no_benchmark_freeze_limit={freeze_limit}). Stopping so no "
                f"more API tokens are wasted — fix the benchmark path "
                f"(compute provider / Docker) and restart the fleet to resume."
            )
            print(f"  [FREEZE] Agent {freeze_msg}")
            post_message(server, agent_name, agent_id, f"[freeze] {freeze_msg}",
                         challenge=iter_challenge, agent_token=agent_token)
            return 0

        def _note_bench_failure() -> None:
            """Count a token-spending iteration that ended benchmark-less."""
            nonlocal bench_failures
            bench_failures += 1
            if freeze_limit:
                print(f"  [FREEZE] {bench_failures}/{freeze_limit} consecutive "
                      f"iterations without a successful benchmark"
                      + (" — freezing next iteration"
                         if bench_failures >= freeze_limit else ""))

        # ── Get state ──────────────────────────────────────────
        print("  [STATE] Fetching agent state…")
        try:
            state = get_state(
                server, agent_id, role=role,
                seeded_start=seeded_start, agent_token=agent_token,
            )
        except Exception as e:
            print(f"  [STATE] FAILED: {e}")
            time.sleep(_ITERATION_BACKOFF_SECS)
            continue

        # If the agent's local `name` (from agent.config.json, materialized
        # from fleet.config.json) differs from the server's agents.name, POST
        # a rename. Cheap: piggybacks on the state we already fetched.
        try:
            from sync_identity import sync_identity_with_state
            renamed = sync_identity_with_state(server, agent_id, state, agent_token=agent_token)
            if renamed:
                agent_name = renamed
                print(f"  [IDENT] renamed to {agent_name!r}")
        except Exception as e:
            print(f"  [IDENT] sync skipped: {e}")

        my_score = _clean_score(state.get("current_trajectory_best"))
        global_best = _clean_score(state.get("best_score"))
        stagnation = state.get("my_runs_since_improvement", 0)
        runs = state.get("my_runs", 0)
        improvements = state.get("my_improvements", 0)
        print(f"  [STATE] My best: {my_score}  Global best: {global_best}")
        print(f"  [STATE] Runs: {runs}  Improvements: {improvements}  Stagnation: {stagnation}")

        reset = state.get("trajectory_reset")
        if reset:
            start = reset.get("start")
            start_str = f" (start: {start})" if start else ""
            # Why the server reset us: "stagnation" (runs_since_improvement
            # hit stagnation_limit) or "negative_cull" (trajectory best still
            # not positive after negative_trajectory_limit edits).
            reason = reset.get("reason")
            reason_str = f" [{reason}]" if reason else ""
            print(f"  [STATE] ** TRAJECTORY RESET — {reset.get('type')}{reason_str}{start_str} **")
            post_message(server, agent_name, agent_id,
                         f"Trajectory reset: {reset.get('type')}{reason_str}",
                         challenge=iter_challenge,
                         agent_token=agent_token)

        # Where the server sourced this iteration's starting code, when it
        # didn't continue our own best: 'seed' (seed pool), 'peer' (best active
        # peer adopted), or 'stub' (true cold start). Makes the standard-tier
        # seeding path observable instead of silent — see server/tiers.py.
        seed_start = state.get("seed_start")
        if seed_start:
            tier = state.get("tier", "?")
            label = {
                "seed": "seeded from the seed pool",
                "peer": "adopted the best active peer's algorithm",
                "stub": "got the bare stub (cold start — no seed/peer available)",
            }.get(seed_start, seed_start)
            print(f"  [STATE] Start source: {label} [tier={tier}]")

        # Soft strategy-tag suggestion from the server (explorers only).
        assigned_tag = state.get("assigned_strategy_tag")

        # ── Write current best to disk ─────────────────────────
        best_code = state.get("best_algorithm_code") or ""
        best_kernel = state.get("best_kernel_code") or ""
        # Full multi-file best (single-file collapses to {entry: best_code});
        # used to seed the main checkout so multi-file algorithms land intact.
        best_file_map = _state_files_map(state, config)
        files = ChallengeFiles(config)
        bootstrap = is_stub_code(best_code)

        # Exploiters refine existing code; they never bootstrap from scratch.
        # The server seeds standard/exploiter agents with a working algorithm
        # (or a peer's best); a stub here means the true cold start — no seed
        # and no feasible peers yet. Idle locally rather than rewriting, and do
        # NOT post to the feed.
        if role == "exploiter" and bootstrap:
            print("  [ROLE] Exploiter awaiting seed (cold start) — skipping iteration, will not bootstrap.")
            time.sleep(_ITERATION_BACKOFF_SECS)
            continue

        if best_code and not bootstrap:
            # write_files prunes stale source files so a multi-file best lands
            # intact; for a single-file best the map is just {entry: best_code}.
            files.write_files(best_file_map)
            print(f"  [FILES] {files.describe_write(best_code, best_kernel)}")
            if files.is_gpu and not best_kernel:
                print(f"  [FILES] No kernel code from server — using local kernels.cu")

        # Adopted an unbenchmarked seed (admin/mainnet seed deposited with no
        # score): benchmark it UNCHANGED first so the trajectory floor is the
        # seed's true score, not its first mutation. The adopted code is already
        # on disk (written just above). Publish a no-mutation iteration, then
        # re-loop so the next pass starts from the now-floored state and mutates
        # normally. Best-effort: if the seed won't even benchmark, fall through.
        if (reset and reset.get("type") == "adopted_inactive"
                and reset.get("needs_benchmark") and best_code and not bootstrap):
            compute_label = f"C3/{args.hardware}" if args.compute == "c3" else "local Docker"
            print(f"  [SEED-BENCH] Adopted unbenchmarked seed — scoring it unchanged "
                  f"on {compute_label} to set the floor before mutating…")
            send_heartbeat(server, agent_id, agent_token=agent_token)
            seed_bench, seed_err = run_benchmark(args, config, server)
            if seed_bench is None:
                print(f"  [SEED-BENCH] FAILED — {seed_err[:300]}")
                print(f"  [SEED-BENCH] Could not score the seed; proceeding to a normal iteration.")
            else:
                _print_bench_result(seed_bench)
                seed_hyp = {
                    "title": "Baseline: adopted mainnet seed",
                    "description": (
                        "Benchmarked the adopted inactive-pool seed unchanged to "
                        "record its true score before mutating."
                    ),
                    "strategy_tag": "seed_baseline",
                }
                try:
                    publish_results(
                        server, agent_id, seed_bench, seed_hyp, config,
                        agent_token=agent_token, role=role,
                    )
                    print(f"  [SEED-BENCH] Floor set at "
                          f"{_clean_score(seed_bench.get('score', 0)):.0f}; "
                          f"re-syncing before mutating.")
                except Exception as e:
                    print(f"  [SEED-BENCH] publish FAILED: {e}")
                post_message(server, agent_name, agent_id,
                             f"[seed_baseline] benchmarked adopted seed → "
                             f"{_clean_score(seed_bench.get('score', 0)):.0f}",
                             challenge=seed_bench.get("challenge") or iter_challenge,
                             agent_token=agent_token)
                send_heartbeat(server, agent_id, agent_token=agent_token)
                bench_failures = 0  # the seed benchmark succeeded
                continue

        if bootstrap:
            print("  [FILES] Starting from stub — will ask LLM to write initial implementation")

        # ── Cleaner: spend this iteration on bloat reduction when the best
        # has outgrown the trigger (docs/cleaner-agent-plan.md). Gated on:
        # size over trigger, cooldown elapsed (a failed clean must not retry
        # next iteration — nothing changed), and the trajectory not being one
        # failure away from a reset (the benchmark would be wasted).
        cleaner_trigger = int(config.get("cleaner_trigger_chars", _CLEANER_TRIGGER_CHARS))
        cleaner_cooldown = int(config.get("cleaner_cooldown_iters", _CLEANER_COOLDOWN_ITERS))
        stagnation_limit = int(config.get("stagnation_limit") or 0)
        total_algo_chars = cleaner_prepass.total_chars(best_file_map)
        if (best_code and not bootstrap
                and total_algo_chars > cleaner_trigger
                and iteration - cleaner_last_attempt >= cleaner_cooldown
                and not (stagnation_limit and stagnation >= stagnation_limit - 1)):
            cleaner_last_attempt = iteration
            print(f"  [CLEANER] trajectory best is {total_algo_chars} chars "
                  f"(> {cleaner_trigger}) — attempting deterministic clean")
            send_heartbeat(server, agent_id, agent_token=agent_token)
            if _run_cleaner_iteration(
                    args, config, server, files, state,
                    agent_id, agent_token, role):
                post_message(server, agent_name, agent_id,
                             f"[refactor] cleaned trajectory best "
                             f"({total_algo_chars} chars → smaller); score kept",
                             challenge=iter_challenge, agent_token=agent_token)
                bench_failures = 0  # the cleaner's benchmark succeeded
                continue
            # Rejected/no-op: files are restored; fall through to a normal
            # iteration. The cooldown stops immediate retries either way.

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
                args, state, config, server, agent_token, agent_id, agent_name,
                workdir, backend, challenge_md, files,
                role=role, assigned_tag=assigned_tag,
            )
            tag = hypothesis.get("strategy_tag", "other")
            title = hypothesis.get("title", "untitled")
            print(f"  [AGENTIC] Hypothesis: [{tag}] {title}")

            if not code:
                print("  [AGENTIC] Agent left no algorithm file — restoring best")
                if best_code:
                    files.write(best_code, best_kernel)
                # Local-only failure: don't broadcast to the swarm feed.
                # A backend that consistently produces no code would otherwise
                # spam every dashboard viewer once per iteration.
                _note_bench_failure()
                continue

            # The agent often rewrites the import block and drops the required
            # `use tig_challenges::<ch>::*;` anchor (or spells it the long
            # way), which would otherwise discard the whole run. Re-insert it
            # (migrating any legacy `use super::*;`) before validating — and
            # likewise the serde_json / std::collections imports it strands.
            code = ensure_common_imports(
                ensure_challenge_import(code, config["challenge"]))
            violation = _validate_entry(code, config, files)
            if violation:
                print(f"  [AGENTIC] Validation failed: {violation} — restoring best")
                if best_code:
                    files.write(best_code, best_kernel)
                _note_bench_failure()
                continue

            # Copy the worktree's edited files into the main checkout so the
            # official benchmark sees them. No compile-fix retry: the agent
            # ran `cargo check` itself before stopping. If the official
            # build still fails (e.g. feature-flag mismatch the agent
            # missed), we restore and continue without escalating.
            # Read the FULL worktree map (multi-file aware), then apply the
            # validated/anchor-fixed entry file over it before writing.
            agent_map = _read_worktree_map(workdir, config)
            if agent_map:
                agent_map[files.entry_name] = code
                files.write_files(agent_map)
            else:
                files.write(code, new_kernel)
            print(f"  [FILES] {files.describe_write(code, new_kernel)}")

            compute_label = f"C3/{args.hardware}" if args.compute == "c3" else "local Docker"
            print(f"  [BENCH] Running benchmark on {compute_label}…")
            send_heartbeat(server, agent_id, agent_token=agent_token)
            bench, build_err = run_benchmark(args, config, server)

            if bench is None:
                print(f"  [BENCH] FAILED — build_err: {build_err[:300]}")
                print(f"  [BENCH] Restoring previous code and continuing")
                if best_code:
                    files.write(best_code, best_kernel)
                _note_bench_failure()
                continue

            _print_bench_result(bench)
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
                    build_hypothesis_system_prompt(challenge_md, config, is_bootstrap=bootstrap, role=role, assigned_tag=assigned_tag),
                    build_hypothesis_user_prompt(state, config, role=role, assigned_tag=assigned_tag)
                    + _cleaner_size_warning(best_file_map, config),
                    args.api_base,
                )
                iter_input_tokens += hyp_usage["input_tokens"]
                iter_output_tokens += hyp_usage["output_tokens"]
            except Exception as e:
                # Local-only: LLM transport errors (rate limit, out of tokens,
                # provider 5xx) used to broadcast a chat message to the swarm
                # feed every time. That spammed every dashboard viewer when
                # an agent exhausted quota and entered a fast retry loop.
                # The local print + heartbeat absence is enough signal for
                # the contributor; the swarm doesn't need to hear about it.
                print(f"  [LLM] HYPOTHESIS FAILED: {e}")
                time.sleep(_ITERATION_BACKOFF_SECS)
                continue

            # An empty completion (the provider returned no text, not an error)
            # isn't a usable hypothesis — treat it like a transport failure and
            # retry next iteration rather than proceeding with a default.
            if not (hyp_response or "").strip():
                print("  [LLM] HYPOTHESIS FAILED: empty response from model — skipping iteration")
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
                challenge_md, files, role=role,
            )
            iter_input_tokens += gen_in
            iter_output_tokens += gen_out

            # Search/replace can legitimately produce no edits (the model
            # returned no blocks). A skip does NOT publish, so it never advances
            # the server's stagnation counter — a plateaued S/R agent would spin
            # forever, never getting reset with fresh code. After a few
            # consecutive skips, force a full rewrite so the agent produces
            # something, publishes, and lets stagnation → reset kick in.
            if not code:
                consecutive_sr_skips += 1
                if consecutive_sr_skips >= _SR_SKIP_FALLBACK:
                    print(f"  [SKIP] {consecutive_sr_skips} consecutive no-edit "
                          f"skips — forcing a full rewrite to break the stall")
                    code, new_kernel, gen_in, gen_out = _generate_code(
                        args, model, api_key, state, hypothesis, config,
                        challenge_md, files, role=role, force_full=True,
                    )
                    iter_input_tokens += gen_in
                    iter_output_tokens += gen_out
                    consecutive_sr_skips = 0
                if not code:
                    print(f"  [SKIP] No valid code produced — skipping to next iteration")
                    _note_bench_failure()
                    continue
            else:
                consecutive_sr_skips = 0

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
            post_message(server, agent_name, agent_id, f"Trying [{tag}] {title}",
                         challenge=iter_challenge,
                         agent_token=agent_token)
            send_heartbeat(server, agent_id, agent_token=agent_token)

            bench, build_err, code_changed, fix_in, fix_out = _benchmark_with_compile_fix(
                args, model, api_key, config, server,
                files,
            )
            iter_input_tokens += fix_in
            iter_output_tokens += fix_out

            if bench is None:
                print(f"  [BENCH] FAILED — build_err: {build_err[:300]}")
                print(f"  [BENCH] Restoring previous code and continuing")
                if best_code:
                    files.write(best_code, best_kernel)
                _note_bench_failure()
                continue

            _print_bench_result(bench)

            # ── Runtime error retry ────────────────────────────
            runtime_errors = bench.get("errors") or []
            if runtime_errors and not bench.get("feasible"):
                bench, rt_changed, rt_in, rt_out = _fix_runtime_errors(
                    args, model, api_key, config, server, agent_token, agent_id, challenge_md,
                    files, bench, best_code, best_kernel,
                )
                iter_input_tokens += rt_in
                iter_output_tokens += rt_out
                code_changed = code_changed or rt_changed

            if bench is None:
                print(f"  [BENCH] Benchmark failed after runtime fix — skipping iteration")
                _note_bench_failure()
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

        # A benchmark completed (both modes converge here with a real
        # `bench`) — the freeze guard's failure streak is over.
        bench_failures = 0

        # ── Hyperparameter search (gated) ──────────────────────
        # `bench` here is the default-config score (test seed). If the gate is
        # open and tuning beats the default on the test seed, `bench` becomes
        # the tuned score and the variant + winning config are published.
        # Capture the default (no-hyperparameters) score first: it's what the
        # server stores to keep the HPO band default-vs-default. It equals the
        # published score for untuned iterations and the pre-tuning score for
        # tuned ones.
        default_score = bench.get("score")
        bench, winning_hyperparameters, hpo_in, hpo_out = _maybe_tune_hyperparameters(
            args, model, api_key, config, server,
            files, challenge_md, bench,
            state.get("improvement_scores") or [],
            state.get("best_hyperparameters"),
            has_tuned=bool(state.get("has_tuned")),
            backend=backend, workdir=workdir,
        )
        iter_input_tokens += hpo_in
        iter_output_tokens += hpo_out

        # ── Publish ────────────────────────────────────────────
        iter_cost = estimate_cost(model, {
            "input_tokens": iter_input_tokens,
            "output_tokens": iter_output_tokens,
        })
        if iter_input_tokens == 0 and iter_output_tokens == 0:
            # claude-code / claude-code-agentic run via the `claude` CLI, which
            # doesn't surface token counts. Zeros here mean "not reported", not
            # "the model did nothing" — say so instead of a misleading $0.0000.
            print(f"  [TOKENS] not reported by {args.provider} provider")
        elif iter_cost == 0.0:
            # Tokens were spent but estimate_cost has no price entry for this
            # model, so it fell through to 0.0. Don't print a misleading
            # $0.0000 — flag the missing price and still show the token spend.
            print(f"  [TOKENS] in={iter_input_tokens:,}  out={iter_output_tokens:,}  "
                  f"est=unknown (no price entry for {model!r})")
        else:
            print(f"  [TOKENS] in={iter_input_tokens:,}  out={iter_output_tokens:,}  est=${iter_cost:.4f}")
        print(f"  [PUBLISH] Publishing results to server…")
        is_new_best = False
        try:
            result = publish_results(
                server, agent_id, bench, hypothesis, config,
                input_tokens=iter_input_tokens,
                output_tokens=iter_output_tokens,
                estimated_cost=iter_cost,
                agent_token=agent_token,
                hyperparameters=winning_hyperparameters,
                default_score=default_score,
                role=role,
            )
            is_new_best = result.get("is_new_best", False)
            if is_new_best:
                print("  [PUBLISH] ** NEW PERSONAL BEST! **")
            else:
                print(f"  [PUBLISH] Recorded (not a new best)")
        except Exception as e:
            # Surface the server's validation detail: a 422 body names the
            # exact field the schema rejected (e.g. "title too long"), which
            # the bare HTTPError str() drops — turning a mystery into a fix.
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:600]
                except Exception:
                    pass
            print(f"  [PUBLISH] FAILED: {e}" + (f" — {detail}" if detail else ""))

        # The HPO gate's improvement history is recorded server-side (this
        # publish sets beats_trajectory_best) and re-read from /api/state next
        # iteration — no local accumulation needed.

        status = "NEW BEST!" if is_new_best else f"score {_clean_score(bench.get('score', 0)):.0f}"
        feasible_str = "" if bench.get("feasible") else " (INFEASIBLE)"
        post_message(server, agent_name, agent_id,
                     f"[{tag}] {title} → {status}{feasible_str}",
                     challenge=bench.get("challenge") or iter_challenge,
                     agent_token=agent_token)
        send_heartbeat(server, agent_id, agent_token=agent_token)

        tk_in, tk_out = _distill_tacit_if_due(
            state, config, is_new_best,
            args.provider, model, api_key, args.api_base,
            files,
        )
        iter_input_tokens += tk_in
        iter_output_tokens += tk_out

        elapsed = time.time() - t_start
        print(f"  [DONE] Iteration {iteration} finished in {elapsed:.0f}s")
        print()

    print("Loop complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
