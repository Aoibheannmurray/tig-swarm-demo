#!/usr/bin/env python3
"""hpo.search config-parallelism (Option B).

Parallel evaluation (max_workers > 1) must be *deterministic-equivalent* to the
sequential path — same per-track winners, same global best, same trial scores —
because the config set is static and trials are collected by index. And it must
actually run configs concurrently when asked to (and stay serial when not).
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hpo  # noqa: E402

SEARCH_SPACE = [{"name": "lr", "type": "float", "range": [0.01, 0.1]}]
SUGGESTED = [{"lr": 0.02}, {"lr": 0.05}, {"lr": 0.09}]


def _tracks(cfg: dict) -> dict:
    lr = float(cfg.get("lr", 0.05))
    # Opposite-end rewards → per-track winners differ; aggregate rises with lr.
    return {"t_high": round(lr * 1000, 3), "t_low": round((0.1 - lr) * 500, 3)}


def _make_fake(track_concurrency: bool = False):
    st = {"active": 0, "max": 0, "lock": threading.Lock()}

    def fake(seed: str, hp_json: str):
        cfg = json.loads(hp_json)
        if track_concurrency:
            with st["lock"]:
                st["active"] += 1
                st["max"] = max(st["max"], st["active"])
            time.sleep(0.05)
            with st["lock"]:
                st["active"] -= 1
        ts = _tracks(cfg)
        return {"score": round(sum(ts.values()), 3), "feasible": True,
                "track_scores": ts}, ""

    return fake, st


def _run(max_workers: int, track: bool = False):
    fake, st = _make_fake(track)
    res = hpo.search(
        fake, SEARCH_SPACE, SUGGESTED,
        n=8, num_suggested=3, hpo_seed="ptest", direction="max",
        log=lambda *_a, **_k: None, max_workers=max_workers,
    )
    return res, st


def _by_cfg(res: dict) -> dict:
    return {json.dumps(t["config"], sort_keys=True): t["score"] for t in res["trials"]}


def test_parallel_equals_sequential():
    seq, _ = _run(1)
    par, _ = _run(4)
    assert seq["winning_configs"] == par["winning_configs"], \
        (seq["winning_configs"], par["winning_configs"])
    assert seq["winning_config"] == par["winning_config"]
    assert seq["winning_score"] == par["winning_score"]
    assert _by_cfg(seq) == _by_cfg(par)
    print("PASS test_parallel_equals_sequential")


def test_parallel_actually_concurrent():
    _, st = _run(4, track=True)
    assert st["max"] > 1, f"expected concurrent execution, max_active={st['max']}"
    print(f"PASS test_parallel_actually_concurrent (max concurrent={st['max']})")


def test_workers_1_is_serial():
    _, st = _run(1, track=True)
    assert st["max"] == 1, f"max_workers=1 must stay serial, got {st['max']}"
    print("PASS test_workers_1_is_serial")


def test_default_evaluates_all_configs_in_parallel():
    # max_workers=0 (the default) means "all configs" — concurrency is then
    # bounded solely by the caller's semaphore, not an hpo-level knob.
    seq, _ = _run(1)
    allp, st = _run(0, track=True)
    assert st["max"] > 1, f"default should fan all configs out, max_active={st['max']}"
    assert seq["winning_configs"] == allp["winning_configs"]
    assert _by_cfg(seq) == _by_cfg(allp)
    print(f"PASS test_default_evaluates_all_configs_in_parallel (max concurrent={st['max']})")


if __name__ == "__main__":
    test_parallel_equals_sequential()
    test_parallel_actually_concurrent()
    test_workers_1_is_serial()
    test_default_evaluates_all_configs_in_parallel()
    print("\nAll hpo-parallel tests passed.")
