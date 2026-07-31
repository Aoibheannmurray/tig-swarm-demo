#!/usr/bin/env python3
"""Self-running tests for dump_trajectories.py's credential plumbing.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_dump_trajectories_auth.py

Why this exists: the tool asks for `include_code=true`, which the server gates
behind X-Username/X-Swarm-Password (`optional_swarm_password`) and answers with
a bare 403 otherwise. `fetch()` sent no headers and caught nothing, so the tool
died with an unhandled HTTPError on its first call — for long enough that it
read as unreferenced code. Nothing in the suite covered it.

Covers:
  - credentials are read from env first, then fleet.config.json
  - the derived password (fleet.config.json), never the base one
    (swarm.admin.json), is what gets sent
  - fetch() attaches both headers when they resolve, and stays anonymous when
    they don't (so the public endpoints still work)
  - a 403 exits with an actionable message instead of a traceback
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# resolve_server_url exits when nothing resolves; give it a value up front so
# importing the module under test never depends on the machine's swarm state.
os.environ.setdefault("TIG_SWARM_SERVER", "https://swarm.test")

import dump_trajectories as dt

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


class _EnvSandbox:
    """Clear the credential env vars and point ROOT at a temp dir."""

    def __enter__(self):
        self._env = {k: os.environ.pop(k, None)
                     for k in ("TIG_SWARM_USERNAME", "TIG_SWARM_PASSWORD")}
        self._root = dt.ROOT
        self._tmp = tempfile.TemporaryDirectory()
        dt.ROOT = Path(self._tmp.name)
        return dt.ROOT

    def __exit__(self, *exc):
        dt.ROOT = self._root
        self._tmp.cleanup()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_credentials_resolution() -> None:
    print("credential resolution")

    with _EnvSandbox() as root:
        check(dt._credentials() == ("", ""), "no env, no config -> empty")

        (root / "fleet.config.json").write_text(json.dumps({
            "server_url": "https://swarm.test",
            "username": "ada",
            # 64 hex chars: the DERIVED per-contributor password.
            "swarm_password": "a" * 64,
        }), encoding="utf-8")
        check(dt._credentials() == ("ada", "a" * 64),
              "falls back to fleet.config.json")

        os.environ["TIG_SWARM_USERNAME"] = "grace"
        os.environ["TIG_SWARM_PASSWORD"] = "b" * 64
        check(dt._credentials() == ("grace", "b" * 64),
              "env wins over fleet.config.json")

    with _EnvSandbox() as root:
        (root / "fleet.config.json").write_text("{not json", encoding="utf-8")
        check(dt._credentials() == ("", ""), "malformed config degrades quietly")


def test_fetch_sends_headers() -> None:
    print("fetch headers")

    seen: dict = {}
    original = dt.urllib.request.urlopen

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        seen["url"] = req.full_url
        return _Resp(b'{"trajectories": {}}')

    with _EnvSandbox():
        os.environ["TIG_SWARM_USERNAME"] = "ada"
        os.environ["TIG_SWARM_PASSWORD"] = "c" * 64
        dt.urllib.request.urlopen = fake_urlopen
        try:
            dt.fetch("/api/trajectory_experiments?include_code=true")
        finally:
            dt.urllib.request.urlopen = original

    # urllib title-cases header names on the Request object.
    hdrs = {k.lower(): v for k, v in seen["headers"].items()}
    check(hdrs.get("X-username".lower()) == "ada", "X-Username sent")
    check(hdrs.get("X-swarm-password".lower()) == "c" * 64, "X-Swarm-Password sent")
    check("include_code=true" in seen["url"], "include_code still requested")

    # Without credentials the request must still go out unauthenticated —
    # the base response is public and useful for the non-code endpoints.
    with _EnvSandbox():
        dt.urllib.request.urlopen = fake_urlopen
        try:
            dt.fetch("/api/trajectories")
        finally:
            dt.urllib.request.urlopen = original
    hdrs = {k.lower(): v for k, v in seen["headers"].items()}
    check("x-username" not in hdrs, "no bogus header when uncredentialed")


def test_403_is_actionable() -> None:
    print("403 handling")

    original = dt.urllib.request.urlopen

    def forbidden(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    with _EnvSandbox():
        dt.urllib.request.urlopen = forbidden
        try:
            dt.fetch("/api/trajectory_experiments?include_code=true")
            check(False, "403 raises SystemExit")
        except SystemExit as e:
            msg = str(e)
            check(True, "403 raises SystemExit")
            check("403" in msg and "credentials" in msg,
                  "message explains the cause")
            check("fleet.config.json" in msg and "swarm.admin.json" in msg,
                  "message names the right file, and the wrong one")
        except urllib.error.HTTPError:
            check(False, "403 raises SystemExit (got a raw HTTPError)")
        finally:
            dt.urllib.request.urlopen = original

    # A non-403 must not be swallowed by the same handler.
    def server_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Boom", {}, None)

    with _EnvSandbox():
        dt.urllib.request.urlopen = server_error
        try:
            dt.fetch("/api/trajectories")
            check(False, "500 propagates")
        except urllib.error.HTTPError as e:
            check(e.code == 500, "500 propagates")
        except SystemExit:
            check(False, "500 propagates (was swallowed as a 403)")
        finally:
            dt.urllib.request.urlopen = original


def main() -> int:
    test_credentials_resolution()
    test_fetch_sends_headers()
    test_403_is_actionable()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all dump_trajectories auth checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
