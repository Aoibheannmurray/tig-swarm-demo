#!/usr/bin/env python3
"""Submit the currently mirrored TIG Docker challenges to C3 in parallel.

This is a focused smoke test for the C3 TIG-native path. It uses the three
challenge images that currently exist in the default Docker Hub namespace:
knapsack, vector_search, and hypergraph.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from c3_compute import run_benchmark_c3  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIG_MONOREPO = Path(
    os.environ.get("TIG_MONOREPO") or (ROOT.parent / "tig-monorepo")
)

SMOKE_CHALLENGES: dict[str, dict] = {
    "knapsack": {
        "is_gpu": False,
        "tracks": {"seed": "c3-smoke", "n_items=1000,budget=10": 1},
        "algorithm_path": "initial_algorithms/knapsack/seeds/greedy.rs",
    },
    "vector_search": {
        "is_gpu": True,
        "tracks": {"seed": "c3-smoke", "n_queries=10": 1},
        "algorithm_path": "initial_algorithms/vector_search/seeds/brute_force.rs",
        "kernel_path": "initial_algorithms/vector_search/seeds/brute_force.cu",
    },
    "hypergraph": {
        "is_gpu": True,
        "tracks": {"seed": "c3-smoke", "n_h_edges=10000": 1},
        "algorithm_path": "initial_algorithms/hypergraph/seeds/construction.rs",
        "kernel_path": "initial_algorithms/hypergraph/seeds/construction.cu",
    },
}


def _tig_monorepo_path(cli_value: str | None) -> str:
    candidates = [
        cli_value,
        os.environ.get("TIG_MONOREPO_PATH"),
        str(DEFAULT_TIG_MONOREPO) if DEFAULT_TIG_MONOREPO.exists() else None,
        str(ROOT.parent / "tig-monorepo"),
    ]
    for value in candidates:
        if value and (Path(value) / "tig-algorithms").exists():
            return value
    raise SystemExit(
        "Pinned TIG monorepo source not found. Pass --tig-monorepo-path or set "
        "TIG_MONOREPO_PATH."
    )


def _hardware_for(cfg: dict, args: argparse.Namespace) -> str:
    override = args.gpu_hardware if cfg.get("is_gpu") else args.cpu_hardware
    return override or args.hardware


def _run_one(challenge: str, base_cfg: dict, args: argparse.Namespace, index: int) -> dict:
    if args.stagger_seconds > 0:
        time.sleep(args.stagger_seconds * index)
    cfg = {
        "challenge": challenge,
        "benchmark_backend": "tig",
        "tig_monorepo_path": args.tig_monorepo_path,
        **base_cfg,
    }
    ns = argparse.Namespace(
        c3_api_key=args.c3_api_key,
        c3_provider=args.c3_provider,
        c3_time=args.c3_time,
        env=None,
        env_image=None,
        env_cpu=None,
        env_gpu=None,
        c3_cpu_image=None,
        c3_gpu_image=None,
        c3_image=None,
        hardware=_hardware_for(cfg, args),
        c3_poll_timeout=args.poll_timeout_seconds,
        c3_cancel_on_timeout=args.cancel_on_timeout,
    )
    bench, err = run_benchmark_c3(
        ns,
        cfg,
        "https://example.invalid",
        seed=str(cfg["tracks"].get("seed", "c3-smoke")),
    )
    if bench is None:
        return {"challenge": challenge, "ok": False, "error": err}
    ok = bool(bench.get("feasible")) and int(bench.get("instances_feasible") or 0) > 0
    return {"challenge": challenge, "ok": ok, "benchmark": bench, "error": "" if ok else "benchmark was not feasible"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--challenges",
        default=",".join(SMOKE_CHALLENGES),
        help="Comma-separated subset of: " + ", ".join(SMOKE_CHALLENGES),
    )
    parser.add_argument("--c3-time", default="01:00:00")
    parser.add_argument("--hardware", default="auto")
    parser.add_argument("--cpu-hardware", default=None)
    parser.add_argument("--gpu-hardware", default=None)
    parser.add_argument("--stagger-seconds", type=float, default=0.0)
    parser.add_argument("--poll-timeout-seconds", type=int, default=None)
    parser.add_argument("--cancel-on-timeout", action="store_true")
    parser.add_argument("--c3-provider", default=None)
    parser.add_argument("--c3-api-key", default=os.environ.get("C3_API_KEY", ""))
    parser.add_argument("--tig-monorepo-path", default=None)
    args = parser.parse_args()
    args.tig_monorepo_path = _tig_monorepo_path(args.tig_monorepo_path)
    requested = [c.strip() for c in args.challenges.split(",") if c.strip()]
    unknown = [c for c in requested if c not in SMOKE_CHALLENGES]
    if unknown:
        raise SystemExit(f"Unknown challenge(s): {', '.join(unknown)}")
    selected = {challenge: SMOKE_CHALLENGES[challenge] for challenge in requested}

    print("Submitting TIG C3 smoke jobs in parallel:")
    for challenge in selected:
        print(f"  - {challenge}")
    if args.stagger_seconds > 0:
        print(f"Start stagger: {args.stagger_seconds:g}s between submissions")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            pool.submit(_run_one, challenge, cfg, args, index): challenge
            for index, (challenge, cfg) in enumerate(selected.items())
        }
        for future in as_completed(futures):
            challenge = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"challenge": challenge, "ok": False, "error": repr(exc)}
            results.append(result)
            status = "OK" if result.get("ok") else "FAIL"
            print(f"[{status}] {challenge}")
            if result.get("error"):
                print(result["error"][-3000:])

    results.sort(key=lambda r: r["challenge"])
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
