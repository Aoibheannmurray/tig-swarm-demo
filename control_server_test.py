#!/usr/bin/env python3
"""Self-running tests for the local control-plane companion (control_server.py).

No pytest in this repo — run directly:

    python control_server_test.py

Uses Starlette's TestClient and redirects the fleet-config / tacit file paths to
a temp dir so the real (gitignored) fleet.config.json / tacit_knowledge.md are
never touched. Fleet-start and Railway provisioning are NOT exercised here (they
spawn subprocesses / hit Railway); those are covered by scripts/test_fleet_core.py
and manual end-to-end verification.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import control_server
import init_fleet

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def main() -> int:
    try:
        from starlette.testclient import TestClient
    except Exception as e:  # pragma: no cover
        print(f"  [skip] TestClient unavailable ({e})")
        return 0

    tmp = Path(tempfile.mkdtemp())
    fleet_path = tmp / "fleet.config.json"
    tacit_path = tmp / "tacit_knowledge.md"
    # Redirect writable paths away from the real repo files.
    control_server.FLEET_CONFIG_PATH = fleet_path
    control_server.TACIT_PATH = tacit_path
    init_fleet.FLEET_CONFIG_PATH = fleet_path

    app = control_server.create_app()
    # base_url must be loopback: the DNS-rebinding guard rejects any other
    # Host header (TestClient's default is "testserver").
    with TestClient(app, base_url="http://127.0.0.1") as c:
        print("read-only endpoints")
        check(c.get("/local-api/env", headers={"host": "evil.example"}).status_code == 403,
              "non-loopback Host rejected (403, DNS-rebinding guard)")
        check(c.get("/local-api/env").json()["mode"] == "local", "env reports local mode")
        p = c.get("/local-api/providers").json()
        check(len(p["providers"]) > 0 and len(p["c3_hardware"]) > 0, "providers + hardware listed")
        ch = c.get("/local-api/challenges").json()
        check("cpu" in ch and "gpu" in ch, "challenges split cpu/gpu")

        print("fleet config write/read")
        r = c.post("/local-api/fleet/config", json={
            "server_url": "https://x.up.railway.app", "username": "me",
            "swarm_password": "pw", "provider": "claude-code", "count": 1,
        })
        check(r.status_code == 200, "valid fleet config accepted")
        check(fleet_path.exists(), "fleet.config.json written to temp path")
        check(c.get("/local-api/fleet/config").json()["exists"] is True, "config read back")
        rb = c.post("/local-api/fleet/config", json={"provider": "claude-code"})
        check(rb.status_code == 400, "invalid fleet config rejected (400)")

        print("tacit append")
        rt = c.post("/local-api/tacit", json={"answers": [{"title": "Q", "body": "a lesson"}]})
        check(rt.status_code == 200, "tacit guided answers accepted")
        check(tacit_path.exists() and "a lesson" in tacit_path.read_text(), "tacit written to temp path")
        rt2 = c.post("/local-api/tacit", json={"text": ""})
        check(rt2.status_code == 400, "empty tacit rejected (400)")

        print("invite derivation")
        ri = c.post("/local-api/invite", json={"username": "alice", "swarm_password": "base123"})
        import hashlib
        expected = hashlib.sha256(b"alice:base123").hexdigest()
        check(ri.json()["swarm_password"] == expected, "invite matches sha256(username:base)")
        check(c.post("/local-api/invite", json={"swarm_password": "b"}).status_code == 400,
              "invite without username rejected")

        print("fleet status / websocket")
        check(c.get("/local-api/fleet/status").json()["state"] == "idle", "fleet idle before start")
        # TestClient stamps Host "testserver" on WS handshakes regardless of
        # base_url, so the accepted cases set a loopback Host explicitly.
        with c.websocket_connect("/local-api/stream",
                                 headers={"host": "127.0.0.1",
                                          "origin": "http://127.0.0.1"}):
            check(True, "event stream websocket connects (loopback Origin)")
        with c.websocket_connect("/local-api/stream",
                                 headers={"host": "127.0.0.1"}):
            check(True, "event stream websocket connects (no Origin — CLI client)")
        # The HTTP middleware doesn't cover WebSockets, so the handler itself
        # must reject a rebinding page's handshake (bad Origin / bad Host).
        for label, headers in (
            ("bad Origin", {"host": "127.0.0.1", "origin": "http://evil.example"}),
            ("bad Host", {"host": "evil.example"}),
        ):
            try:
                with c.websocket_connect("/local-api/stream", headers=headers):
                    check(False, f"websocket with {label} rejected")
            except Exception:
                check(True, f"websocket with {label} rejected")

    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all control_server checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
