"""Tests for the hosted runner.

Hermetic: RUNNER_SECRET_KEY + RUNNER_DATA_DIR point at temp values before the
runner modules load, and the supervisor is driven with a fake launcher, so no
test clones a repo or spawns a fleet.

Self-running: `python runner/test_runner.py`.
"""

import os
import sys
import tempfile
from pathlib import Path

# Isolate storage + provide a vault key BEFORE importing runner modules.
os.environ["RUNNER_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cryptography.fernet import Fernet  # noqa: E402
os.environ["RUNNER_SECRET_KEY"] = Fernet.generate_key().decode()

from runner import store, validation, vault  # noqa: E402
from runner.supervisor import Supervisor, FleetHandle  # noqa: E402


# ── vault ──

def test_vault_round_trip_and_tamper():
    token = vault.encrypt_map({"ANTHROPIC_API_KEY": "sk-ant-1", "C3_API_KEY": "c3-1"})
    assert "sk-ant-1" not in token  # ciphertext, not plaintext
    assert vault.decrypt_map(token) == {"ANTHROPIC_API_KEY": "sk-ant-1", "C3_API_KEY": "c3-1"}
    # A tampered / foreign token degrades to {} rather than raising.
    assert vault.decrypt_map(token[:-4] + "AAAA") == {}
    assert vault.decrypt_map("not-a-token") == {}
    print("PASS test_vault_round_trip_and_tamper")


def test_vault_unavailable_without_key():
    saved = os.environ.pop("RUNNER_SECRET_KEY")
    try:
        assert vault.available() is False
        raised = False
        try:
            vault.encrypt("x")
        except vault.VaultUnavailable:
            raised = True
        assert raised
    finally:
        os.environ["RUNNER_SECRET_KEY"] = saved
    assert vault.available() is True
    print("PASS test_vault_unavailable_without_key")


# ── validation ──

def _c3_plan(n=1, provider="anthropic", compute="c3"):
    return {"agents": [
        {"name": f"a{i}", "provider": provider, "api_key_env": "ANTHROPIC_API_KEY",
         "compute": compute} for i in range(n)
    ]}


def test_validation_accepts_c3_api_plan():
    agents = validation.validate_plan(_c3_plan(2))
    assert len(agents) == 2
    assert validation.required_env_vars(agents) == {"ANTHROPIC_API_KEY", "C3_API_KEY"}
    print("PASS test_validation_accepts_c3_api_plan")


def test_validation_rejects_local_and_agentic_and_caps():
    def rejects(plan, **kw):
        try:
            validation.validate_plan(plan, **kw)
        except validation.EnrollmentError:
            return True
        return False

    assert rejects(_c3_plan(compute="local"))
    assert rejects(_c3_plan(provider="claude-code"))
    assert rejects({"agents": []})
    os.environ["RUNNER_MAX_AGENTS_PER_CONTRIBUTOR"] = "3"
    assert rejects(_c3_plan(4))
    del os.environ["RUNNER_MAX_AGENTS_PER_CONTRIBUTOR"]
    # Global ceiling: this plan plus others exceeds the cap.
    os.environ["RUNNER_MAX_TOTAL_AGENTS"] = "5"
    assert rejects(_c3_plan(3), existing_total_agents=4)
    del os.environ["RUNNER_MAX_TOTAL_AGENTS"]
    print("PASS test_validation_rejects_local_and_agentic_and_caps")


def test_validation_rejects_contributor_local_llm_endpoint():
    """A custom/local LLM is provider `openai` + an api_base on the
    contributor's own box — which the runner, a different machine, can't
    reach. Caught at enroll time instead of as connection-refused every
    iteration."""
    def with_base(base):
        plan = _c3_plan(1)
        plan["agents"][0]["api_base"] = base
        try:
            validation.validate_plan(plan)
        except validation.EnrollmentError as exc:
            return str(exc)
        return None

    for base in ("http://127.0.0.1:8000/v1", "http://localhost:11434/v1",
                 "http://192.168.1.9:8000/v1", "http://my-box.local:1234/v1"):
        assert with_base(base), f"{base} should be rejected"
        assert base in with_base(base), "the message must name the endpoint"
    # A public OpenAI-compatible gateway is fine — that's OpenRouter/DeepSeek,
    # which the runner has always accepted.
    assert with_base("https://openrouter.ai/api/v1") is None
    assert with_base("https://api.deepseek.com/v1") is None
    print("PASS test_validation_rejects_contributor_local_llm_endpoint")


# ── store ──

def test_store_upsert_preserves_status_and_created():
    store.init_db()
    store.upsert_enrollment("alice", "enc1", _c3_plan(1), "2026-07-08T00:00:00Z")
    store.set_status("alice", "running", "2026-07-08T00:01:00Z")
    # Update keys/config later — status + created_at must survive.
    store.upsert_enrollment("alice", "enc2", _c3_plan(2), "2026-07-08T00:02:00Z")
    row = store.get_enrollment("alice")
    assert row["encrypted_keys"] == "enc2"
    assert row["status"] == "running"
    assert row["created_at"] == "2026-07-08T00:00:00Z"
    assert len(store.config_of(row)["agents"]) == 2
    store.delete_enrollment("alice")
    assert store.get_enrollment("alice") is None
    print("PASS test_store_upsert_preserves_status_and_created")


# ── supervisor (fake launcher) ──

class _FakeLauncher:
    def __init__(self):
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start(self, username, config, keys):
        self.started.append(username)
        h = FleetHandle(username, Path("/tmp/fake"))
        h.log.append(f"started {username} with {len(config['agents'])} agents")
        h._alive = True  # our fake "running" flag
        # Patch running() for the fake (no real process).
        h.running = lambda: getattr(h, "_alive", False)  # type: ignore
        return h

    def stop(self, handle):
        self.stopped.append(handle.username)
        handle._alive = False  # type: ignore


def test_supervisor_lifecycle_and_isolation():
    fake = _FakeLauncher()
    sup = Supervisor(launcher=fake)

    sup.start("alice", _c3_plan(2), {"C3_API_KEY": "x"})
    sup.start("bob", _c3_plan(1), {"C3_API_KEY": "y"})
    assert sup.is_running("alice") and sup.is_running("bob")
    assert set(sup.running_usernames()) == {"alice", "bob"}
    assert any("2 agents" in ln for ln in sup.logs("alice"))

    # Restart alice → old handle stopped, new one started (config/key refresh).
    sup.start("alice", _c3_plan(3), {"C3_API_KEY": "x2"})
    assert fake.stopped.count("alice") == 1
    assert fake.started.count("alice") == 2
    assert sup.is_running("bob")  # bob untouched

    assert sup.stop("bob") is True
    assert sup.is_running("bob") is False
    assert sup.stop("nobody") is False

    sup.stop_all()
    assert sup.running_usernames() == []
    print("PASS test_supervisor_lifecycle_and_isolation")


if __name__ == "__main__":
    test_vault_round_trip_and_tamper()
    test_vault_unavailable_without_key()
    test_validation_accepts_c3_api_plan()
    test_validation_rejects_local_and_agentic_and_caps()
    test_validation_rejects_contributor_local_llm_endpoint()
    test_store_upsert_preserves_status_and_created()
    test_supervisor_lifecycle_and_isolation()
    print("ALL PASS")
