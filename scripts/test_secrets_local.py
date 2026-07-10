"""Tests for the local secret store (server-first onboarding P2).

secrets_local.py backs `python run.py --join` — API keys stored in a
gitignored 0600 file so contributors never `export`. Env vars always win.

Self-running: `python scripts/test_secrets_local.py`. Points the module's
SECRETS_PATH at a temp file so it never touches a real secrets.local.json.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_local


def _isolate():
    """Redirect the store to a fresh temp file; clear related env vars."""
    tmp = Path(tempfile.mkdtemp()) / "secrets.local.json"
    secrets_local.SECRETS_PATH = tmp
    for var in ("ANTHROPIC_API_KEY", "C3_API_KEY", "TEST_API_KEY"):
        os.environ.pop(var, None)
    return tmp


def test_store_and_resolve():
    path = _isolate()
    assert secrets_local.resolve("ANTHROPIC_API_KEY") is None
    secrets_local.store("ANTHROPIC_API_KEY", "sk-ant-123")
    assert secrets_local.resolve("ANTHROPIC_API_KEY") == "sk-ant-123"
    assert path.exists()
    # Reloads from disk (not just in-memory).
    assert secrets_local.load_secrets()["ANTHROPIC_API_KEY"] == "sk-ant-123"
    print("PASS test_store_and_resolve")


def test_env_wins_over_file():
    _isolate()
    secrets_local.store("ANTHROPIC_API_KEY", "from-file")
    os.environ["ANTHROPIC_API_KEY"] = "from-env"
    assert secrets_local.resolve("ANTHROPIC_API_KEY") == "from-env"
    os.environ.pop("ANTHROPIC_API_KEY")
    assert secrets_local.resolve("ANTHROPIC_API_KEY") == "from-file"
    print("PASS test_env_wins_over_file")


def test_blank_clears_entry():
    _isolate()
    secrets_local.store("TEST_API_KEY", "x")
    secrets_local.store("TEST_API_KEY", "")
    assert secrets_local.resolve("TEST_API_KEY") is None
    assert "TEST_API_KEY" not in secrets_local.load_secrets()
    print("PASS test_blank_clears_entry")


def test_file_permissions_are_owner_only():
    path = _isolate()
    secrets_local.store("TEST_API_KEY", "x")
    if os.name != "nt":  # POSIX perms only
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)
    print("PASS test_file_permissions_are_owner_only")


def test_status_reports_source_without_values():
    _isolate()
    secrets_local.store("ANTHROPIC_API_KEY", "secret-value")
    os.environ["C3_API_KEY"] = "env-value"
    st = secrets_local.status()
    assert st["ANTHROPIC_API_KEY"] == {"set": True, "source": "file"}, st
    assert st["C3_API_KEY"] == {"set": True, "source": "env"}, st
    # Never leaks the value.
    assert "secret-value" not in repr(st) and "env-value" not in repr(st)
    os.environ.pop("C3_API_KEY")
    print("PASS test_status_reports_source_without_values")


def test_corrupt_file_degrades_gracefully():
    path = _isolate()
    path.write_text("{not json", encoding="utf-8")
    assert secrets_local.load_secrets() == {}
    assert secrets_local.resolve("ANYTHING") is None
    print("PASS test_corrupt_file_degrades_gracefully")


if __name__ == "__main__":
    test_store_and_resolve()
    test_env_wins_over_file()
    test_blank_clears_entry()
    test_file_permissions_are_owner_only()
    test_status_reports_source_without_values()
    test_corrupt_file_degrades_gracefully()
    print("ALL PASS")
