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

    # Resume a previous agent
    python scripts/run_loop.py --provider anthropic --agent-id <id> --agent-name <name>

API keys are read from the environment: ANTHROPIC_API_KEY, OPENAI_API_KEY,
GOOGLE_API_KEY (or pass --api-key directly).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


def algo_path(config: dict) -> Path:
    return ROOT / config.get("algorithm_path", "src/knapsack/algorithm/mod.rs")


def write_algorithm(code: str, config: dict) -> None:
    p = algo_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code)


def read_algorithm(config: dict) -> str:
    p = algo_path(config)
    return p.read_text() if p.exists() else ""


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


def build_hypothesis_system_prompt(challenge_md: str, config: dict) -> str:
    challenge = config.get("challenge", "unknown")
    tags = ", ".join(get_strategy_tags(config))
    return f"""\
You are planning an improvement to a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

Your job: propose ONE specific change to try. Do NOT write code — just describe the idea.

Respond in EXACTLY this format (4 lines, nothing else):

TITLE: <short title of what to change, under 80 chars>
DESCRIPTION: <2-3 sentence description of the change and reasoning>
STRATEGY_TAG: <one of: {tags}>
NOTES: <brief interpretation of your approach>"""


def build_hypothesis_user_prompt(state: dict) -> str:
    parts: list[str] = []

    code = state.get("best_algorithm_code") or ""
    if code:
        parts.append(f"Current algorithm:\n```rust\n{code}\n```")
    else:
        parts.append("No existing algorithm yet — starting from scratch.")

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

    reset = state.get("trajectory_reset")
    if reset:
        parts.append(f"\n** TRAJECTORY RESET — start fresh.")

    parts.append("\nPropose one specific improvement to try.")
    return "\n".join(parts)


def build_code_system_prompt(challenge_md: str, config: dict) -> str:
    challenge = config.get("challenge", "unknown")
    return f"""\
You are optimizing a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

The code must compile as valid Rust.

Rules:
- Do NOT change the `solve_challenge` function signature.
- `use super::*;` must remain as the first import.
- You may add, remove, or change any `use` statements, helper functions, \
structs, or code within `solve_challenge`.
- Return the complete file.

No explanation, no markdown fences — just the complete Rust source file."""


def build_code_user_prompt(state: dict, hypothesis: dict) -> str:
    parts: list[str] = []

    code = state.get("best_algorithm_code") or ""
    if code:
        parts.append(f"Current algorithm:\n```rust\n{code}\n```")
    else:
        parts.append(
            "No existing algorithm yet — write a complete solve_challenge "
            "implementation from scratch."
        )

    title = hypothesis.get("title", "")
    description = hypothesis.get("description", "")
    parts.append(f"\nApply this change:\n{title}\n{description}")

    return "\n".join(parts)


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


