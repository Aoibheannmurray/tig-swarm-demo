"""Endpoint tests for the hosted runner service.

Drives the FastAPI app with TestClient, stubbing the coordination-server
round-trips (auth + plan fetch) and the fleet launcher, so it exercises the
real request/validation/vault/store wiring without a network or a subprocess.

Self-running: `python runner/test_service.py`.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["RUNNER_DATA_DIR"] = tempfile.mkdtemp()
os.environ["RUNNER_ADMIN_KEY"] = "admin-secret"
os.environ["COORDINATION_SERVER_URL"] = "https://coord.example"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cryptography.fernet import Fernet  # noqa: E402
os.environ["RUNNER_SECRET_KEY"] = Fernet.generate_key().decode()

from fastapi.testclient import TestClient  # noqa: E402
from runner import service, auth, store, vault  # noqa: E402
from runner.supervisor import Supervisor, FleetHandle  # noqa: E402


class _FakeLauncher:
    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, username, config, keys):
        self.started.append((username, keys))
        h = FleetHandle(username, Path("/tmp/fake"))
        h.log.append(f"launched {username}")
        h._alive = True
        h.running = lambda: getattr(h, "_alive", False)  # type: ignore
        return h

    def stop(self, handle):
        self.stopped.append(handle.username)
        handle._alive = False  # type: ignore


# Inject a fake launcher into the module-level supervisor + stub the
# coordination-server calls.
_fake = _FakeLauncher()
service.supervisor = Supervisor(launcher=_fake)

_PLAN = {"agents": [
    {"name": "a1", "provider": "anthropic", "api_key_env": "ANTHROPIC_API_KEY",
     "compute": "c3", "role": "explorer"},
]}
_stub = {"config": _PLAN}

auth.verify_contributor = lambda u, p, **k: {"username": u} if p == "good" else _raise_auth()
auth.fetch_contributor_config = lambda u, p, **k: dict(_stub["config"])


def _raise_auth():
    raise auth.AuthError("Invalid or revoked credentials.", status=401)


CT = {"X-Username": "alice", "X-Swarm-Password": "good"}
BAD = {"X-Username": "alice", "X-Swarm-Password": "wrong"}


def test_health_reports_vault_and_caps():
    c = TestClient(service.app)
    with c:
        h = c.get("/api/runner/health").json()
    assert h["vault_configured"] is True
    assert h["coordination_server"] is True
    assert h["max_agents_per_contributor"] >= 1
    print("PASS test_health_reports_vault_and_caps")


def test_enroll_status_logs_unenroll():
    c = TestClient(service.app)
    with c:
        # Bad creds are rejected.
        assert c.post("/api/runner/enroll", json={"keys": {}}, headers=BAD).status_code == 401

        # Missing keys → 422 naming what's needed.
        r = c.post("/api/runner/enroll", json={"keys": {"ANTHROPIC_API_KEY": "sk"}}, headers=CT)
        assert r.status_code == 422 and "C3_API_KEY" in r.json()["detail"], r.json()

        # Full keys → enrolled + launched.
        keys = {"ANTHROPIC_API_KEY": "sk-ant-zzzz", "C3_API_KEY": "c3-wwww"}
        r = c.post("/api/runner/enroll", json={"keys": keys}, headers=CT)
        assert r.status_code == 200 and r.json()["status"] == "running", r.json()
        assert _fake.started and _fake.started[-1][0] == "alice"

        # Stored keys are encrypted at rest (never plaintext on disk).
        row = store.get_enrollment("alice")
        assert "sk-ant-zzzz" not in row["encrypted_keys"]
        assert vault.decrypt_map(row["encrypted_keys"]) == keys

        # Status masks the key values.
        st = c.get("/api/runner/status", headers=CT).json()
        assert st["enrolled"] and st["status"] == "running"
        assert st["keys"]["ANTHROPIC_API_KEY"] == "…zzzz"
        assert "sk-ant-zzzz" not in str(st)

        # Logs stream from the fleet handle.
        assert any("launched alice" in ln for ln in c.get("/api/runner/logs", headers=CT).json()["lines"])

        # Unenroll stops + purges.
        assert c.request("DELETE", "/api/runner/enroll", headers=CT).json() == {"enrolled": False}
        assert store.get_enrollment("alice") is None
        assert "alice" in _fake.stopped
    print("PASS test_enroll_status_logs_unenroll")


def test_admin_revoke_requires_key_and_purges():
    c = TestClient(service.app)
    with c:
        keys = {"ANTHROPIC_API_KEY": "sk", "C3_API_KEY": "c3"}
        c.post("/api/runner/enroll", json={"keys": keys}, headers=CT)

        # Wrong / missing admin key.
        assert c.post("/api/runner/admin/revoke", json={"username": "alice"}).status_code == 403
        assert c.post("/api/runner/admin/revoke", json={"username": "alice"},
                      headers={"X-Admin-Key": "nope"}).status_code == 403

        # Correct admin key tears down.
        r = c.post("/api/runner/admin/revoke", json={"username": "alice"},
                   headers={"X-Admin-Key": "admin-secret"})
        assert r.status_code == 200 and r.json()["revoked"] is True
        assert store.get_enrollment("alice") is None
    print("PASS test_admin_revoke_requires_key_and_purges")


if __name__ == "__main__":
    test_health_reports_vault_and_caps()
    test_enroll_status_logs_unenroll()
    test_admin_revoke_requires_key_and_purges()
    print("ALL PASS")
