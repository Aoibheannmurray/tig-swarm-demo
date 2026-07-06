"""Shared HTTP helpers: the POST-JSON request pattern (previously
copy-pasted at every server call site) and the mainnet API GET."""

from __future__ import annotations

import json
import urllib.request


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