def parse_code(text: str) -> str:
    """Extract Rust code from the code LLM response."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


def validate_code(original: str, modified: str) -> str | None:
    """Basic sanity check on LLM-generated code.

    Returns None if valid, or an error description."""
    if "use super::*;" not in modified:
        return "`use super::*;` is missing — it must remain as the first import."
    if "fn solve_challenge(" not in modified:
        return "`fn solve_challenge(` not found — the function signature must not change."
    return None


# ── Benchmark & publish ─────────────────────────────────────────────


def run_benchmark() -> dict | None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0:
        print(f"  Benchmark failed:\n{result.stderr[-2000:]}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  Benchmark output not valid JSON:\n{result.stdout[:300]}", file=sys.stderr)
        return None


def publish_results(
    server: str, agent_id: str, bench: dict, mutation: dict, config: dict,
) -> dict:
    code = read_algorithm(config)
    payload = {
        "agent_id": agent_id,
        "title": mutation.get("title", ""),
        "description": mutation.get("description", ""),
        "strategy_tag": mutation.get("strategy_tag", "other"),
        "algorithm_code": code,
        "score": bench["score"],
        "feasible": bench["feasible"],
        "num_vehicles": bench.get("num_vehicles", 0),
        "total_distance": bench.get("total_distance", bench["score"]),
        "notes": mutation.get("notes", ""),
        "solution_data": bench.get("viz_data"),
        "track_scores": bench.get("track_scores"),
        "challenge": bench.get("challenge"),
    }
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
        "--provider", required=True, choices=["anthropic", "openai", "google"],
        help="LLM provider",
    )
    p.add_argument("--model", help="Model ID (default: per-provider sensible default)")
    p.add_argument("--api-key", help="API key (default: from env var)")
    p.add_argument("--api-base", help="Base URL for OpenAI-compatible endpoints")
    p.add_argument("--max-iterations", type=int, default=0, help="Stop after N iterations (0=unlimited)")
    p.add_argument("--agent-id", help="Resume with an existing agent ID")
    p.add_argument("--agent-name", help="Agent name (used with --agent-id)")
    return p.parse_args()


def resolve_api_key(args: argparse.Namespace) -> str:
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
    print(f"Challenge: {config.get('challenge', '?')}")
    print(f"Server: {server}")
    print()

    iteration = 0
    while args.max_iterations == 0 or iteration < args.max_iterations:
        iteration += 1
        print(f"{'=' * 60}")
        print(f"  Iteration {iteration}")
        print(f"{'=' * 60}")

        # ── Step 0: sync challenge ──────────────────────────────
        sync_challenge()
        config = load_config()
        challenge_md = read_challenge_md()

        # Fetch server-side swarm config for dynamic fields (strategy_tags).
        try:
            swarm_cfg = server_get(f"{server}/api/swarm_config")
            config["available_challenges"] = swarm_cfg.get("available_challenges", {})
        except Exception:
            pass

        # ── Step 1: get state ───────────────────────────────────
        try:
            state = get_state(server, agent_id)
        except Exception as e:
            print(f"  Failed to get state: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        my_score = state.get("my_best_score")
        global_best = state.get("best_score")
        stagnation = state.get("my_runs_since_improvement", 0)
        print(f"  My best: {my_score}  Global best: {global_best}  Stagnation: {stagnation}")

        reset = state.get("trajectory_reset")
        if reset:
            print(f"  ** TRAJECTORY RESET — {reset.get('type')} **")
            post_message(server, agent_name, agent_id,
                         f"Trajectory reset: {reset.get('type')}")

        # ── Step 2: write current best to mod.rs ────────────────
        best_code = state.get("best_algorithm_code") or ""
        if best_code:
            write_algorithm(best_code, config)

        # ── Step 3a: LLM hypothesis ────────────────────────────
        hint = state.get("stagnation_hint")
        if hint:
            print(f"  Stagnation hint: {hint}")

        print(f"  Generating hypothesis via {args.provider}/{model} ...")
        try:
            hyp_response = call_llm(
                args.provider, model, api_key,
                build_hypothesis_system_prompt(challenge_md, config),
                build_hypothesis_user_prompt(state),
                args.api_base,
            )
        except Exception as e:
            print(f"  Hypothesis LLM call failed: {e}", file=sys.stderr)
            post_message(server, agent_name, agent_id,
                         f"LLM call failed: {type(e).__name__}")
            time.sleep(5)
            continue

        hypothesis = parse_hypothesis(hyp_response)
        tag = hypothesis.get("strategy_tag", "?")
        title = hypothesis.get("title", "?")
        print(f"  Hypothesis: [{tag}] {title}")

        # ── Step 3b: LLM code (with retry on validation failure) ─
        original_code = best_code
        code = None
        max_code_attempts = 3
        for attempt in range(max_code_attempts):
            if attempt == 0:
                print(f"  Generating code via {args.provider}/{model} ...")
                user_prompt = build_code_user_prompt(state, hypothesis)
            else:
                print(f"  Retry {attempt}/{max_code_attempts - 1}: {violation}")
                user_prompt = (
                    build_code_user_prompt(state, hypothesis)
                    + f"\n\nYour previous response was rejected: {violation}\n"
                    "Fix the issue and return the complete Rust source file."
                )
            try:
                code_response = call_llm(
                    args.provider, model, api_key,
                    build_code_system_prompt(challenge_md, config),
                    user_prompt,
                    args.api_base,
                )
            except Exception as e:
                print(f"  Code LLM call failed: {e}", file=sys.stderr)
                post_message(server, agent_name, agent_id,
                             f"LLM call failed: {type(e).__name__}")
                time.sleep(5)
                break

            parsed = parse_code(code_response)
            if not parsed:
                print("  Empty code response — skipping iteration")
                break

            violation = validate_code(original_code, parsed)
            if violation:
                continue
            code = parsed
            break

        if not code:
            continue

        write_algorithm(code, config)

        # ── Step 4: benchmark ───────────────────────────────────
        print("  Running benchmark ...")
        post_message(server, agent_name, agent_id,
                     f"Trying [{tag}] {title}")

        send_heartbeat(server, agent_id)

        bench = run_benchmark()
        if bench is None:
            print("  Benchmark failed — restoring previous code and continuing")
            if best_code:
                write_algorithm(best_code, config)
            post_message(server, agent_name, agent_id,
                         f"[{tag}] {title} — benchmark failed (build error?)")
            continue

        print(f"  Score: {bench['score']}  Feasible: {bench['feasible']}")

        # ── Step 5: publish ─────────────────────────────────────
        is_new_best = False
        try:
            result = publish_results(server, agent_id, bench, hypothesis, config)
            is_new_best = result.get("is_new_best", False)
            if is_new_best:
                print("  ** NEW PERSONAL BEST! **")
        except Exception as e:
            print(f"  Publish failed: {e}", file=sys.stderr)

        # ── chat + heartbeat ────────────────────────────────────
        status = "NEW BEST!" if is_new_best else f"score {bench['score']:.0f}"
        feasible_str = "" if bench["feasible"] else " (INFEASIBLE)"
        post_message(server, agent_name, agent_id,
                     f"[{tag}] {title} → {status}{feasible_str}")
        send_heartbeat(server, agent_id)
        print()

    print("Loop complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
