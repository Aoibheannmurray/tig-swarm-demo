"""Swarm coordination server communication.

HTTP helpers and all API calls: agent registration, state polling,
heartbeats, chat messages, and result publishing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from challenge_files import read_algorithm, read_optional, kernel_path, read_files

_ROOT = Path(__file__).resolve().parent.parent

# Network-level errors that we'll log-and-swallow on fire-and-forget calls
# like heartbeats/messages. Programmer errors (KeyError, TypeError, etc.)
# still propagate so they aren't hidden.
_NET_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError)


# ── HTTP helpers ───────────────────────────────────────────────────


def server_post(
    url: str, payload: dict, timeout: int = 10,
    *,
    username: str | None = None,
    swarm_password: str | None = None,
    agent_token: str | None = None,
) -> dict:
    # `username` + `swarm_password` gate /api/agents/register (the server
    # recomputes sha256(username + ':' + base_password) and compares).
    # `agent_token` gates every other participant-write endpoint. The two
    # credential shapes are intentionally separate headers so a client
    # mixing them up gets a 403 rather than silently using the wrong one.
    headers = {"Content-Type": "application/json"}
    if username:
        headers["X-Username"] = username
    if swarm_password:
        headers["X-Swarm-Password"] = swarm_password
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def server_get(
    url: str, timeout: int = 10,
    *, headers: dict[str, str] | None = None,
) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def resolve_server_url(script: str = "swarm_client", *, required: bool = True) -> str:
    """Resolve the swarm server URL: $TIG_SWARM_SERVER wins, then the
    `server_url` persisted in .swarm-cache.json by `setup.py sync`.

    Values starting with `$` (an unexpanded template placeholder) are
    rejected. When nothing resolves: exits with a clear message naming the
    calling `script`, or returns "" when `required=False` (callers that can
    run offline, e.g. benchmark.py's advisory server probe).
    """
    env = os.environ.get("TIG_SWARM_SERVER", "")
    if env and not env.startswith("$"):
        return env.rstrip("/")
    cfg_path = _ROOT / ".swarm-cache.json"
    if cfg_path.exists():
        try:
            url = json.loads(cfg_path.read_text()).get("server_url", "")
            if url and not url.startswith("$"):
                return url.rstrip("/")
        except Exception:
            pass
    if not required:
        return ""
    sys.exit(
        f"{script}: server URL not configured. "
        "Run `python setup.py sync` (or set TIG_SWARM_SERVER)."
    )


# ── Agent API ──────────────────────────────────────────────────────


_AGENTIC_PROVIDERS = ("claude-code-agentic", "codex-agentic")


def derive_llm_label(provider: str | None, model: str | None) -> str:
    """Dashboard label inferred from what the loop is actually running.

    The model name is the most informative bit (`claude-opus-4-7`,
    `gpt-5`, `gemini-2.5-pro`), so we lead with it. For agentic providers
    we append the provider in parens so the dashboard can distinguish
    e.g. `claude-opus-4-7` (single-shot API) from `claude-opus-4-7 (claude-
    code-agentic)`. When the model is unspecified (the CLI is using its
    own default), the provider name alone is the best we can do.
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    if model:
        if provider in _AGENTIC_PROVIDERS:
            return f"{model} ({provider})"
        return model
    return provider or "unknown"


def register_agent(
    server: str,
    *, provider: str | None = None, model: str | None = None,
    requested_name: str | None = None,
    name: str | None = None,
    username: str | None = None,
    swarm_password: str | None = None,
) -> tuple[str, str, str]:
    """Register an agent. Forwards a dashboard label as `llm_type`.

    Sends X-Username + X-Swarm-Password — the server validates that the
    derived password (sha256(username + ':' + base)) matches the value
    issued by `setup.py invite`. Returns (agent_id, agent_name,
    agent_token); the token gates every subsequent write call.

    Identity resolution (in order):
      - `requested_name` wins (used on re-registration to keep the same
        identity when the server has lost the original row).
      - explicit `name` kwarg (from agent.config.json's `name`, materialized
        from fleet.config.json).

    Dashboard label is auto-derived from provider+model.
    """
    body: dict = {}
    resolved_name = (requested_name or "").strip() or (name or "").strip()
    if resolved_name:
        body["agent_name"] = resolved_name

    body["llm_type"] = derive_llm_label(provider, model)
    # Structured provider/model let the server auto-classify the model tier
    # (frontier/standard) precisely instead of parsing the llm_type label.
    if provider:
        body["provider"] = provider
    if model:
        body["model"] = model

    data = server_post(
        f"{server}/api/agents/register", body,
        username=username,
        swarm_password=swarm_password,
    )
    return data["agent_id"], data["agent_name"], data["agent_token"]


def get_state(
    server: str, agent_id: str, role: str | None = None,
    *, seeded_start: bool | None = None, agent_token: str | None = None,
) -> dict:
    # /api/state?agent_id=X is authenticated: the server requires an
    # X-Agent-Token header resolving to agent X (403 otherwise). The
    # agent_id-less form of /api/state stays public.
    url = f"{server}/api/state?agent_id={urllib.parse.quote(agent_id)}"
    if role:
        url += f"&role={urllib.parse.quote(role)}"
    # Contributor-owned seeding override (`seeded_start` in fleet.config.json).
    # Reported each poll like `role`; omitted entirely when unset so the
    # server applies its tier/role auto policy.
    if seeded_start is not None:
        url += f"&seeded_start={'true' if seeded_start else 'false'}"
    headers = {"X-Agent-Token": agent_token} if agent_token else None
    # /api/state does real work server-side (stagnation resets, seed
    # selection, pool deposits) and shares the SQLite writer with every
    # other agent's publish, so >10s stalls are routine on a busy swarm.
    # A failed fetch costs the caller a whole iteration slot, so retry
    # with a generous timeout before giving up. Safe to repeat: a reset
    # triggered by the first (timed-out) call zeroes the stagnation
    # counter, so the retry just reads the post-reset state.
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            return server_get(url, timeout=60, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                # A 4xx is a real answer, not a stall — e.g. agent_exists
                # depends on seeing the 403 immediately to trigger
                # re-registration.
                raise
            last_err = e
        except _NET_ERRORS as e:
            last_err = e
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    raise last_err


def agent_exists(server: str, agent_id: str, agent_token: str | None) -> bool:
    """True if the server still has an `agents` row for this id.

    Probes via /api/state — the server returns `agent_name="unknown"`
    when there's no row for the supplied id (see `get_agent_name` in the
    server package). A 403 means the token no longer resolves to this
    agent (revoked, or the row is gone), so we return False to trigger a
    re-register. On other transport failures we return True so a flaky
    network doesn't trigger a spurious re-register.
    """
    try:
        state = get_state(server, agent_id, agent_token=agent_token)
    except urllib.error.HTTPError as e:
        return e.code != 403
    except _NET_ERRORS:
        return True
    return (state.get("agent_name") or "").strip() != "unknown"


class AgentTokenRevoked(Exception):
    """Server rejected the stored agent_token with 403.

    Raised by validate_agent_token so the loop can bail out with a clear
    message before spending an LLM call on a worker whose access has been
    cut (admin revoke, manual DB edit, etc.).
    """


def validate_agent_token(
    server: str, agent_id: str, agent_token: str,
) -> None:
    """Confirm the stored agent_token still authenticates against the server.

    POSTs a heartbeat — the cheapest authenticated endpoint we have, with
    side effects limited to bumping last_heartbeat. Without this, a revoked
    worker (token cleared by /api/admin/revoke) clears agent_exists, runs
    a full LLM iteration, and only learns it's been cut when the trailing
    post_message/heartbeat returns 403.

    Raises AgentTokenRevoked on a 403 response. Returns normally on
    success or on transport failure — same fail-open policy as
    agent_exists so a flaky network doesn't lock anyone out.
    """
    try:
        server_post(
            f"{server}/api/agents/{urllib.parse.quote(agent_id)}/heartbeat",
            {"status": "working"}, timeout=5,
            agent_token=agent_token,
        )
    except urllib.error.HTTPError as e:
        if e.code == 403:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise AgentTokenRevoked(detail.strip() or "agent token rejected") from e
        # Non-403 HTTP errors (5xx, transient 4xx) — treat as transport.
    except _NET_ERRORS:
        pass


def send_heartbeat(
    server: str, agent_id: str,
    *, agent_token: str | None = None,
) -> None:
    try:
        server_post(
            f"{server}/api/agents/{urllib.parse.quote(agent_id)}/heartbeat",
            {"status": "working"}, timeout=5,
            agent_token=agent_token,
        )
    except _NET_ERRORS as e:
        print(f"  [WARN] heartbeat failed: {e}", file=sys.stderr)


def post_message(
    server: str, agent_name: str, agent_id: str, content: str,
    *, challenge: str | None = None,
    agent_token: str | None = None,
) -> None:
    """Post a chat-feed message. When `challenge` is provided it pins the
    message to that challenge's feed; otherwise the server falls back to
    its current `active_challenge`. Callers inside an iteration loop
    should always pass the iteration's challenge so a host-side
    `setup.py switch` mid-benchmark can't reroute the message to the
    wrong feed."""
    payload = {
        "agent_name": agent_name, "agent_id": agent_id,
        "content": content, "msg_type": "agent",
    }
    if challenge:
        payload["challenge"] = challenge
    try:
        server_post(
            f"{server}/api/messages", payload,
            timeout=5, agent_token=agent_token,
        )
    except _NET_ERRORS as e:
        print(f"  [WARN] post_message failed: {e}", file=sys.stderr)


def post_failure_record(
    server: str, agent_id: str,
    *, kind: str, challenge: str | None = None,
    agent_token: str | None = None,
    lesson: str = "", approach_summary: str = "", what_was_tried: str = "",
    observed_outcome: str = "", possible_reasons: str = "",
) -> None:
    """Post an LLM-authored failed-attempt artifact to the server's
    failed-attempts archive (kind='retrospective' or 'lesson').

    Fire-and-forget: offline / old servers (404) / a server with the
    archive toggled off (`stored: False`) are all logged and ignored —
    the archive is additive telemetry and must never fail an iteration."""
    payload = {
        "agent_id": agent_id, "kind": kind,
        "approach_summary": approach_summary,
        "what_was_tried": what_was_tried,
        "observed_outcome": observed_outcome,
        "possible_reasons": possible_reasons,
        "lesson": lesson,
    }
    if challenge:
        payload["challenge"] = challenge
    try:
        resp = server_post(
            f"{server}/api/failure_records", payload,
            timeout=10, agent_token=agent_token,
        )
        if not resp.get("stored"):
            print(
                f"  [FAILARC] record not stored "
                f"({resp.get('reason', 'unknown')})",
            )
    except _NET_ERRORS as e:
        print(f"  [FAILARC] post failed: {e}", file=sys.stderr)


# ── Publish ────────────────────────────────────────────────────────


def publish_results(
    server: str, agent_id: str, bench: dict, mutation: dict, config: dict,
    *, input_tokens: int = 0, output_tokens: int = 0,
    estimated_cost: float = 0.0,
    agent_token: str | None = None,
    hyperparameters: dict | None = None,
    default_score: float | None = None,
    role: str | None = None,
    iteration_type: str | None = None,
) -> dict:
    code = read_algorithm(config)
    kernel_code = read_optional(kernel_path(config))
    # Coerce the agent-controlled fields to what the server's IterationCreate
    # schema accepts, so a stray value can't 422 the whole publish (losing the
    # iteration's score + hypothesis). `title` is capped at the server's
    # MAX_LABEL_LEN — agents are ASKED for a short title but LLMs sometimes
    # overrun it; `score` is coerced to a float (the schema requires one, and
    # a failed/None benchmark score would otherwise be rejected).
    _MAX_LABEL_LEN = 300  # mirrors server/models.py MAX_LABEL_LEN
    title = str(mutation.get("title") or "")[:_MAX_LABEL_LEN]
    try:
        score = float(bench.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    payload = {
        "agent_id": agent_id,
        "title": title,
        "description": str(mutation.get("description") or ""),
        "strategy_tag": mutation.get("strategy_tag", "other"),
        "algorithm_code": code,
        "score": score,
        "feasible": bool(bench.get("feasible", False)),
        "notes": str(mutation.get("notes") or ""),
        "solution_data": bench.get("viz_data"),
        "track_scores": bench.get("track_scores"),
        # Always attribute the result to the challenge it was benchmarked on.
        # `bench["challenge"]` is stamped by benchmark.py; fall back to the
        # synced config's challenge (same iteration, so identical) rather than
        # ever sending null — the server refuses a challenge-less publish
        # instead of inferring it from the (possibly since-switched) active
        # challenge.
        "challenge": bench.get("challenge") or config.get("challenge"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost": estimated_cost,
    }
    # Tag the hypothesis with the agent's role (explorer/exploiter) at publish
    # time so the server/dashboard can attribute work by role. Omitted => server
    # treats it as unset.
    if role:
        payload["role"] = role
    if kernel_code:
        payload["kernel_code"] = kernel_code
    # Full multi-file algorithm as a {relpath: content} map so multi-file
    # algorithms round-trip intact (seed pool / adoption / inspiration). Only
    # send it when it carries more than the single entry file — a single-file
    # algorithm is fully represented by `algorithm_code` above, so omitting the
    # map keeps the payload small and old servers happy.
    file_map = read_files(config)
    if len(file_map) > 1:
        payload["algorithm_files"] = file_map
    # The winning hyperparameter config when this iteration was tuned (the
    # score above is the tuned score), else None — the algorithm scored at its
    # in-code defaults. Lets the dashboard / a resuming process recover the
    # config the published score was achieved with.
    if hyperparameters is not None:
        payload["hyperparameters"] = hyperparameters
    # The no-hyperparameters score; lets the server keep the HPO band
    # default-vs-default (it differs from `score` only when this iteration was
    # tuned). Omitted => server falls back to `score`.
    if default_score is not None:
        payload["default_score"] = default_score
    if bench.get("challenge_metrics") is not None:
        payload["challenge_metrics"] = bench["challenge_metrics"]
    # "refactor" marks a behavior-preserving bloat reduction (cleaner —
    # docs/cleaner-agent-plan.md): the server swaps the trajectory-best code
    # but keeps the recorded score, counting it as neither improvement nor
    # stagnation. Omitted => "mutation" (server default).
    if iteration_type is not None:
        payload["iteration_type"] = iteration_type
    # Publish carries the full algorithm source + bench artifacts and is the
    # only call that loses work on timeout (score + hypothesis never reach
    # the dashboard, while the local code is overwritten next iteration). A
    # generous ceiling absorbs the slow-but-eventually-responds case we
    # actually observed in the wild without slowing the happy path — the
    # call still returns the moment the server responds.
    return server_post(
        f"{server}/api/iterations", payload,
        agent_token=agent_token,
        timeout=30,
    )
