"""Self-running tests for the mid-benchmark heartbeat identity wiring.

run_benchmark's background heartbeat thread reads `agent_id`/`agent_token`
off `config` — but `config` comes from .swarm-cache.json, which carries no
identity. Before _attach_benchmark_identity stamped those keys, the guard
was never true and long benchmarks ran heartbeat-silent: the server's
inactive_minutes sweep reaped the trajectory mid-benchmark and every publish
landed on a fresh one (the vrp-swarm fable004 churn, 2026-07-17).

Run directly: `python scripts/test_bench_heartbeat_identity.py`
"""

import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_loop  # noqa: E402


def test_identity_stamped_onto_config():
    cfg = {}
    uid = run_loop._attach_benchmark_identity(cfg, "alice", "abc123", "tok-1")
    assert cfg["agent_id"] == "abc123"
    assert cfg["agent_token"] == "tok-1"
    assert cfg["tig_user_id"] == uid
    # Token omitted (legacy caller): agent_id still stamped, no bogus token.
    cfg2 = {}
    run_loop._attach_benchmark_identity(cfg2, "alice", "abc123")
    assert cfg2["agent_id"] == "abc123"
    assert "agent_token" not in cfg2
    print("  ok: _attach_benchmark_identity stamps identity")


def test_run_benchmark_starts_heartbeat():
    started = []
    real_hb = run_loop._start_heartbeat_thread
    real_local = run_loop._run_benchmark_local
    try:
        def fake_hb(server, agent_id, agent_token, **kw):
            started.append((server, agent_id, agent_token))
            return threading.Event()

        run_loop._start_heartbeat_thread = fake_hb
        run_loop._run_benchmark_local = lambda *a, **k: ({"score": 1.0}, "")
        args = SimpleNamespace(compute="local")

        # With identity on config the heartbeat thread must start.
        bench, err = run_loop.run_benchmark(
            args, {"agent_id": "abc123", "agent_token": "tok-1"}, "http://srv")
        assert bench == {"score": 1.0} and err == ""
        assert started == [("http://srv", "abc123", "tok-1")], \
            "heartbeat thread did not start for an identified benchmark"

        # Without identity it must not start (and must not crash).
        started.clear()
        bench, _ = run_loop.run_benchmark(args, {}, "http://srv")
        assert bench == {"score": 1.0}
        assert not started
    finally:
        run_loop._start_heartbeat_thread = real_hb
        run_loop._run_benchmark_local = real_local
    print("  ok: run_benchmark heartbeat gating")


if __name__ == "__main__":
    test_identity_stamped_onto_config()
    test_run_benchmark_starts_heartbeat()
    print("All bench-heartbeat-identity tests passed.")
