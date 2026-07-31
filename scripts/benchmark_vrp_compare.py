#!/usr/bin/env python3
"""Benchmark the live VRP swarm winner against two TIG mainnet algorithms.

The parent process freezes all three source bundles, then starts one isolated
C3 benchmark per candidate.  Each benchmark uses the swarm's wall-clock
harness, identical test-seeded instances, and one 96-vCPU machine.  Keeping
the candidates in separate processes avoids c3_compute's module-level ROOT
from leaking between their staged workspaces while allowing all three C3 jobs
to run concurrently.

Default comparison:
  * current credentialed global best from vrp-swarm-production
  * vehicle_routing/hgs_advance (current TIG mainnet winner)
  * vehicle_routing/prometheus_hgs_adv (Prometheus submission)
  * 200 instances on each n_nodes=600..1000 track
  * 220-second per-instance solver timeout
  * one high-availability L40 host per candidate, with 12 CPU solver processes
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

VRP_SERVER = "https://vrp-swarm-production.up.railway.app"
VRP_ADMIN = ROOT / "swarm.admin copy.json"
TRACKS = [f"n_nodes={n}" for n in (600, 700, 800, 900, 1000)]
SOURCE_SUFFIXES = (".rs", ".cu", ".cuh")
INFEASIBLE_QUALITY = -10_000_000.0


def _json_get(url: str, headers: dict[str, str] | None = None) -> dict:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "tig-vrp-three-way-benchmark",
        **(headers or {}),
    }
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def _source_files(files: dict[str, str]) -> dict[str, str]:
    return {
        path: body for path, body in files.items() if path.endswith(SOURCE_SUFFIXES)
    }


def _source_hash(files: dict[str, str]) -> str:
    canonical = "".join(f"{path}\0{files[path]}\0" for path in sorted(files))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _write_bundle(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    for relative, body in files.items():
        output = directory / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8", newline="\n")


def _fetch_swarm_winner() -> tuple[dict[str, str], dict]:
    admin = json.loads(VRP_ADMIN.read_text(encoding="utf-8-sig"))
    username = admin["owner_name"]
    password = hashlib.sha256(
        f"{username}:{admin['swarm_password']}".encode()
    ).hexdigest()
    state = _json_get(
        f"{VRP_SERVER}/api/state?challenge=vehicle_routing",
        {
            "X-Username": username,
            "X-Swarm-Password": password,
        },
    )
    files = state.get("best_algorithm_files") or {
        "mod.rs": state.get("best_algorithm_code") or ""
    }
    files = _source_files(files)
    if not files.get("mod.rs"):
        raise RuntimeError("VRP swarm returned no credentialed winner source")
    leader = (state.get("leaderboard") or [{}])[0]
    return files, {
        "kind": "swarm_global_best",
        "server_url": VRP_SERVER,
        "experiment_id": state.get("best_experiment_id"),
        "published_score": state.get("best_score"),
        "published_track_scores": state.get("best_track_scores"),
        "agent_id": leader.get("agent_id"),
        "agent_name": leader.get("agent_name"),
        "llm_type": leader.get("llm_type"),
    }


def _mainnet_snapshot() -> dict:
    base = "https://mainnet-api.tig.foundation"
    block = _json_get(f"{base}/get-block")["block"]
    block_id = block["id"]
    challenges = _json_get(f"{base}/get-challenges?block_id={block_id}")
    algorithms = _json_get(f"{base}/get-algorithms?block_id={block_id}")
    names = {
        item["id"]: (item.get("config") or {}).get("name")
        for item in challenges.get("challenges", [])
    }
    compiled = {
        item.get("algorithm_id"): bool(
            (item.get("details") or {}).get("compile_success")
        )
        for item in algorithms.get("binarys", [])
    }
    rows = []
    for item in algorithms.get("codes", []):
        details = item.get("details") or {}
        if names.get(details.get("challenge_id")) != "vehicle_routing":
            continue
        rows.append(
            {
                "id": item.get("id"),
                "name": details.get("name"),
                "player_id": details.get("player_id"),
                "parent_algorithm_id": details.get("algorithm_id"),
                "compiled": compiled.get(item.get("id"), False),
                "adoption": int((item.get("block_data") or {}).get("adoption") or 0),
                "state": item.get("state"),
            }
        )
    return {
        "block_id": block_id,
        "block": block,
        "vehicle_routing_algorithms": rows,
    }


def _github_commit(branch: str) -> str | None:
    encoded = urllib.parse.quote(branch, safe="")
    try:
        data = _json_get(
            "https://api.github.com/repos/tig-foundation/"
            f"tig-monorepo/commits/{encoded}"
        )
        return data.get("sha")
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError):
        return None


def _fetch_mainnet_bundle(name: str) -> tuple[dict[str, str], dict]:
    branch = f"vehicle_routing/{name}"
    branch_path = urllib.parse.quote(branch, safe="/")
    url = (
        "https://codeload.github.com/tig-foundation/tig-monorepo/"
        f"tar.gz/refs/heads/{branch_path}"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "tig-vrp-three-way-benchmark"}
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        archive = response.read()
    prefix = f"tig-algorithms/src/vehicle_routing/{name}/"
    all_files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            marker = member.name.find(prefix)
            if not member.isfile() or marker < 0:
                continue
            relative = member.name[marker + len(prefix) :]
            extracted = tar.extractfile(member)
            if relative and extracted is not None:
                all_files[relative] = extracted.read().decode("utf-8", errors="replace")
    files = _source_files(all_files)
    if not files.get("mod.rs"):
        raise RuntimeError(f"official branch for {name} returned no mod.rs")
    return files, {
        "kind": "tig_algorithm",
        "algorithm_name": name,
        "github_branch": branch,
        "github_commit": _github_commit(branch),
    }


def _copy_candidate_root(source_dir: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(ROOT / ".cargo", destination / ".cargo")
    shutil.copytree(ROOT / "scripts", destination / "scripts")
    shutil.copytree(
        ROOT / "src",
        destination / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    algorithm_dir = destination / "src" / "vehicle_routing" / "algorithm"
    if algorithm_dir.exists():
        shutil.rmtree(algorithm_dir)
    shutil.copytree(source_dir, algorithm_dir)


def _worker(args: argparse.Namespace) -> int:
    import c3_compute
    import secrets_local

    candidate_dir = Path(args.candidate_dir).resolve()
    result_path = Path(args.result).resolve()
    with tempfile.TemporaryDirectory(prefix=f"vrp-{args.label}-") as temp:
        candidate_root = Path(temp) / "root"
        _copy_candidate_root(candidate_dir, candidate_root)
        c3_compute.ROOT = candidate_root

        config = {
            "active_challenge": "vehicle_routing",
            "challenge": "vehicle_routing",
            "tracks": {"seed": args.seed, **{track: args.count for track in TRACKS}},
            "timeout": args.timeout,
            "scoring_direction": "max",
            "is_gpu": False,
            "algorithm_path": "src/vehicle_routing/algorithm/mod.rs",
            "bench_workers": args.workers,
            "c3_hardware": args.hardware,
            "c3_max_parallel_jobs": 1,
            "c3_warm_images": True,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        run_args = SimpleNamespace(
            c3_api_key=secrets_local.resolve("C3_API_KEY"),
            hardware=args.hardware,
            c3_time=args.walltime,
            c3_provider=args.provider,
            c3_max_parallel_jobs=1,
            c3_poll_timeout=None,
        )
        print(
            f"[{args.label}] starting: {args.count} per track, timeout={args.timeout}s, "
            f"hardware={args.hardware}, workers={args.workers}",
            flush=True,
        )
        benchmark, error = c3_compute.run_benchmark_c3(run_args, config, VRP_SERVER)
        if benchmark is None:
            print(f"[{args.label}] FAILED: {error}", file=sys.stderr, flush=True)
            return 1
        benchmark["candidate"] = args.label
        benchmark["benchmark_config"] = config
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(benchmark, indent=2), encoding="utf-8")
        print(
            f"[{args.label}] completed: score={benchmark.get('score')} "
            f"feasible={benchmark.get('instances_feasible')}/"
            f"{benchmark.get('instances_solved')}",
            flush=True,
        )
    return 0


def _instance_map(benchmark: dict) -> dict[tuple[str, str], float]:
    values = {}
    for row in benchmark.get("instance_results") or []:
        key = (str(row.get("track")), str(row.get("instance")))
        score = row.get("score")
        values[key] = (
            float(score)
            if row.get("feasible") and isinstance(score, (int, float))
            else INFEASIBLE_QUALITY
        )
    return values


def _pairwise(left_name: str, left: dict, right_name: str, right: dict) -> dict:
    left_values = _instance_map(left)
    right_values = _instance_map(right)
    common = sorted(left_values.keys() & right_values.keys())
    by_track: dict[str, list[float]] = defaultdict(list)
    wins = Counter()
    differences = []
    for key in common:
        delta = left_values[key] - right_values[key]
        differences.append(delta)
        by_track[key[0]].append(delta)
        if delta > 0:
            wins["left"] += 1
        elif delta < 0:
            wins["right"] += 1
        else:
            wins["tie"] += 1
    return {
        "left": left_name,
        "right": right_name,
        "common_instances": len(common),
        "left_wins": wins["left"],
        "right_wins": wins["right"],
        "ties": wins["tie"],
        "mean_score_delta_left_minus_right": (
            sum(differences) / len(differences) if differences else None
        ),
        "mean_score_delta_by_track": {
            track: sum(values) / len(values)
            for track, values in sorted(by_track.items())
        },
    }


def _markdown_report(comparison: dict) -> str:
    lines = [
        "# VRP three-way benchmark",
        "",
        f"Generated: {comparison['completed_at']}",
        "",
        (
            f"Seed: `{comparison['config']['seed']}`; "
            f"{comparison['config']['instances_per_track']} instances per track; "
            f"maximum timeout per instance: "
            f"{comparison['config']['timeout_seconds']}s; "
            f"hardware: `{comparison['config']['hardware']}`; "
            f"workers: {comparison['config']['workers_per_machine']}."
        ),
        "",
        (
            "Higher scores are better. The timeout is an external guard, not a "
            "requirement that an algorithm consume the full budget."
        ),
        "",
        "| candidate | source sha256 | aggregate score | feasible |",
        "|---|---:|---:|---:|",
    ]
    for name, item in comparison["candidates"].items():
        result = item["result"]
        lines.append(
            f"| {name} | `{item['source_sha256'][:16]}` | "
            f"{result.get('score')} | {result.get('instances_feasible')}/"
            f"{result.get('instances_solved')} |"
        )
    lines.extend(["", "## Track scores", ""])
    lines.append("| track | " + " | ".join(comparison["candidates"]) + " |")
    lines.append("|---|" + "---:|" * len(comparison["candidates"]))
    for track in TRACKS:
        scores = [
            comparison["candidates"][name]["result"].get("track_scores", {}).get(track)
            for name in comparison["candidates"]
        ]
        lines.append(f"| {track} | " + " | ".join(map(str, scores)) + " |")
    lines.extend(["", "## Paired comparisons", ""])
    for pair in comparison["pairwise"]:
        lines.append(
            f"- {pair['left']} vs {pair['right']}: "
            f"{pair['left_wins']}–{pair['right_wins']} "
            f"({pair['ties']} ties), mean quality delta "
            f"{pair['mean_score_delta_left_minus_right']}."
        )
    return "\n".join(lines) + "\n"


def _parent(args: argparse.Namespace) -> int:
    started = datetime.now(timezone.utc)
    output_dir = (
        Path(args.out).resolve()
        if args.out
        else (ROOT / "reports" / f"vrp_compare_{started.strftime('%Y%m%dT%H%M%SZ')}")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    candidate_root = output_dir / "candidates"
    results_root = output_dir / "results"

    mainnet = _mainnet_snapshot()
    indexed = {row["name"]: row for row in mainnet["vehicle_routing_algorithms"]}
    winner_rows = [
        row
        for row in mainnet["vehicle_routing_algorithms"]
        if row["compiled"] and row["adoption"] > 0
    ]
    winner_rows.sort(key=lambda row: row["adoption"], reverse=True)
    if not winner_rows or winner_rows[0]["name"] != "hgs_advance":
        raise RuntimeError(
            "current TIG VRP winner is no longer hgs_advance; refusing to run "
            f"a mislabeled comparison (observed {winner_rows[:1]})"
        )

    bundles: dict[str, tuple[dict[str, str], dict]] = {
        "vrp_swarm_best": _fetch_swarm_winner(),
        "tig_hgs_advance": _fetch_mainnet_bundle("hgs_advance"),
        "prometheus_hgs_adv": _fetch_mainnet_bundle("prometheus_hgs_adv"),
    }
    bundles["tig_hgs_advance"][1]["mainnet"] = indexed.get("hgs_advance")
    bundles["prometheus_hgs_adv"][1]["mainnet"] = indexed.get("prometheus_hgs_adv")

    manifest = {
        "started_at": started.isoformat(),
        "config": {
            "seed": args.seed,
            "tracks": TRACKS,
            "instances_per_track": args.count,
            "timeout_seconds": args.timeout,
            "hardware": args.hardware,
            "workers_per_machine": args.workers,
            "c3_walltime": args.walltime,
            "c3_provider": args.provider,
        },
        "mainnet": mainnet,
        "candidates": {},
    }
    for label, (files, provenance) in bundles.items():
        source_dir = candidate_root / label
        _write_bundle(source_dir, files)
        manifest["candidates"][label] = {
            "source_sha256": _source_hash(files),
            "files": {path: len(body) for path, body in sorted(files.items())},
            "provenance": provenance,
        }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"Frozen candidates in {output_dir}", flush=True)

    processes = []
    for label in bundles:
        result_path = results_root / f"{label}.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--label",
            label,
            "--candidate-dir",
            str(candidate_root / label),
            "--result",
            str(result_path),
            "--count",
            str(args.count),
            "--timeout",
            str(args.timeout),
            "--seed",
            args.seed,
            "--hardware",
            args.hardware,
            "--workers",
            str(args.workers),
            "--walltime",
            args.walltime,
            "--provider",
            args.provider,
        ]
        processes.append((label, subprocess.Popen(command, cwd=ROOT)))

    failures = []
    for label, process in processes:
        returncode = process.wait()
        if returncode:
            failures.append((label, returncode))
    if failures:
        print(f"Benchmark worker failures: {failures}", file=sys.stderr)
        return 1

    benchmarks = {
        label: json.loads((results_root / f"{label}.json").read_text())
        for label in bundles
    }
    expected = len(TRACKS) * args.count
    completeness = {}
    for label, benchmark in benchmarks.items():
        rows = benchmark.get("instance_results") or []
        per_track = Counter(str(row.get("track")) for row in rows)
        completeness[label] = {
            "expected_instances": expected,
            "reported_instances": len(rows),
            "per_track": dict(per_track),
            "complete": len(rows) == expected
            and all(per_track[track] == args.count for track in TRACKS),
        }
    if not all(item["complete"] for item in completeness.values()):
        (output_dir / "completeness.json").write_text(
            json.dumps(completeness, indent=2), encoding="utf-8"
        )
        raise RuntimeError(f"incomplete C3 results: {completeness}")

    labels = list(bundles)
    comparison = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "config": manifest["config"],
        "mainnet_block_id": mainnet["block_id"],
        "completeness": completeness,
        "candidates": {
            label: {
                **manifest["candidates"][label],
                "result": {
                    key: benchmarks[label].get(key)
                    for key in (
                        "score",
                        "feasible",
                        "instances_solved",
                        "instances_feasible",
                        "instances_infeasible",
                        "track_scores",
                        "errors",
                    )
                },
            }
            for label in labels
        },
        "pairwise": [
            _pairwise(
                labels[i], benchmarks[labels[i]], labels[j], benchmarks[labels[j]]
            )
            for i in range(len(labels))
            for j in range(i + 1, len(labels))
        ],
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2), encoding="utf-8"
    )
    (output_dir / "REPORT.md").write_text(
        _markdown_report(comparison), encoding="utf-8"
    )
    print(_markdown_report(comparison), flush=True)
    print(f"Full artifacts: {output_dir}", flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=220)
    parser.add_argument("--seed", default="test")
    parser.add_argument("--hardware", default="l40")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--walltime", default="06:00:00")
    parser.add_argument("--provider", default="")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--label", help=argparse.SUPPRESS)
    parser.add_argument("--candidate-dir", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return _worker(args) if args.worker else _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
