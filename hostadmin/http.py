"""Shared HTTP helpers: the POST-JSON request pattern (previously
copy-pasted at every server call site) and the mainnet API GET."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# Statuses that always mean "the edge/app is momentarily unavailable", never
# "your request was wrong" — safe to retry an idempotent POST against.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def looks_like_platform_error(detail: str) -> bool:
    """True if an error body came from the PLATFORM EDGE (Railway's router)
    rather than from our FastAPI app.

    This distinction is load-bearing. Railway answers `404 Application not
    found` — complete with a `request_id` — whenever the service has no
    routable deployment: the seconds-to-minutes window during a rollout,
    restart, or crash-loop. That is a *transient* 404, but it is
    indistinguishable by status code from our app's genuine "this route does
    not exist" 404, which is what an old server image returns for an endpoint
    it predates. Conflating the two is what let a whole `setup.py create` run
    silently no-op: every seed POST hit the edge mid-rollout, and the verifier
    read the 404 as "old server, can't check" and declared success.

    Our app always answers with FastAPI's `{"detail": ...}` shape; the edge
    never does. So: an error body that parses as JSON and carries no `detail`
    key is the edge's, as is any body carrying the tell-tale phrase."""
    if not detail:
        # An empty body is not our app — FastAPI always serialises `detail`.
        return True
    if "Application not found" in detail or "Application failed to respond" in detail:
        return True
    try:
        body = json.loads(detail)
    except (ValueError, TypeError):
        # HTML error pages (proxies, CDNs) are never ours.
        return "<html" in detail.lower()
    return isinstance(body, dict) and "detail" not in body


def classify_http_error(e: urllib.error.HTTPError) -> tuple[bool, str]:
    """Read an HTTPError body ONCE and return `(retryable, detail)`.

    Reading is destructive, so every caller that wants both the retry decision
    and a message for the operator must go through here. Retryable means the
    request never reached the app (edge error) or the app was unhealthy —
    never a 400/401/403, where retrying just repeats a bad admin key."""
    try:
        detail = e.read().decode(errors="replace")[:200]
    except Exception:
        detail = ""
    if e.code in RETRYABLE_STATUSES:
        return True, detail
    if e.code == 404:
        return looks_like_platform_error(detail), detail
    return False, detail


def post_json(url: str, payload: dict, *, timeout: int = 10) -> dict:
    """POST `payload` as JSON to `url` and JSON-decode the response body.

    This is the one shared POST helper — callers keep their own
    `urllib.error.HTTPError` / `URLError` except-blocks so each command's
    error messages and exit codes stay exactly as they were."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


_MAINNET_API = "https://mainnet-api.tig.foundation"


def _mainnet_get(url: str, *, timeout: int = 8) -> object:
    """GET + JSON-decode a mainnet API endpoint.

    Bare `urllib.request.urlopen` ships `Python-urllib/3.X` which the CDN
    in front of mainnet-api.tig.foundation rejects with HTTP 403, so we
    set an explicit User-Agent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tig-swarm-demo-setup",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
