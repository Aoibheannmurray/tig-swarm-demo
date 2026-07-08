"""Tests for `setup.py set-runner` (enable the zero-install cloud tier).

Stubs the admin-creds lookup + HTTP POST + admin.json write, so it verifies
run_set_runner's wiring (endpoint, query encoding, swarm.admin.json mirror,
validation) without a server or filesystem writes.

Self-running: `python scripts/test_set_runner.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hostadmin import contributors as C  # noqa: E402


def _stub(monkey: dict):
    """Install stubs on the contributors module; return captured state."""
    cap = {"posts": [], "written": None}
    C._admin_creds = lambda cmd: ({"admin_key": "admin-key", "server_url": "https://swarm.app"},
                                  "admin-key", "https://swarm.app")
    C.post_json = lambda url, payload, **k: cap["posts"].append((url, payload)) or {"updated": True}
    C.write_swarm_admin = lambda admin: cap.__setitem__("written", dict(admin))
    return cap


def test_set_runner_posts_and_mirrors():
    cap = _stub({})
    rc = C.run_set_runner("https://my-runner.up.railway.app/")
    assert rc == 0
    url, payload = cap["posts"][0]
    # runner_url written as a config key, URL-encoded in the query, admin_key in body.
    assert url == ("https://swarm.app/api/admin/config"
                   "?key=runner_url&value=https%3A%2F%2Fmy-runner.up.railway.app"), url
    assert payload == {"admin_key": "admin-key"}, payload
    # Mirrored into swarm.admin.json (trailing slash trimmed) for revoke teardown.
    assert cap["written"]["runner_url"] == "https://my-runner.up.railway.app", cap["written"]
    print("PASS test_set_runner_posts_and_mirrors")


def test_unset_with_empty_url():
    cap = _stub({})
    rc = C.run_set_runner("")
    assert rc == 0
    url, _ = cap["posts"][0]
    assert url.endswith("?key=runner_url&value="), url  # empty value = unset
    assert cap["written"]["runner_url"] == "", cap["written"]
    print("PASS test_unset_with_empty_url")


def test_rejects_non_http_url():
    cap = _stub({})
    rc = C.run_set_runner("my-runner.up.railway.app")  # no scheme
    assert rc == 1
    assert cap["posts"] == [] and cap["written"] is None, "must not POST/write on bad input"
    print("PASS test_rejects_non_http_url")


if __name__ == "__main__":
    test_set_runner_posts_and_mirrors()
    test_unset_with_empty_url()
    test_rejects_non_http_url()
    print("ALL PASS")
