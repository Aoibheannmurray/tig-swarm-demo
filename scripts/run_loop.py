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
import re
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


STRATEGY_TAGS = (
    "greedy, local_search, tabu, simulated_annealing, metaheuristic, "
    "construction, constraint_relaxation, decomposition, hybrid, "
    "data_structure, branch_and_bound, dp, other"
)


def build_system_prompt(challenge_md: str, config: dict) -> str:
    challenge = config.get("challenge", "unknown")
    path = config.get("algorithm_path", "src/unknown/algorithm/mod.rs")
    return f"""\
You are optimizing a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

Your task: improve the algorithm in `{path}`.

The function signature must remain exactly:
```rust
pub fn solve_challenge(
    challenge: &Challenge,
    save_solution: &dyn Fn(&Solution) -> Result<()>,
    hyperparameters: &Option<Map<String, Value>>,
) -> Result<()>
```

Rules:
- Call save_solution() every time you find an improved solution (hard timeout may kill the process).
- Only the most recent save_solution() call is kept — never save a worse solution after a better one.
- Single-threaded only — no std::thread, rayon, crossbeam, or async.
- The code must compile as valid Rust.

Respond in EXACTLY this format (metadata lines, then a --- separator, then the complete Rust file):

TITLE: <short title of what you changed, under 80 chars>
DESCRIPTION: <2-3 sentence description of the change and reasoning>
STRATEGY_TAG: <one of: {STRATEGY_TAGS}>
NOTES: <brief interpretation of your approach>
---
<complete Rust source file — all use statements, all functions, everything>"""


def build_user_prompt(state: dict) -> str:
    parts: list[str] = []

    score = state.get("my_best_score")
    best_global = state.get("best_score")
    runs = state.get("my_runs", 0)
    improvements = state.get("my_improvements", 0)
    stagnation = state.get("my_runs_since_improvement", 0)
    parts.append(
        f"Stats: score={score}, global_best={best_global}, "
        f"runs={runs}, improvements={improvements}, stagnation={stagnation}"
    )

    code = state.get("best_algorithm_code") or ""
    if code:
        parts.append(f"\nCurrent algorithm:\n```rust\n{code}\n```")
    else:
        parts.append(
            "\nNo existing algorithm yet — write a complete solve_challenge "
            "implementation from scratch."
        )

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = [f"\n{len(prior)} strategies already tried on this code — try something STRUCTURALLY DIFFERENT:"]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            sc = h.get("score", "?")
            lines.append(f"  - [{tag}] {title} (score: {sc})")
        parts.append("\n".join(lines))

    hint = state.get("stagnation_hint")
    if hint == "inspiration":
        insp = state.get("inspiration_code", "")
        name = state.get("inspiration_agent_name", "another agent")
        if insp:
            parts.append(
                f"\nYou are stagnating. Study {name}'s approach for ideas "
                f"(adapt ideas, do NOT copy wholesale):\n```rust\n{insp}\n```"
            )
    elif hint == "tacit_knowledge":
        tk = read_tacit_knowledge().strip()
        if tk:
            parts.append(f"\nYou are stagnating. Personal strategy hints:\n{tk}")
        else:
            insp = state.get("inspiration_code", "")
            name = state.get("inspiration_agent_name", "another agent")
            if insp:
                parts.append(
                    f"\nYou are stagnating. Study {name}'s approach for ideas:\n```rust\n{insp}\n```"
                )

    reset = state.get("trajectory_reset")
    if reset:
        parts.append(f"\n** TRAJECTORY RESET — {reset.get('type', 'unknown')} ** — start fresh.")

    parts.append("\nImprove this algorithm. Make a meaningful change that could improve the score.")
    return "\n".join(parts)


# ── Response parsing ────────────────────────────────────────────────


_DEFAULTS = {
    "title": "LLM mutation",
    "description": "Automated code improvement",
    "strategy_tag": "other",
    "notes": "",
}


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


def _parse_metadata(header: str) -> dict:
    meta = dict(_DEFAULTS)
    for line in header.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if key in meta and value:
                meta[key] = value
    return meta


def parse_response(text: str) -> dict:
    """Extract code + metadata from LLM response.

    Tries delimiter format first, then JSON, then code-block extraction.
    """
    # Strategy 1: delimiter format (TITLE: ...\n---\n<code>)
    if "\n---\n" in text:
        header, code = text.split("\n---\n", 1)
        meta = _parse_metadata(header)
        meta["code"] = _strip_code_fences(code)
        if meta["code"]:
            return meta

    # Strategy 2: JSON (some models prefer it)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "code" in data:
            for k, v in _DEFAULTS.items():
                data.setdefault(k, v)
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 3: JSON inside markdown fences
    json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if isinstance(data, dict) and "code" in data:
                for k, v in _DEFAULTS.items():
                    data.setdefault(k, v)
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 4: bare Rust code block
    code_match = re.search(r"```rust\s*\n(.*?)```", text, re.DOTALL)
    if code_match:
        return {**_DEFAULTS, "code": code_match.group(1).strip()}

    # Strategy 5: raw text as code
    return {**_DEFAULTS, "code": text.strip()}


# ── Benchmark & publish ─────────────────────────────────────────────


def run_benchmark() -> dict | None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0:
        print(f"  Benchmark failed:\n{result.stderr[:500]}", file=sys.stderr)
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
        "route_data": bench.get("viz_data") or bench.get("route_data"),
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

        # ── Step 3: LLM mutation ────────────────────────────────
        system_prompt = build_system_prompt(challenge_md, config)
        user_prompt = build_user_prompt(state)

        hint = state.get("stagnation_hint")
        if hint:
            print(f"  Stagnation hint: {hint}")

        print(f"  Calling {args.provider}/{model} ...")
        try:
            response = call_llm(
                args.provider, model, api_key,
                system_prompt, user_prompt, args.api_base,
            )
        except Exception as e:
            print(f"  LLM call failed: {e}", file=sys.stderr)
            post_message(server, agent_name, agent_id,
                         f"LLM call failed: {type(e).__name__}")
            time.sleep(5)
            continue

        mutation = parse_response(response)
        tag = mutation.get("strategy_tag", "?")
        title = mutation.get("title", "?")
        print(f"  Strategy: [{tag}] {title}")

        write_algorithm(mutation["code"], config)

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
            result = publish_results(server, agent_id, bench, mutation, config)
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
