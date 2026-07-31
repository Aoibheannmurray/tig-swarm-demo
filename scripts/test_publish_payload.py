"""Tests for publish_results payload coercion (swarm_client).

The server's IterationCreate schema requires `score: float` and caps `title`
at MAX_LABEL_LEN (300). A stray agent-supplied value must not 422 the whole
publish. Runs standalone (`python test_publish_payload.py` from scripts/).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swarm_client


def _capture_payload(monkeypatch_target, bench, mutation):
    """Call publish_results with server I/O stubbed; return the built payload."""
    captured = {}

    def fake_post(url, payload, timeout=10, **kw):
        captured.update(payload)
        return {"ok": True}

    orig = {name: getattr(swarm_client, name) for name in
            ("server_post", "read_algorithm", "read_optional", "kernel_path", "read_files")}
    swarm_client.server_post = fake_post
    swarm_client.read_algorithm = lambda cfg: "// code"
    swarm_client.read_optional = lambda p: None
    swarm_client.kernel_path = lambda cfg: None
    swarm_client.read_files = lambda cfg: {"mod.rs": "// code"}
    try:
        swarm_client.publish_results(
            "https://x.invalid", "agent1", bench, mutation, {"challenge": "knapsack"})
    finally:
        for name, fn in orig.items():
            setattr(swarm_client, name, fn)
    return captured


def test_title_clamped_to_300():
    long_title = "x" * 500
    p = _capture_payload(None, {"score": 1.0, "challenge": "knapsack"},
                         {"title": long_title})
    assert len(p["title"]) == 300, len(p["title"])
    print("PASS test_title_clamped_to_300")


def test_score_coerced_to_float():
    # None score (failed benchmark) -> 0.0, not a schema-violating null.
    p = _capture_payload(None, {"score": None, "challenge": "knapsack"}, {"title": "t"})
    assert isinstance(p["score"], float) and p["score"] == 0.0, p["score"]
    # Missing score key -> 0.0.
    p = _capture_payload(None, {"challenge": "knapsack"}, {"title": "t"})
    assert isinstance(p["score"], float) and p["score"] == 0.0
    # A legit negative float passes through unchanged (not treated as falsy-zero).
    p = _capture_payload(None, {"score": -121021.0, "challenge": "knapsack"}, {"title": "t"})
    assert p["score"] == -121021.0, p["score"]
    # An int-typed score becomes a float.
    p = _capture_payload(None, {"score": 5, "challenge": "knapsack"}, {"title": "t"})
    assert isinstance(p["score"], float) and p["score"] == 5.0
    print("PASS test_score_coerced_to_float")


def test_none_title_and_desc_become_empty_strings():
    p = _capture_payload(None, {"score": 1.0, "challenge": "knapsack"},
                         {"title": None, "description": None, "notes": None})
    assert p["title"] == "" and p["description"] == "" and p["notes"] == ""
    assert p["feasible"] is False  # coerced bool default
    print("PASS test_none_title_and_desc_become_empty_strings")


def _main():
    test_title_clamped_to_300()
    test_score_coerced_to_float()
    test_none_title_and_desc_become_empty_strings()
    print("\nAll publish-payload tests passed.")


if __name__ == "__main__":
    _main()
