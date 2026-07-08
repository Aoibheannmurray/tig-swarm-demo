"""Enrollment guardrails for the hosted runner (P3).

Hosted fleets run on the runner's box, so the rules are stricter than a local
fleet's:

  * C3-only compute — the runner has no Docker, and keeping compute off-box
    means LLM-authored code is *submitted* to C3, never executed on the runner.
    That's what lets us skip per-contributor OS sandboxing (plan §8).
  * No agentic providers — claude-code / codex need interactive CLI logins the
    runner can't perform.
  * Agent-count caps — one contributor can't monopolize the shared box.

These run at enroll time so a bad plan is rejected with an actionable message
rather than failing halfway through a launch.
"""

from __future__ import annotations

import os

# Providers that drive a local interactive CLI (its own OAuth/subscription
# login), which a headless runner cannot satisfy.
AGENTIC_PROVIDERS = frozenset({"claude-code", "claude-code-agentic", "codex-agentic"})


def max_agents_per_contributor() -> int:
    try:
        return int(os.environ.get("RUNNER_MAX_AGENTS_PER_CONTRIBUTOR", "8"))
    except ValueError:
        return 8


def max_total_agents() -> int:
    try:
        return int(os.environ.get("RUNNER_MAX_TOTAL_AGENTS", "64"))
    except ValueError:
        return 64


class EnrollmentError(ValueError):
    """A plan/keys combination the runner won't accept. The message is
    contributor-facing (surfaced verbatim by the service as a 4xx detail)."""


def validate_plan(config: dict, *, existing_total_agents: int = 0) -> list[dict]:
    """Return the agents list if the plan can run hosted, else raise
    EnrollmentError. `existing_total_agents` is the agent count already
    enrolled by *other* contributors, for the global ceiling check."""
    agents = (config or {}).get("agents") or []
    if not agents:
        raise EnrollmentError(
            "Your fleet has no agents. Add some under “My fleet” first."
        )

    per_cap = max_agents_per_contributor()
    if len(agents) > per_cap:
        raise EnrollmentError(
            f"Hosted fleets are capped at {per_cap} agents per contributor "
            f"(you have {len(agents)}). Trim your fleet, or run the extras "
            "locally with `python run.py --join`."
        )

    total_cap = max_total_agents()
    if existing_total_agents + len(agents) > total_cap:
        raise EnrollmentError(
            "This swarm's hosted runner is at capacity right now. Try fewer "
            "agents, or run locally with `python run.py --join`."
        )

    for agent in agents:
        name = agent.get("name") or "?"
        provider = (agent.get("provider") or "").strip()
        if provider in AGENTIC_PROVIDERS:
            raise EnrollmentError(
                f"Agent “{name}” uses {provider}, which needs an interactive "
                "CLI login and can't run on the hosted runner. Use an API "
                "provider (Anthropic / OpenAI / Google / OpenRouter) for "
                "hosted agents, or run this agent locally."
            )
        compute = (agent.get("compute") or "").strip()
        if compute != "c3":
            raise EnrollmentError(
                f"Agent “{name}” is set to “{compute or 'local'}” compute. "
                "Hosted agents must use C3 cloud benchmarking (no Docker on "
                "the runner). Set compute to “c3”."
            )
    return agents


def required_env_vars(agents: list[dict]) -> set[str]:
    """The env-var names the plan's agents reference for their LLM keys, plus
    C3_API_KEY (every hosted agent benchmarks on C3). The service checks the
    submitted key bundle covers these."""
    names = {"C3_API_KEY"}
    for agent in agents:
        env = (agent.get("api_key_env") or "").strip()
        if env:
            names.add(env)
    return names
