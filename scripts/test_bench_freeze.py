"""Self-running tests for the benchmark-failure freeze guard — the
`no_benchmark_freeze_limit` knob that stops an agent after N consecutive
token-spending iterations without a successful benchmark, so a broken
benchmark path (C3 outage, dead Docker) can't burn LLM tokens forever.

Run directly: `python scripts/test_bench_freeze.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_loop import _freeze_limit, _NO_BENCHMARK_FREEZE_LIMIT  # noqa: E402
import run_fleet  # noqa: E402


def test_freeze_limit_parsing():
    # Absent → the baked-in default (guard is ON out of the box).
    assert _freeze_limit({}) == _NO_BENCHMARK_FREEZE_LIMIT
    # Explicit values win, including 0 (disabled).
    assert _freeze_limit({"no_benchmark_freeze_limit": 25}) == 25
    assert _freeze_limit({"no_benchmark_freeze_limit": 0}) == 0
    # String forms (env-style configs) parse; negatives clamp to disabled.
    assert _freeze_limit({"no_benchmark_freeze_limit": "7"}) == 7
    assert _freeze_limit({"no_benchmark_freeze_limit": -3}) == 0
    # Garbage falls back to the default rather than crashing the loop.
    assert _freeze_limit({"no_benchmark_freeze_limit": "lots"}) == \
        _NO_BENCHMARK_FREEZE_LIMIT
    assert _freeze_limit({"no_benchmark_freeze_limit": None}) == \
        _NO_BENCHMARK_FREEZE_LIMIT
    print("  ok: _freeze_limit parsing")


def test_fleet_wiring():
    # The knob must flow fleet.config.json → agent.config.json (per-agent)
    # and inherit from the top level as a fleet-wide default.
    assert "no_benchmark_freeze_limit" in run_fleet._AGENT_CONFIG_KEYS
    assert "no_benchmark_freeze_limit" in run_fleet._FLEET_WIDE_DEFAULT_KEYS
    print("  ok: run_fleet key wiring")


def test_server_whitelists():
    # The hosted contributor-config validator (server/server.py) must accept
    # the knob at both levels. server/ deps (FastAPI) may not be installed
    # here, so check the source text instead of importing.
    server_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, "server", "server.py")
    with open(server_py, encoding="utf-8") as f:
        src = f.read()
    assert src.count('"no_benchmark_freeze_limit"') >= 2, \
        "expected the knob in both _CONTRIB_AGENT_KEYS and _CONTRIB_TOP_KEYS"
    print("  ok: server contributor-config whitelists")


if __name__ == "__main__":
    test_freeze_limit_parsing()
    test_fleet_wiring()
    test_server_whitelists()
    print("All bench-freeze tests passed.")
