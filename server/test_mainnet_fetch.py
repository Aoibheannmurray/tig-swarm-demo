#!/usr/bin/env python3
"""Self-running tests for mainnet_seed's GitHub fetch path.

No pytest — run directly:  python server/test_mainnet_fetch.py

Why this exists: a five-challenge "Seed from mainnet" run 403'd partway
through. The old fetch walked the contents API — one request per directory
plus one PER FILE — against the anonymous quota of 60 requests/hour/IP,
which on a PaaS is shared with other tenants behind the same egress IP. The
bare "HTTP 403" it surfaced on a public repo read as a permissions bug.

Covers:
  - fetch_algorithm_files spends exactly ONE API request (the recursive tree
    listing); file contents come from raw.githubusercontent.com
  - nested paths keep their relative names; non-blob and out-of-dir entries
    are ignored
  - a truncated tree fails loudly instead of seeding a partial algorithm
  - quota exhaustion (403 + X-RateLimit-Remaining: 0) produces an actionable
    message naming the reset and GITHUB_TOKEN; a plain 403 stays a plain 403
  - GITHUB_TOKEN is attached to api.github.com requests only — never to the
    raw CDN
"""

from __future__ import annotations

import io
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mainnet_seed as ms

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tree(entries):
    return {"tree": entries, "truncated": False}


def test_one_api_call_rest_from_raw() -> None:
    print("request budget & mapping")
    calls: list[str] = []
    orig = ms.urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if "api.github.com" in url:
            import json
            return _Resp(json.dumps(_tree([
                {"path": "tig-algorithms/src/knapsack/kv1/mod.rs", "type": "blob"},
                {"path": "tig-algorithms/src/knapsack/kv1/sub/helper.rs", "type": "blob"},
                {"path": "tig-algorithms/src/knapsack/kv1/sub", "type": "tree"},
                {"path": "tig-algorithms/src/knapsack/OTHER/mod.rs", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ])).encode())
        return _Resp(f"content of {url.rsplit('/', 1)[-1]}".encode())

    ms.urllib.request.urlopen = fake_urlopen
    try:
        files = ms.fetch_algorithm_files("knapsack", "kv1")
    finally:
        ms.urllib.request.urlopen = orig

    api_calls = [c for c in calls if "api.github.com" in c]
    raw_calls = [c for c in calls if "raw.githubusercontent.com" in c]
    check(len(api_calls) == 1, f"exactly one API request ({len(api_calls)})")
    check("git/trees/knapsack%2Fkv1?recursive=1" in api_calls[0],
          "tree listing uses the url-encoded branch ref")
    check(len(raw_calls) == 2, "one raw fetch per in-dir blob")
    check(set(files) == {"mod.rs", "sub/helper.rs"},
          f"nested relpaths kept; other dirs and repo files ignored (got {set(files)})")
    check(files["mod.rs"] == "content of mod.rs", "raw content returned verbatim")


def test_truncated_tree_fails_loudly() -> None:
    print("truncated tree")
    orig = ms._get_json
    ms._get_json = lambda url, ua: {"tree": [], "truncated": True}
    try:
        ms.fetch_algorithm_files("knapsack", "kv1")
        check(False, "truncated listing raises")
    except ms.MainnetSeedError as e:
        check("truncated" in str(e), "truncated listing raises")
    finally:
        ms._get_json = orig


def test_rate_limit_message() -> None:
    print("rate-limit reporting")

    def http_error(code, headers):
        import email.message
        m = email.message.Message()
        for k, v in headers.items():
            m[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code, "x", m, None)

    orig = ms.urllib.request.urlopen

    def limited(req, timeout=None):
        raise http_error(403, {"X-RateLimit-Remaining": "0",
                               "X-RateLimit-Reset": "0"})

    ms.urllib.request.urlopen = limited
    try:
        ms._get("https://api.github.com/x", "ua", accept="application/json")
        check(False, "quota 403 raises")
    except ms.MainnetSeedError as e:
        msg = str(e)
        check("rate limit" in msg, "quota 403 names the rate limit")
        check("GITHUB_TOKEN" in msg, "message says how to raise the quota")
        check("60 requests/hour" in msg, "message explains the anonymous budget")
    finally:
        ms.urllib.request.urlopen = orig

    # A 403 that is NOT quota (remaining > 0) must stay a plain HTTP error —
    # masking a real permissions problem as "rate limited" would be worse.
    def forbidden(req, timeout=None):
        raise http_error(403, {"X-RateLimit-Remaining": "42"})

    ms.urllib.request.urlopen = forbidden
    try:
        ms._get("https://api.github.com/x", "ua", accept="application/json")
        check(False, "plain 403 raises")
    except ms.MainnetSeedError as e:
        check("HTTP 403" in str(e) and "rate limit" not in str(e),
              "plain 403 not misreported as rate limiting")
    finally:
        ms.urllib.request.urlopen = orig


def test_token_scoped_to_api_host() -> None:
    print("token scoping")
    seen: dict[str, dict] = {}
    orig = ms.urllib.request.urlopen

    def capture(req, timeout=None):
        seen[req.full_url] = dict(req.headers)
        return _Resp(b"{}")

    ms.urllib.request.urlopen = capture
    old_env = os.environ.get("GITHUB_TOKEN")
    os.environ["GITHUB_TOKEN"] = "ghp_test123"
    try:
        ms._get("https://api.github.com/repos/x", "ua", accept="application/json")
        ms._get("https://raw.githubusercontent.com/x/y", "ua", accept="*/*")
    finally:
        ms.urllib.request.urlopen = orig
        if old_env is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = old_env

    api_h = {k.lower(): v for k, v in seen["https://api.github.com/repos/x"].items()}
    raw_h = {k.lower(): v for k, v in seen["https://raw.githubusercontent.com/x/y"].items()}
    check(api_h.get("authorization") == "Bearer ghp_test123",
          "token sent to api.github.com")
    check("authorization" not in raw_h, "token NEVER sent to the raw CDN")
    check("user-agent" in api_h, "explicit User-Agent kept (CDN rejects bare urllib)")


def main() -> int:
    test_one_api_call_rest_from_raw()
    test_truncated_tree_fails_loudly()
    test_rate_limit_message()
    test_token_scoped_to_api_host()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all mainnet-fetch checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
