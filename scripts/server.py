"""Swarm coordination server communication.

HTTP helpers and all API calls: agent registration, state polling,
heartbeats, chat messages, and result publishing.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from challenge_files import read_algorithm, read_optional, kernel_path

# Network-level errors that we'll log-and-swallow on fire-and-forget calls
# like heartbeats/messages. Programmer errors (KeyError, TypeError, etc.)
# still propagate so they aren't hidden.
_NET_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError)


# ── HTTP helpers ───────────────────────────────────────────────────


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


# ── Agent API ──────────────────────────────────────────────────────


def register_agent(server: str, config: dict | None = None) -> tuple[str, str]:
    body: dict = {}
    if config:
        if config.get("contributor_name"):
            body["agent_name"] = config["contributor_name"]
        if config.get("contributor_llm"):
            body["llm_type"] = config["contributor_llm"]
    data = server_post(f"{server}/api/agents/register", body)
    return data["agent_id"], data["agent_name"]


def get_state(server: str, agent_id: str) -> dict:
    return server_get(f"{server}/api/state?agent_id={urllib.parse.quote(agent_id)}")


def send_heartbeat(server: str, agent_id: str) -> None:
    try:
        server_post(
            f"{server}/api/agents/{urllib.parse.quote(agent_id)}/heartbeat",
            {"status": "working"}, timeout=5,
        )
    except _NET_ERRORS as e:
        print(f"  [WARN] heartbeat failed: {e}", file=sys.stderr)


def post_message(server: str, agent_name: str, agent_id: str, content: str) -> None:
    try:
        server_post(f"{server}/api/messages", {
            "agent_name": agent_name, "agent_id": agent_id,
            "content": content, "msg_type": "agent",
        }, timeout=5)
    except _NET_ERRORS as e:
        print(f"  [WARN] post_message failed: {e}", file=sys.stderr)


# ── Publish ────────────────────────────────────────────────────────


def publish_results(
    server: str, agent_id: str, bench: dict, mutation: dict, config: dict,
    *, input_tokens: int = 0, output_tokens: int = 0,
    estimated_cost: float = 0.0,
) -> dict:
    code = read_algorithm(config)
    kernel_code = read_optional(kernel_path(config))
    payload = {
        "agent_id": agent_id,
        "title": mutation.get("title", ""),
        "description": mutation.get("description", ""),
        "strategy_tag": mutation.get("strategy_tag", "other"),
        "algorithm_code": code,
        "score": bench.get("score", 0),
        "feasible": bench.get("feasible", False),
        "notes": mutation.get("notes", ""),
        "solution_data": bench.get("viz_data"),
        "track_scores": bench.get("track_scores"),
        "challenge": bench.get("challenge"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost": estimated_cost,
    }
    if kernel_code:
        payload["kernel_code"] = kernel_code
    if bench.get("challenge_metrics") is not None:
        payload["challenge_metrics"] = bench["challenge_metrics"]
    return server_post(f"{server}/api/iterations", payload)
