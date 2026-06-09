"""Tests for benchmark run-identity logging (User ID + Benchmark ID).

Runs standalone (`python test_benchmark_run_ids.py` from the scripts dir) and is
pytest-compatible.

At startup each benchmark run prints, to stderr, before any compute:
    User ID: <username> (agent <agent_id>)
    Benchmark ID: <unique-id>
so a problematic run can be reported to TIG / the compute provider. These cover
how the user id is resolved (env override, config files, graceful degradation)
and that the benchmark id is a fresh 10-char hex per run.
"""

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark
import c3_compute


def _with_root(tmp, **files):
    """Point benchmark.ROOT_DIR at a temp dir seeded with the given JSON files,
    and clear identity env. Returns nothing; caller uses benchmark._resolve_user_id()."""
    for name, data in files.items():
        import json
        (Path(tmp) / name).write_text(json.dumps(data))
    benchmark.ROOT_DIR = Path(tmp)
    for k in ("TIG_USER_ID", "TIG_USERNAME", "TIG_AGENT_ID"):
        os.environ.pop(k, None)


def test_env_override_wins():
    with tempfile.TemporaryDirectory() as tmp:
        _with_root(tmp, **{"agent.config.json": {"username": "x", "agent_id": "y"}})
        os.environ["TIG_USER_ID"] = "preset-123"
        assert benchmark._resolve_user_id() == "preset-123"
    os.environ.pop("TIG_USER_ID", None)
    print("PASS test_env_override_wins")


def test_username_and_agent_id_compose():
    with tempfile.TemporaryDirectory() as tmp:
        _with_root(tmp, **{"agent.config.json": {"username": "aoibheann", "agent_id": "69e67db9ffb3"}})
        assert benchmark._resolve_user_id() == "aoibheann (agent 69e67db9ffb3)"
    print("PASS test_username_and_agent_id_compose")


def test_username_only_falls_back_to_fleet_config():
    with tempfile.TemporaryDirectory() as tmp:
        # No agent.config.json → username comes from fleet.config.json, no agent id.
        _with_root(tmp, **{"fleet.config.json": {"username": "aoibheann"}})
        assert benchmark._resolve_user_id() == "aoibheann"
    print("PASS test_username_only_falls_back_to_fleet_config")


def test_agent_id_only():
    with tempfile.TemporaryDirectory() as tmp:
        _with_root(tmp, **{"agent.config.json": {"agent_id": "69e67db9ffb3"}})
        assert benchmark._resolve_user_id() == "agent 69e67db9ffb3"
    print("PASS test_agent_id_only")


def test_unknown_when_no_identity():
    with tempfile.TemporaryDirectory() as tmp:
        _with_root(tmp)  # empty dir, no env
        assert benchmark._resolve_user_id() == "unknown"
    print("PASS test_unknown_when_no_identity")


def test_benchmark_id_is_fresh_10_char_hex():
    # Mirrors the inline generation in main(): uuid.uuid4().hex[:10].
    ids = {uuid.uuid4().hex[:10] for _ in range(100)}
    assert len(ids) == 100, "benchmark ids must be unique per run"
    assert all(re.fullmatch(r"[0-9a-f]{10}", i) for i in ids)
    print("PASS test_benchmark_id_is_fresh_10_char_hex")


def test_c3_runner_exports_precomposed_user_id():
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        script = c3_compute._write_c3_project(
            stage,
            {
                "challenge": "knapsack",
                "c3_hardware": "l40",
                "tig_user_id": "aoibheann (agent 69e67db9ffb3)",
            },
            "https://example.invalid",
            "00:10:00",
            "rust:1-bookworm",
        )
        runner = (stage / script).read_text()
        assert 'export TIG_USER_ID="aoibheann (agent 69e67db9ffb3)"' in runner
        assert "agent.config.json" not in runner
    print("PASS test_c3_runner_exports_precomposed_user_id")


def _main():
    test_env_override_wins()
    test_username_and_agent_id_compose()
    test_username_only_falls_back_to_fleet_config()
    test_agent_id_only()
    test_unknown_when_no_identity()
    test_benchmark_id_is_fresh_10_char_hex()
    test_c3_runner_exports_precomposed_user_id()
    print("\nAll benchmark run-id tests passed.")


if __name__ == "__main__":
    _main()
