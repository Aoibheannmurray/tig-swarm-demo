"""Swarm lifecycle flows: `create` (Railway provisioning + config push +
pool seeding), `switch` (active-challenge broadcast), and `sync` (refresh
.swarm-cache.json from the live server). Moved from the root setup.py.

Note for patching/tests: the side-effect helpers create_swarm calls
(`_railway_provision`, `template_files`, `write_swarm_cache`, …) are
resolved from THIS module's globals at call time. setup.py's compat layer
forwards `setattr(setup, name, stub)` into this namespace, which is how
scripts/test_fleet_core.py stubs the Railway/network side effects."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import secrets
import shutil
import subprocess as sp
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .challenges_bridge import (
    _load_challenge_registry,
    get_challenges,
    get_cpu_challenges,
    get_gpu_challenges,
)
from .config_io import (
    ROOT,
    read_swarm_admin,
    read_swarm_cache,
    resolve_server_url,
    template_files,
    write_swarm_admin,
    write_swarm_cache,
)
from .http import _MAINNET_API, _mainnet_get, post_json
from .prompting import prompt, prompt_choice, prompt_int
from .railway import (
    _pick_workspace,
    _railway_add_volume,
    _railway_check_auth,
    _railway_check_installed,
    _railway_domain,
    _railway_provision,
    _railway_set_variables,
    _railway_up,
    _wait_for_server,
)

DEFAULT_INSTANCES_PER_TRACK = 2
DEFAULT_TRACKS_PER_CHALLENGE = {
    "satisfiability": {"n_vars=100000,ratio=4150": 2},
    "vehicle_routing": {"n_nodes=600": 2},
    "knapsack": {"n_items=1000,budget=10": 2},
    "job_scheduling": {"n=50,s=flow_shop": 2},
    "energy_arbitrage": {"s=baseline": 2},
    "hypergraph": {"n_h_edges=10000": 2},
    "neuralnet_optimizer": {"n_hidden=4": 2},
    "vector_search": {"n_queries=7000": 2},
}


def _arg_value(args: argparse.Namespace | None, name: str):
    return getattr(args, name, None) if args is not None else None


def _arg_enabled(args: argparse.Namespace | None, name: str) -> bool:
    return bool(getattr(args, name, False)) if args is not None else False


def _top_mainnet_algorithm(challenge: str) -> tuple[str, int] | None:
    """Return `(algorithm_name, adoption_fp)` for the highest-adoption
    successfully-compiled mainnet algorithm on `challenge`, or None if
    none qualifies / the API is unreachable.

    `adoption_fp` is the raw 1e16-scaled fixed-point integer the API
    returns; divide by 1e16 for a percentage."""
    try:
        block = _mainnet_get(f"{_MAINNET_API}/get-block")["block"]
        block_id = block["id"]
        challenges_resp = _mainnet_get(
            f"{_MAINNET_API}/get-challenges?block_id={block_id}"
        )
        algos_resp = _mainnet_get(
            f"{_MAINNET_API}/get-algorithms?block_id={block_id}"
        )
    except Exception as e:
        print(f"  mainnet unreachable ({e})")
        return None

    # challenge_id -> challenge_name. The upstream response carries the
    # human-readable name under `config.name`.
    id_to_name: dict[str, str] = {
        c["id"]: c["config"]["name"] for c in challenges_resp["challenges"]
    }
    target_cid = next((cid for cid, name in id_to_name.items() if name == challenge), None)
    if target_cid is None:
        return None

    # Only consider algorithms that compiled successfully upstream.
    compile_ok: dict[str, bool] = {
        b["algorithm_id"]: bool(b.get("details", {}).get("compile_success"))
        for b in algos_resp.get("binarys", [])
    }

    best: tuple[str, int] | None = None
    for algo in algos_resp["codes"]:
        if (algo.get("details") or {}).get("challenge_id") != target_cid:
            continue
        if not compile_ok.get(algo["id"]):
            continue
        try:
            adoption = int((algo.get("block_data") or {}).get("adoption") or 0)
        except (TypeError, ValueError):
            adoption = 0
        if adoption <= 0:
            continue
        name = (algo.get("details") or {}).get("name")
        if not name:
            continue
        if best is None or adoption > best[1]:
            best = (name, adoption)
    return best


def seed_inactive_pool_from_mainnet(
    server_url: str, admin_key: str, challenges: set[str],
) -> None:
    """For each requested challenge, find the current top-adoption mainnet
    algorithm, fetch its source in-memory via
    ``download_algorithm.fetch_algorithm`` (deliberately NOT
    ``download_algorithm`` — we never want to mutate the host's
    ``initial_algorithms/`` directory as a side effect of seeding the
    server's inactive pool), and POST it to ``/api/admin/seed_inactive``
    so the swarm's first stagnation-with-adoption event picks it up.

    Multi-file aware: the full {relpath: content} map (multiple ``.rs``
    modules and multiple ``.cu`` kernels, names preserved) is sent as
    ``algorithm_files``, with the entry ``mod.rs`` mirrored into
    ``algorithm_code`` for single-file consumers. Non-code companions
    (e.g. README.md) are dropped. A bundle with no ``mod.rs`` entry is
    skipped.

    Best-effort throughout: network failures, unknown algorithms, and
    server errors are warned-and-skipped rather than aborting setup."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from download_algorithm import fetch_algorithm, DownloadError
        from challenge_files import reshape_mainnet_for_swarm
    except Exception as e:
        print(f"  could not import seeding helpers: {e}; skipping seed.")
        return

    targets = sorted(challenges)
    if not targets:
        return

    for ch in targets:
        top = _top_mainnet_algorithm(ch)
        if top is None:
            print(f"  {ch}: no compiled mainnet algorithm found; skipping seed.")
            continue
        algo_name, adoption = top
        print(
            f"  {ch}: top algorithm '{algo_name}' "
            f"(adoption {adoption / 1e16:.2f}%); fetching…"
        )
        try:
            files = fetch_algorithm(ch, algo_name)
        except DownloadError as e:
            print(f"  {ch}: fetch of {algo_name} failed ({e}); skipping seed.")
            continue

        # Keep only compilable source; drop README.md and other companions
        # (mirrors challenge_files._ALGO_FILE_SUFFIXES). The full map — multiple
        # `.rs` modules and multiple `.cu` kernels, names preserved — rides in
        # `algorithm_files`; `algorithm_code` carries the entry file.
        code_files = {
            p: c for p, c in files.items()
            if p.endswith((".rs", ".cu", ".cuh"))
        }
        if "mod.rs" not in code_files:
            print(
                f"  {ch}: upstream {algo_name} has no mod.rs entry "
                f"(files={sorted(files)}); skipping seed."
            )
            continue

        # Reshape the bundle into the swarm's expected layout (e.g. strip the
        # harness-owned solve_challenge/training_loop on optimizer-hook
        # challenges) and validate it. On failure, print an ERROR and skip so
        # the host notices rather than seeding code the swarm will reject.
        code_files, reshape_err = reshape_mainnet_for_swarm(ch, code_files)
        if reshape_err:
            print(
                f"  ERROR {ch}: mainnet '{algo_name}' does not fit the swarm "
                f"format and could not be converted ({reshape_err}); skipping seed."
            )
            continue

        cu_files = sorted(p for p in code_files if p.endswith((".cu", ".cuh")))
        # `kernel_code` is single-kernel back-compat only; with multiple kernels
        # the map is the source of truth and we leave the scalar None.
        kernel_code = code_files[cu_files[0]] if len(cu_files) == 1 else None

        payload = {
            "admin_key": admin_key,
            "challenge": ch,
            "algorithm_code": code_files["mod.rs"],
            "algorithm_files": code_files,
            "kernel_code": kernel_code,
            "source_label": "tig-foundation",
        }
        try:
            body = post_json(
                f"{server_url.rstrip('/')}/api/admin/seed_inactive",
                payload, timeout=10,
            )
            if not body.get("seeded"):
                print(
                    f"  {ch}: already seeded ({body.get('reason', 'skipped')}); "
                    f"leaving the existing pool entry in place."
                )
            else:
                kernels = sorted(p for p in code_files if p.endswith((".cu", ".cuh")))
                extra = f", kernels={kernels}" if kernels else ""
                print(
                    f"  {ch}: seeded inactive pool "
                    f"(inactive_id={body.get('inactive_id')}, "
                    f"files={len(code_files)}{extra}, source={body.get('source')})"
                )
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            print(f"  {ch}: server rejected seed (HTTP {e.code}: {detail}); skipping.")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  {ch}: could not reach {server_url} ({e}); skipping seed.")


def encode_challenges_blob(challenges_cfg: dict) -> str:
    """gzip+base64 the per-challenge sub-config dict for transport as a single
    Railway service variable (`SWARM_CHALLENGES_B64`).

    The server applies it at boot in `db._apply_env_swarm_config`, so the swarm
    is configured the instant it comes up — no dependence on a post-deploy POST
    landing on the right container during rollout. Compact JSON + gzip keeps it
    well within Railway's per-variable size limit even for GPU challenges whose
    sub-configs embed the initial algorithm + kernel source; base64 keeps it to
    a single argv-safe token."""
    raw = json.dumps(challenges_cfg, separators=(",", ":")).encode()
    return base64.b64encode(gzip.compress(raw)).decode()


def _swarm_config_matches(live: dict, cfg: dict) -> bool:
    """True iff the server's live config reflects the intended swarm config."""
    return (
        live.get("active_challenge") == cfg["active_challenge"]
        and live.get("swarm_type") == cfg.get("swarm_type", "cpu")
        and set((live.get("available_challenges") or {}).keys()) >= set(cfg["challenges"])
    )


def push_config_to_server(
    server_url: str, admin_key: str, cfg: dict, *, deadline_s: int = 300,
) -> bool:
    """Ensure the running server reflects `cfg`, re-asserting until it sticks.

    The owner's config is also injected as boot-time env vars (see
    `db._apply_env_swarm_config`), so on a current server image the swarm is
    already configured before this runs and the first verify passes
    immediately. This POST stays as belt-and-suspenders: it re-asserts the
    config (idempotent — the endpoint upserts) and, crucially, *verifies it
    held*, which guards the historical failure where the config was lost.

    Why a deadline loop instead of a few quick retries: `railway up --ci`
    returns on build success, but the new container's health-rollout lags. A
    POST during that window can land on a transient/old container and be
    discarded once the persistent-/data container becomes authoritative —
    leaving the swarm on bare `DEFAULT_CONFIG` (satisfiability / cpu) forever,
    since `init_db` seeds those with INSERT OR IGNORE. So we keep POSTing and
    require the GET-back to match for TWO consecutive reads a few seconds apart
    (a single match can be the doomed container) before declaring success, up
    to `deadline_s`. Returns True once verified-stable, False if the deadline
    passes.

    `cfg["challenges"]` is a dict of {challenge: {tracks,
    scoring_direction, initial_algorithm_code, ...}}; `cfg["active_challenge"]`
    selects which one contributors auto-follow.
    """
    payload = {
        "admin_key": admin_key,
        "active_challenge": cfg["active_challenge"],
        "challenges": cfg["challenges"],
        "swarm_name": cfg.get("swarm_name", ""),
        "owner_name": cfg.get("owner_name", ""),
        "swarm_type": cfg.get("swarm_type", "cpu"),
        "stagnation_threshold": cfg.get("stagnation_threshold", 2),
        "stagnation_limit": cfg.get("stagnation_limit", 4),
        "hypothesis_recall_threshold": cfg.get("hypothesis_recall_threshold", 3),
    }
    url = f"{server_url.rstrip('/')}/api/swarm_config"

    deadline = time.time() + deadline_s
    last_err: Exception | None = None
    confirmations = 0
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            post_json(url, payload, timeout=30)
            # A 200 isn't proof: read the config back and require it to match
            # for two consecutive reads so a write that landed on a container
            # about to be replaced during rollout can't masquerade as success.
            with urllib.request.urlopen(url, timeout=15) as r:
                live = json.load(r)
            if _swarm_config_matches(live, cfg):
                confirmations += 1
                if confirmations >= 2:
                    print(f"  POSTed + verified config at {url} "
                          f"(active={live.get('active_challenge')}, "
                          f"type={live.get('swarm_type')})")
                    return True
                time.sleep(4)
                continue
            confirmations = 0
            last_err = RuntimeError(
                f"server reports active={live.get('active_challenge')!r} "
                f"type={live.get('swarm_type')!r} "
                f"challenges={sorted((live.get('available_challenges') or {}).keys())}"
            )
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError, ValueError) as e:
            confirmations = 0
            last_err = e
        if time.time() < deadline:
            print(f"  config push attempt {attempt} not yet confirmed "
                  f"({last_err}); retrying…")
            time.sleep(5)

    print(
        f"\n  ERROR: could not persist swarm config to {url} ({last_err}).\n"
        f"  The server is up but still on its DEFAULT config "
        f"(satisfiability / cpu) — contributors would run the WRONG challenge.\n"
        f"  Re-run `python setup.py create` (it resumes) once the server is\n"
        f"  reachable, or POST /api/swarm_config yourself with the admin_key\n"
        f"  from swarm.admin.json."
    )
    return False


def read_initial_algorithms() -> dict[str, dict[str, str]]:
    """Read per-challenge initial algorithm files. Missing files map to
    empty strings — agents start from a stub. Returns
    {challenge: {"algorithm_code": ..., "kernel_code": ...}}."""
    out: dict[str, dict[str, str]] = {}
    for ch in get_challenges():
        algo_path = ROOT / "initial_algorithms" / f"{ch}.rs"
        kernel_path = ROOT / "initial_algorithms" / f"{ch}.cu"
        out[ch] = {
            "algorithm_code": algo_path.read_text() if algo_path.is_file() else "",
            "kernel_code": kernel_path.read_text() if kernel_path.is_file() else "",
        }
    return out


def read_authored_seeds() -> list[dict]:
    """Scan ``initial_algorithms/<challenge>/seeds/`` for host-authored seed
    algorithms. Each ``<tag>.rs`` (with an optional matching ``<tag>.cu``) is
    one seed whose ``strategy_tag`` is the filename stem. These are handed to
    standard-tier / exploiter agents on a fresh trajectory instead of the
    stub. Returns a list of
    ``{challenge, strategy_tag, algorithm_code, kernel_code}``."""
    seeds: list[dict] = []
    for ch in get_challenges():
        seeds_dir = ROOT / "initial_algorithms" / ch / "seeds"
        if not seeds_dir.is_dir():
            continue
        for rs in sorted(seeds_dir.glob("*.rs")):
            cu = rs.with_suffix(".cu")
            seeds.append({
                "challenge": ch,
                "strategy_tag": rs.stem,
                "algorithm_code": rs.read_text(),
                "kernel_code": cu.read_text() if cu.is_file() else None,
            })
    return seeds


def seed_pool_from_authored(
    server_url: str, admin_key: str, seeds: list[dict],
) -> list[str]:
    """POST each host-authored seed to ``/api/admin/seed_pool``. Per-seed
    errors are warned-and-continued so one bad seed doesn't abort setup, but
    failures are RETURNED (as challenge/tag labels) so the caller can verify
    and retry — a silently empty pool means seeded agents get the bare stub
    and nobody notices until the "why is everything a cold start?" hunt.
    Idempotent — the server dedupes by (challenge, strategy_tag, source), so
    re-running create silently ignores seeds already present."""
    if not seeds:
        return []
    failed: list[str] = []
    print(f"Seeding the seed pool with {len(seeds)} authored algorithm(s)…")
    for s in seeds:
        payload = {
            "admin_key": admin_key,
            "challenge": s["challenge"],
            "strategy_tag": s["strategy_tag"],
            "algorithm_code": s["algorithm_code"],
            "kernel_code": s["kernel_code"],
        }
        label = f"{s['challenge']}/{s['strategy_tag']}"
        try:
            body = post_json(
                f"{server_url.rstrip('/')}/api/admin/seed_pool",
                payload, timeout=10,
            )
            status = "added" if body.get("seeded") else "already present"
            print(f"  {label}: seed {status}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            print(f"  {label}: server rejected seed (HTTP {e.code}: {detail}).")
            failed.append(label)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  {label}: could not reach {server_url} ({e}).")
            failed.append(label)
    return failed


def verify_seed_pool(
    server_url: str, admin_key: str, seeds: list[dict], *, deadline_s: int = 90,
) -> list[str]:
    """Verify every authored seed is actually IN the server's seed pool,
    re-depositing missing ones until the deadline. Same belt-and-suspenders
    rationale as `push_config_to_server`: a POST during the deploy's
    health-rollout window can land on a doomed container and vanish, leaving
    the pool empty with create's output looking successful.

    Reads back via ``POST /api/admin/seeds`` (metadata listing). Returns the
    labels still missing at the deadline — empty list means verified. A
    server predating the listing endpoint (HTTP 404) can't be verified;
    that's reported and treated as unverified-but-not-fatal ([] returned)
    so resumed creates against old images don't hard-fail."""
    if not seeds:
        return []
    wanted: dict[str, set[str]] = {}
    for s in seeds:
        wanted.setdefault(s["challenge"], set()).add(s["strategy_tag"])
    by_label = {f"{s['challenge']}/{s['strategy_tag']}": s for s in seeds}

    deadline = time.time() + deadline_s
    missing: list[str] = []
    while True:
        missing = []
        for challenge, tags in wanted.items():
            try:
                body = post_json(
                    f"{server_url.rstrip('/')}/api/admin/seeds",
                    {"admin_key": admin_key, "challenge": challenge},
                    timeout=10,
                )
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    print(
                        "  note: this server predates /api/admin/seeds — "
                        "cannot verify the seed pool; check it in the Admin "
                        "Console after redeploying."
                    )
                    return []
                missing.extend(f"{challenge}/{t}" for t in sorted(tags))
                continue
            except (urllib.error.URLError, TimeoutError, OSError):
                missing.extend(f"{challenge}/{t}" for t in sorted(tags))
                continue
            present = {
                s["strategy_tag"] for s in body.get("seeds", [])
                if s.get("source") == "authored"
            }
            missing.extend(f"{challenge}/{t}" for t in sorted(tags - present))
        if not missing or time.time() >= deadline:
            return missing
        print(f"  seed pool incomplete ({len(missing)} missing) — re-depositing…")
        seed_pool_from_authored(
            server_url, admin_key, [by_label[m] for m in missing],
        )
        time.sleep(3)


def fetch_challenge_sub_config(server_url: str, challenge: str) -> dict | None:
    """Pull a challenge's tracks/timeout/scoring_direction from the live
    server. Used by switch / sync to mirror the active challenge's sub-config
    into .swarm-cache.json so benchmark.py's offline fallback keeps working."""
    try:
        with urllib.request.urlopen(
            f"{server_url.rstrip('/')}/api/swarm_config", timeout=4,
        ) as r:
            data = json.load(r)
    except Exception:
        return None
    available = (data.get("available_challenges") or {})
    return available.get(challenge)


def collect_per_challenge_configs(
    initial_algorithms: dict[str, dict[str, str]],
    *,
    use_defaults: bool,
    challenge_set: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Build the `challenges` payload for POST /api/swarm_config, either by
    accepting defaults across all challenges (use_defaults=True, no prompts)
    or by asking the host for tracks/timeout per challenge.

    `challenge_set` restricts which challenges are configured (defaults to
    all). Used to only configure CPU or GPU challenges based on swarm type.
    """
    challenges: dict[str, dict] = {}
    target = challenge_set if challenge_set is not None else get_challenges()
    for ch, meta in target.items():
        ch_def = _load_challenge_registry()[ch]
        tracks: dict = {"seed": "test"}
        if use_defaults:
            default_tracks = DEFAULT_TRACKS_PER_CHALLENGE.get(ch)
            if default_tracks:
                for key, count in default_tracks.items():
                    tracks[key] = count
            else:
                for key in meta["track_keys"]:
                    tracks[key] = DEFAULT_INSTANCES_PER_TRACK
        else:
            print(f"\n── {ch} ──")
            ch_track_defaults = DEFAULT_TRACKS_PER_CHALLENGE.get(ch, {})
            for key in meta["track_keys"]:
                tracks[key] = prompt_int(
                    f"  instances for {key}",
                    ch_track_defaults.get(key, 0),
                    minimum=0,
                )
        algo_data = initial_algorithms.get(ch, {})
        sub: dict = {
            "tracks": tracks,
            "scoring_direction": meta["scoring_direction"],
            "initial_algorithm_code": algo_data.get("algorithm_code", ""),
            "strategy_tags": meta.get("strategy_tags", []),
        }
        if algo_data.get("kernel_code"):
            sub["initial_kernel_code"] = algo_data["kernel_code"]
        challenges[ch] = sub
    return challenges


def write_challenge_md(challenge: str) -> None:
    src = ROOT / "src" / challenge / "README.md"
    dst = ROOT / "CHALLENGE.md"
    if not src.exists():
        print(f"  warning: no README at {src.relative_to(ROOT)}; skipping CHALLENGE.md")
        return
    dst.write_text(src.read_text())
    print(f"  wrote {dst.relative_to(ROOT)} (from {src.relative_to(ROOT)})")


# ── Modes ────────────────────────────────────────────────────────────


def create_swarm(params: dict, progress_cb=None) -> dict:
    """Non-interactive core of `run_create`: provision + configure a swarm.

    Assumes the Railway CLI is installed and authenticated and that every
    wizard decision is already resolved into `params`. Performs the side-effect
    sequence (Railway provision → env vars → volume → deploy → domain → wait →
    push config → seed pools → write local files) and streams human-readable
    progress through `progress_cb(msg)` (each line is also printed). Shared by
    the CLI wizard and the control-ui host companion so the deploy logic lives
    in one place.

    `params`: swarm_name, workspace (optional), swarm_type ("cpu"|"gpu"),
    active_challenge, challenges_cfg (from collect_per_challenge_configs),
    stagnation_threshold, stagnation_limit, hypothesis_recall_threshold,
    seed_inactive_pool (bool), seedable (set, optional).

    Returns: server_url, admin_key, swarm_password, active_challenge,
    swarm_type, type_label, n_challenges, config_ok."""
    def emit(msg: str) -> None:
        print(msg)
        if progress_cb is not None:
            try:
                progress_cb(msg)
            except Exception:  # a progress consumer must never break the deploy
                pass

    swarm_name = params["swarm_name"]
    workspace = params.get("workspace")
    swarm_type = params["swarm_type"]
    is_gpu_swarm = swarm_type == "gpu"
    challenge_set = get_gpu_challenges() if is_gpu_swarm else get_cpu_challenges()
    n_challenges = len(challenge_set)
    type_label = "GPU" if is_gpu_swarm else "CPU"
    active_challenge = params["active_challenge"]
    challenges_cfg = params["challenges_cfg"]
    challenge_meta = challenge_set[active_challenge]
    active_def = _load_challenge_registry()[active_challenge]
    stagnation_threshold = params["stagnation_threshold"]
    stagnation_limit = params["stagnation_limit"]
    hypothesis_recall_threshold = params["hypothesis_recall_threshold"]
    # HPO knobs default in for other callers (e.g. the control-ui companion)
    # that don't set them explicitly.
    hpo_first_tune_improvements = params.get("hpo_first_tune_improvements", 10)
    hpo_min_improvements = params.get("hpo_min_improvements", 4)
    hpo_search_budget = params.get("hpo_search_budget", 13)
    hpo_num_suggested_configs = params.get("hpo_num_suggested_configs", 5)
    seed_inactive_pool = params.get("seed_inactive_pool", False)
    seedable = params.get("seedable")
    if seedable is None:
        seedable = set(challenge_set.keys())

    initial_algorithms = read_initial_algorithms()

    admin_key = secrets.token_urlsafe(16)
    swarm_password = secrets.token_urlsafe(16)

    railway_dir = ROOT / ".railway"
    if railway_dir.exists():
        emit(f"Removing existing {railway_dir.relative_to(ROOT)} from a prior run.")
        shutil.rmtree(railway_dir)

    emit("Provisioning on Railway…")
    project, service, resumed = _railway_provision(swarm_name, workspace)
    if resumed:
        emit("  (resuming a prior half-finished run — adopting existing resources)")

    # USER is absent on Windows (USERNAME is the equivalent there).
    owner_name = os.environ.get("USER") or os.environ.get("USERNAME") or "owner"
    emit("  setting environment variables…")
    # The swarm config travels as deploy-time env vars (applied at server boot
    # by db._apply_env_swarm_config) so the swarm comes up correctly configured
    # the instant it deploys — independent of whether the post-deploy POST
    # below lands during the Railway rollout.
    _railway_set_variables(swarm_name, {
        "DATA_DIR": "/data",
        "ADMIN_KEY": admin_key,
        "SWARM_PASSWORD": swarm_password,
        "ACTIVE_CHALLENGE": active_challenge,
        "SWARM_TYPE": swarm_type,
        "SWARM_NAME": swarm_name,
        "OWNER_NAME": owner_name,
        "STAGNATION_THRESHOLD": str(stagnation_threshold),
        "STAGNATION_LIMIT": str(stagnation_limit),
        "HYPOTHESIS_RECALL_THRESHOLD": str(hypothesis_recall_threshold),
        "HPO_FIRST_TUNE_IMPROVEMENTS": str(hpo_first_tune_improvements),
        "HPO_MIN_IMPROVEMENTS": str(hpo_min_improvements),
        "HPO_SEARCH_BUDGET": str(hpo_search_budget),
        "HPO_NUM_SUGGESTED_CONFIGS": str(hpo_num_suggested_configs),
        "SWARM_CHALLENGES_B64": encode_challenges_blob(challenges_cfg),
    })

    emit("  attaching /data volume…")
    _railway_add_volume(swarm_name, "/data")

    emit("  deploying (build logs follow; this takes a few minutes)…\n")
    _railway_up(swarm_name)

    emit("\n  fetching public URL…")
    server_url = _railway_domain(swarm_name)
    emit(f"  URL: {server_url}")

    emit("  waiting for the server to come online…")
    if not _wait_for_server(server_url):
        emit(
            "  warning: server did not respond at /api/swarm_config within 4 minutes.\n"
            "  Check `railway logs` for errors. Once it's up, the URL will be\n"
            f"  reachable at {server_url} — point fleet.config.json's server_url at it."
        )

    n_with_code = sum(1 for v in initial_algorithms.values() if v.get("algorithm_code", "").strip())
    n_total = len(initial_algorithms)
    emit(f"  read initial algorithms from initial_algorithms/ "
         f"({n_with_code}/{n_total} have content; the rest broadcast empty)")

    # Top-level `tracks` and `timeout` mirror the active challenge's
    # sub-config so `scripts/benchmark.py`'s offline fallback keeps working.
    active_sub = challenges_cfg[active_challenge]
    cfg = {
        "swarm_name": swarm_name,
        "owner_name": owner_name,
        "server_url": server_url,
        "admin_key": admin_key,
        "swarm_password": swarm_password,
        "role": "owner",
        "swarm_type": swarm_type,
        "active_challenge": active_challenge,
        "challenge": active_challenge,
        "challenges": challenges_cfg,
        "stagnation_threshold": stagnation_threshold,
        "stagnation_limit": stagnation_limit,
        "hypothesis_recall_threshold": hypothesis_recall_threshold,
        "scoring_direction": challenge_meta["scoring_direction"],
        "tracks": active_sub["tracks"],
        "algorithm_path": f"src/{active_challenge}/algorithm/mod.rs",
    }
    if active_def.is_gpu:
        cfg["kernel_path"] = f"src/{active_challenge}/algorithm/kernels.cu"
        cfg["is_gpu"] = True

    emit("  verifying swarm config on the server…")
    config_ok = push_config_to_server(server_url, admin_key, cfg)

    # Only seed once the config is verified — seeding a server that's still on
    # bare defaults loads pool entries for challenges nobody is running.
    seeds_ok = True
    if config_ok:
        if seed_inactive_pool:
            emit("\nSeeding inactive trajectory pool from TIG mainnet…")
            seed_inactive_pool_from_mainnet(server_url, admin_key, seedable)

        authored_seeds = read_authored_seeds()
        if authored_seeds:
            emit("")
            seed_pool_from_authored(server_url, admin_key, authored_seeds)
            emit("  verifying the seed pool on the server…")
            still_missing = verify_seed_pool(server_url, admin_key, authored_seeds)
            if still_missing:
                seeds_ok = False
                emit(
                    "  ERROR: seed pool verification FAILED — missing: "
                    + ", ".join(still_missing)
                )
                emit(
                    "  Seeded agents will get the bare stub on those challenges. "
                    "Re-run `python setup.py create` once the server is stable "
                    "(idempotent), or deposit via the Admin Console."
                )
            else:
                emit(f"  seed pool verified: all {len(authored_seeds)} authored seed(s) present.")
    else:
        # Config never verified, so seeding was skipped — the pool is EMPTY,
        # which historically read as "agents mysteriously start from the stub".
        seeds_ok = False
        emit(
            "  WARNING: skipping seed-pool load (swarm config unverified) — "
            "the seed pool is EMPTY until you re-run `python setup.py create`."
        )

    emit("\nWriting local files…")
    template_files(
        server_url,
        challenge=active_challenge,
        algorithm_path=cfg["algorithm_path"],
        prior=read_swarm_cache(),
    )
    write_challenge_md(active_challenge)
    write_swarm_admin(cfg)
    write_swarm_cache(cfg)
    _scaffold_fleet_config(server_url, swarm_password)

    return {
        "server_url": server_url,
        "admin_key": admin_key,
        "swarm_password": swarm_password,
        "active_challenge": active_challenge,
        "swarm_type": swarm_type,
        "type_label": type_label,
        "n_challenges": n_challenges,
        "config_ok": config_ok,
        "seeds_ok": seeds_ok,
    }


# HPO knobs are good out of the box, so the wizard hides them behind one opt-in
# question. Kept here so both wizard branches resolve them identically. Values
# mirror the server's SWARM_DEFAULTS; the client driver reads them via the
# pushed swarm config (scripts/run_loop.py's HPO gate + search).
_HPO_ORDER = (
    "hpo_first_tune_improvements", "hpo_min_improvements",
    "hpo_search_budget", "hpo_num_suggested_configs",
)
_HPO_DEFAULTS = {
    "hpo_first_tune_improvements": 10,
    "hpo_min_improvements": 4,
    "hpo_search_budget": 13,
    "hpo_num_suggested_configs": 5,
}
_HPO_PROMPTS = {
    "hpo_first_tune_improvements": (
        "HPO first-tune improvements (per-trajectory improvements before a "
        "trajectory's FIRST tune)", 1),
    "hpo_min_improvements": (
        "HPO min improvements (improvements before later tunes; also the "
        "tune-band width)", 1),
    "hpo_search_budget": (
        "HPO search budget (configs evaluated per tune, incl. the default + "
        "LLM-suggested)", 1),
    "hpo_num_suggested_configs": (
        "HPO LLM-suggested configs (max LLM-proposed configs in the budget; "
        "0 = random draws only)", 0),
}


def _resolve_hpo_settings(
    args: argparse.Namespace | None, *, interactive: bool = False,
) -> tuple[int, int, int, int]:
    """Resolve the 4 host-tunable HPO knobs (in `_HPO_ORDER`).

    CLI flags win when present. The `--yes` / use-defaults path takes the
    defaults silently. The interactive wizard asks one opt-in question and only
    prompts for the 4 values if the host says yes (defaults are sensible, so
    most hosts skip). Any knob left unset falls back to its default.
    """
    vals = {k: _arg_value(args, k) for k in _HPO_ORDER}
    edit = any(vals[k] is not None for k in _HPO_ORDER)  # a flag was passed
    if interactive and not edit:
        ans = prompt(
            "Edit HPO (hyperparameter-optimization) settings? The defaults are "
            "sensible — only tune if you know you want to. [y/N]",
            default="N",
        )
        edit = ans.strip().lower() in ("y", "yes")
    if edit and interactive:
        for k in _HPO_ORDER:
            if vals[k] is None:
                label, minimum = _HPO_PROMPTS[k]
                vals[k] = prompt_int(label, _HPO_DEFAULTS[k], minimum=minimum)
    for k in _HPO_ORDER:
        if vals[k] is None:
            vals[k] = _HPO_DEFAULTS[k]
    return tuple(vals[k] for k in _HPO_ORDER)


def run_create(args: argparse.Namespace | None = None) -> int:
    """Owner setup: configure a new swarm and deploy it on Railway.

    End-to-end: verify `railway` CLI + auth → wizard prompts → reset any
    prior `.railway/` link in this clone → `railway init` → `railway add
    --service` → `railway variable set` (DATA_DIR, ADMIN_KEY) → `railway
    volume add --mount-path /data` → `railway up --ci` (blocks until the
    deploy is live) → `railway domain --json` → POST swarm-wide config.

    Re-running on a clone that already created a swarm is fine: this
    deletes `.railway/` and creates a fresh project on Railway. The
    previous swarm is unaffected — it lives independently in your Railway
    workspace; manage it via the Railway dashboard."""
    print("TIG Swarm — create a new swarm on Railway")
    print("=" * 48)

    _railway_check_installed()
    user = _railway_check_auth()
    who = user.get("email") or user.get("name") or "unknown"
    print(f"  authed as Railway user: {who}\n")
    workspace = _arg_value(args, "workspace") or _pick_workspace(user)

    yes = _arg_enabled(args, "yes")
    swarm_type = _arg_value(args, "swarm_type")
    if not swarm_type:
        swarm_type = "cpu" if yes else prompt_choice(
            "\nWhat type of swarm is this?",
            ["cpu", "gpu"],
            default="cpu",
        )
    is_gpu_swarm = swarm_type == "gpu"
    challenge_set = get_gpu_challenges() if is_gpu_swarm else get_cpu_challenges()
    n_challenges = len(challenge_set)
    type_label = "GPU" if is_gpu_swarm else "CPU"
    print(f"  -> {type_label} swarm ({n_challenges} challenges available)")

    swarm_name = _arg_value(args, "swarm_name")
    if not swarm_name:
        swarm_name = "my-tig-swarm" if yes else prompt(
            "\nSwarm name (used as Railway project + service name; lowercase + dashes)",
            default="my-tig-swarm",
        )

    print(
        f"\nThis swarm hosts all {n_challenges} {type_label} challenges in parallel.\n"
        "The host picks ONE active challenge that contributors automatically\n"
        "work on; you can flip between challenges later via `python setup.py\n"
        "switch` and per-challenge state is preserved on the server (so\n"
        "resuming a previous challenge picks up every agent's prior trajectory).\n"
    )

    use_defaults = _arg_enabled(args, "use_defaults") or yes
    if not use_defaults:
        use_defaults_ans = prompt(
            f"Use defaults for all {n_challenges} challenges? "
            f"({DEFAULT_INSTANCES_PER_TRACK} instances per track, "
            f"default timeout per challenge, empty initial algorithm) [Y/n]",
            default="Y",
        )
        use_defaults = use_defaults_ans.strip().lower() not in ("n", "no")

    # Optional: seed the server's inactive_algorithms pool with the current
    # top-earning TIG mainnet algorithm. Covers every challenge in the swarm
    # (multi-file aware); challenges with no fetchable/compatible mainnet algo
    # are warned-and-skipped per challenge at seed time. Resolved here so we
    # honor --yes / --seed-inactive-pool / wizard input, but the actual fetch +
    # POST is deferred until after the server is up.
    seedable = set(challenge_set.keys())
    seed_inactive_pool = _arg_enabled(args, "seed_inactive_pool")
    if seedable and not seed_inactive_pool and not yes:
        ans = prompt(
            f"Seed the inactive trajectory pool with the current top-earning "
            f"TIG mainnet algorithm for {', '.join(sorted(seedable))}? [y/N]",
            default="N",
        )
        seed_inactive_pool = ans.strip().lower() in ("y", "yes")
    if seed_inactive_pool and not seedable:
        # Host passed the flag on a swarm with no challenges configured —
        # warn rather than silently ignoring.
        print(
            "  --seed-inactive-pool requested but this swarm has no "
            "challenges; nothing to seed."
        )
        seed_inactive_pool = False

    initial_algorithms = read_initial_algorithms()
    challenges_cfg = collect_per_challenge_configs(
        initial_algorithms, use_defaults=use_defaults, challenge_set=challenge_set,
    )

    challenge_names = list(challenge_set.keys())
    default_active = challenge_names[0]
    active_challenge = _arg_value(args, "active_challenge")
    if active_challenge and active_challenge not in challenge_names:
        print(f"{active_challenge} is not available in a {type_label} swarm.")
        print(f"Available challenges: {', '.join(challenge_names)}")
        return 1
    if not active_challenge:
        active_challenge = default_active if yes else prompt_choice(
            "\nWhich challenge should this swarm START with as the active challenge?",
            challenge_names,
            default=default_active,
        )
    challenge_meta = challenge_set[active_challenge]
    print(f"  -> active = {active_challenge} (contributors auto-follow this)")

    if use_defaults:
        # Sensible defaults for the global stagnation knobs; the host can
        # tweak via curl /api/swarm_config later if they want.
        stagnation_threshold = _arg_value(args, "stagnation_threshold") or 2
        stagnation_limit = _arg_value(args, "stagnation_limit")
        stagnation_limit = 4 if stagnation_limit is None else stagnation_limit
        hypothesis_recall_threshold = _arg_value(args, "hypothesis_recall_threshold") or 3
        (hpo_first_tune_improvements, hpo_min_improvements,
         hpo_search_budget, hpo_num_suggested_configs) = _resolve_hpo_settings(args)
    else:
        stagnation_threshold = _arg_value(args, "stagnation_threshold") or prompt_int(
            "Stagnation threshold (iterations without improvement before hints/inspiration)",
            2, minimum=1,
        )
        stagnation_limit = _arg_value(args, "stagnation_limit")
        if stagnation_limit is None:
            stagnation_limit = prompt_int(
                "Stagnation limit (iterations without improvement before trajectory reset, 0=disabled)",
                4, minimum=0,
            )
        hypothesis_recall_threshold = _arg_value(args, "hypothesis_recall_threshold") or prompt_int(
            "Hypothesis recall threshold (iterations without improvement before "
            "showing prior failed hypotheses for the current program)",
            3, minimum=1,
        )
        (hpo_first_tune_improvements, hpo_min_improvements,
         hpo_search_budget, hpo_num_suggested_configs) = _resolve_hpo_settings(
            args, interactive=True,
        )

    # Hand the resolved wizard decisions to the non-interactive core, which does
    # the Railway provisioning + config push + seeding + local-file writes. The
    # control-ui host companion calls the same create_swarm() with a progress_cb.
    result = create_swarm({
        "swarm_name": swarm_name,
        "workspace": workspace,
        "swarm_type": swarm_type,
        "active_challenge": active_challenge,
        "challenges_cfg": challenges_cfg,
        "stagnation_threshold": stagnation_threshold,
        "stagnation_limit": stagnation_limit,
        "hypothesis_recall_threshold": hypothesis_recall_threshold,
        "hpo_first_tune_improvements": hpo_first_tune_improvements,
        "hpo_min_improvements": hpo_min_improvements,
        "hpo_search_budget": hpo_search_budget,
        "hpo_num_suggested_configs": hpo_num_suggested_configs,
        "seed_inactive_pool": seed_inactive_pool,
        "seedable": seedable,
    })
    server_url = result["server_url"]
    admin_key = result["admin_key"]
    swarm_password = result["swarm_password"]
    config_ok = result["config_ok"]
    seeds_ok = result.get("seeds_ok", True)
    repo_url = "<this-repo-url>"
    try:
        result = sp.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, cwd=str(ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            repo_url = result.stdout.strip()
    except Exception:
        pass
    repo_dir_hint = (
        Path(repo_url).stem.replace(".git", "")
        if repo_url != "<this-repo-url>"
        else "tig-swarm-demo"
    )

    print("\n" + "=" * 48)
    if config_ok:
        print(f"{type_label} SWARM IS LIVE")
    else:
        print(f"{type_label} SWARM DEPLOYED — CONFIG NOT APPLIED")
    print("=" * 48)
    print(f"\n  Dashboard:  {server_url}/")
    print(f"  Swarm type:  {type_label}")
    print(f"  Active challenge:  {active_challenge}")
    if config_ok:
        print(f"  All {n_challenges} {type_label} challenges configured and ready (switch via `setup.py switch <name>`).")
        if not seeds_ok:
            print(
                "  WARNING: the SEED POOL could not be verified — seeded agents "
                "will start from the bare stub.\n"
                "  Re-run `python setup.py create` (idempotent) to retry seeding, "
                "then check Pools → Seed pool in the Admin Console."
            )
    else:
        print(
            f"  WARNING: the server is still on its DEFAULT config "
            f"(satisfiability / cpu), NOT {active_challenge} / {type_label.lower()}.\n"
            f"  Contributors would run the wrong challenge. Re-run "
            f"`python setup.py create` (it resumes) to push the config once the\n"
            f"  server is reachable, before onboarding anyone."
        )
    print("\n  Onboard each contributor with:\n")
    print("    python setup.py invite [<username>]")
    print("    # Prints a one-line JOIN LINK (plus the raw values for the")
    print("    # manual flow). Omit <username> to auto-generate a random slug.")
    print("    # They open the link, configure agents in the browser, then run")
    print('    #   python run.py --join "<link>"   (or the no-clone paths in the README).')
    print("\n  Boot a bad actor any time with:\n")
    print("    python setup.py revoke <username>")
    print("    # blocks future registers, kills their agents, and (if a runner")
    print("    # is set) tears down their hosted fleet + purges their keys.")
    print("\n  Base password (keep private — used by `setup.py invite` to derive")
    print("  per-contributor passwords; rotating it kicks every contributor):")
    print(f"    {swarm_password}")
    print("\n  Admin key (keep private — gates /api/admin/*):")
    print(f"    {admin_key}")
    print("\n  Your own clone has been scaffolded with fleet.config.json —")
    print("  edit the agent entry then run `python scripts/run_fleet.py` to participate.")
    print("\n  Manage the service in Railway: https://railway.com/dashboard")
    print()
    # Non-zero exit when the config OR the seed pool never verified so the
    # failure is loud (CI, scripts, and the operator all see it) instead of a
    # half-initialised swarm masquerading as ready.
    return 0 if (config_ok and seeds_ok) else 1


def run_create_runner(args: argparse.Namespace | None = None) -> int:
    """Deploy the hosted fleet runner (Tier 1) as its own Railway service and
    point the swarm at it — the automated equivalent of the manual runner
    steps `setup.py create` prints. Requires an existing swarm (reads its
    server_url + admin_key from swarm.admin.json)."""
    print("TIG Swarm — deploy the hosted fleet runner (zero-install tier)")
    print("=" * 48)

    _railway_check_installed()
    user = _railway_check_auth()
    who = user.get("email") or user.get("name") or "unknown"
    print(f"  authed as Railway user: {who}\n")
    workspace = _arg_value(args, "workspace") or _pick_workspace(user)

    admin = read_swarm_admin()
    server_url = (
        admin.get("server_url") or read_swarm_cache().get("server_url") or ""
    ).rstrip("/")
    admin_key = (admin.get("admin_key") or "").strip()
    if not server_url or not admin_key:
        print(
            "create-runner: no existing swarm found (need server_url + admin_key\n"
            "  in swarm.admin.json). Run `python setup.py create` first.",
            file=sys.stderr,
        )
        return 1

    swarm_name = admin.get("swarm_name") or "tig-swarm"
    runner_name = _arg_value(args, "runner_name") or f"{swarm_name}-runner"
    # Fernet key = 32 random bytes, urlsafe-base64. Generated with stdlib so the
    # host CLI stays dependency-free (runner.vault reads this exact format).
    secret_key = base64.urlsafe_b64encode(os.urandom(32)).decode()

    print(f"Deploying runner service '{runner_name}' on Railway…")
    _project, service, _resumed = _railway_provision(runner_name, workspace)
    svc = service.get("name", runner_name)

    print("  setting environment variables…")
    _railway_set_variables(svc, {
        # Build runner/Dockerfile, not the root (server) Dockerfile.
        "RAILWAY_DOCKERFILE_PATH": "runner/Dockerfile",
        "RUNNER_SECRET_KEY": secret_key,
        "COORDINATION_SERVER_URL": server_url,
        "RUNNER_ADMIN_KEY": admin_key,
        "RUNNER_DATA_DIR": "/data",
        "RUNNER_WORKSPACES": "/data/workspaces",
    })

    print("  attaching /data volume (enrollment DB + per-contributor workspaces)…")
    _railway_add_volume(svc, "/data")

    print("  deploying (build logs follow; this takes a few minutes)…\n")
    _railway_up(svc)

    print("\n  fetching public URL…")
    runner_url = _railway_domain(svc)
    print(f"  runner URL: {runner_url}")

    print("  waiting for the runner to come online…")
    if not _wait_for_server(runner_url, probe_path="/api/runner/health"):
        print(
            "\n  WARNING: the runner didn't answer /api/runner/health in time — it\n"
            "  may still be rolling out. Once it's up, finish by running:\n"
            f"    python setup.py set-runner {runner_url}",
            file=sys.stderr,
        )
        return 1

    print("  pointing the swarm at the runner…")
    from .contributors import run_set_runner
    rc = run_set_runner(runner_url)
    if rc != 0:
        return rc

    print("\n" + "=" * 48)
    print("HOSTED RUNNER IS LIVE")
    print("=" * 48)
    print(f"\n  Runner:  {runner_url}")
    print(f"  Swarm:   {server_url}")
    print("\n  NOTE: the join page's cloud tab is currently disabled in the UI;")
    print("  the runner is reachable via its API only (see runner/README.md).")
    print("  Manage it in Railway: https://railway.com/dashboard")
    print("\n  NOTE: your clone's .railway/ link now points at the runner project;")
    print("  swarm admin (switch/invite/revoke) uses the server API, so it's")
    print("  unaffected. Re-run `setup.py create` (resume) to relink the swarm.")
    print()
    return 0


def _scaffold_fleet_config(server_url: str, swarm_password: str) -> None:
    """After `setup.py create`, leave the host with a working fleet.config.json
    so they can immediately participate via `python scripts/run_fleet.py`.
    Skipped if a fleet.config.json already exists — never clobbers user edits."""
    path = ROOT / "fleet.config.json"
    if path.exists():
        print(f"  fleet.config.json already present — leaving as-is")
        return
    starter = {
        "server_url": server_url,
        "swarm_password": swarm_password,
        "agents": [
            {
                "name": os.environ.get("USER") or os.environ.get("USERNAME") or "agent-1",
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "api_key_env": "ANTHROPIC_API_KEY",
                "compute": "local",
            }
        ],
    }
    path.write_text(json.dumps(starter, indent=2) + "\n")
    print(f"  scaffolded {path.relative_to(ROOT)} (one agent — edit before launching)")


# ── Switch / sync subcommands ─────────────────────────────────────────


def switch_challenge(challenge: str) -> dict:
    """Non-interactive core of `run_switch`: change the swarm's active challenge.

    POSTs to /api/swarm_config (admin-key gated), then refreshes the local
    .swarm-cache.json and re-templates CHALLENGE.md. Switching is restricted to
    challenges of the same type (CPU/GPU) as the swarm was created with.

    Raises ValueError(message) on any failure (unknown challenge, missing
    admin creds, type mismatch, unreachable server). Returns
    {active_challenge, prior_challenge, server_url} on success. Shared by the
    CLI wrapper and the control-ui host companion."""
    if challenge not in get_challenges():
        raise ValueError(
            f"unknown challenge: {challenge}; choose from {', '.join(get_challenges())}"
        )
    admin = read_swarm_admin()
    if not admin.get("admin_key"):
        raise ValueError(
            "swarm.admin.json not found — switch is host-only; run "
            "`python setup.py create` first."
        )
    server_url = resolve_server_url()
    if not server_url:
        raise ValueError("no server_url found — run `python setup.py create` first.")
    admin_key = admin["admin_key"]
    cache = read_swarm_cache()
    swarm_type = cache.get("swarm_type", "cpu")
    is_gpu_swarm = swarm_type == "gpu"
    target_is_gpu = _load_challenge_registry()[challenge].is_gpu
    if target_is_gpu != is_gpu_swarm:
        allowed = get_gpu_challenges() if is_gpu_swarm else get_cpu_challenges()
        label = "GPU" if is_gpu_swarm else "CPU"
        raise ValueError(
            f"This is a {label} swarm — cannot switch to "
            f"{'GPU' if target_is_gpu else 'CPU'} challenge '{challenge}'. "
            f"Available challenges: {', '.join(allowed)}"
        )

    # 1. POST the new active_challenge to the server.
    try:
        post_json(
            f"{server_url.rstrip('/')}/api/swarm_config",
            {"admin_key": admin_key, "active_challenge": challenge},
            timeout=8,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise ValueError(f"could not reach {server_url} ({e}); aborting switch.")

    # 2. Refresh the local cache + CHALLENGE.md so the host can also work on
    #    the new challenge from their own clone.
    new_algo_path = f"src/{challenge}/algorithm/mod.rs"
    ch_def = _load_challenge_registry()[challenge]
    sub = fetch_challenge_sub_config(server_url, challenge)
    template_files(
        server_url, challenge=challenge,
        algorithm_path=new_algo_path, prior=cache,
    )
    write_challenge_md(challenge)
    refreshed = {
        "server_url": server_url,
        "swarm_type": swarm_type,
        "active_challenge": challenge,
        "challenge": challenge,
        "algorithm_path": new_algo_path,
    }
    if ch_def.is_gpu:
        refreshed["kernel_path"] = f"src/{challenge}/algorithm/kernels.cu"
        refreshed["is_gpu"] = True
    if sub:
        refreshed["tracks"] = sub.get("tracks", {})
        refreshed["scoring_direction"] = sub.get("scoring_direction", "max")
    # Carry the stagnation knobs into the host's cache too (switch doesn't
    # fetch /api/swarm_config, so source them from swarm.admin.json).
    for knob in ("stagnation_threshold", "stagnation_limit"):
        if admin.get(knob) is not None:
            refreshed[knob] = admin[knob]
    write_swarm_cache(refreshed)

    return {
        "active_challenge": challenge,
        "prior_challenge": cache.get("active_challenge"),
        "server_url": server_url,
    }


def run_switch(challenge: str) -> int:
    """Host-only CLI: change the swarm's active challenge (wraps
    switch_challenge). Contributors auto-follow on their next iteration via
    `setup.py sync`."""
    try:
        result = switch_challenge(challenge)
    except (ValueError, RuntimeError) as e:
        print(str(e))
        return 1

    prior_challenge = result["prior_challenge"]
    print(f"\nActive challenge → {challenge} (broadcast to all contributors).")
    if prior_challenge and prior_challenge != challenge:
        print(f"  Prior trajectories on {prior_challenge} are preserved")
        print(f"  server-side and resume on switch-back.")
    print("  All contributors auto-follow on their next iteration —")
    print("  scripts/run_loop.py runs `setup.py sync` at the top of each loop.")
    return 0


def run_sync() -> int:
    """Pull live config from the server and refresh .swarm-cache.json.

    Idempotent — re-templates CHALLENGE.md only when active_challenge changes.
    Called by scripts/run_loop.py at the top of every iteration so a host's
    challenge switch propagates to running contributors automatically.
    """
    server_url = resolve_server_url()
    if not server_url:
        print(
            "no server_url found — run `python setup.py create` (host) or "
            "edit fleet.config.json (contributor)."
        )
        return 1
    server_url = server_url.rstrip("/")

    try:
        with urllib.request.urlopen(
            f"{server_url}/api/swarm_config", timeout=4
        ) as r:
            live = json.load(r)
    except Exception as e:
        print(f"  could not reach {server_url} ({e}); skipping sync.")
        return 0

    new_challenge = live.get("active_challenge") or live.get("challenge")
    if not new_challenge:
        print("server returned no active_challenge; nothing to sync.")
        return 0

    cache = read_swarm_cache()
    # If the cache was written against a different server, it's a leftover from
    # a prior swarm (e.g. fleet.config.json was repointed). Treat it as absent
    # so we don't take the early-return below and don't feed its stale
    # prior_url into template_files (which would mis-rewrite URLs).
    cache_server = (cache.get("server_url") or "").rstrip("/")
    if cache_server and cache_server != server_url:
        cache = {}
    local_challenge = cache.get("active_challenge") or cache.get("challenge")

    # Build the refreshed cache payload from live server state.
    new_algo_path = f"src/{new_challenge}/algorithm/mod.rs"
    try:
        ch_def = _load_challenge_registry().get(new_challenge)
        challenge_is_gpu = bool(ch_def and ch_def.is_gpu)
    except RuntimeError:
        # server/ missing from this clone — sync still works; derive
        # GPU-ness from the live server config instead.
        challenge_is_gpu = bool(live.get("is_gpu"))
    sub = fetch_challenge_sub_config(server_url, new_challenge)
    refreshed = {
        "server_url": server_url,
        "active_challenge": new_challenge,
        "challenge": new_challenge,
        "algorithm_path": new_algo_path,
    }
    if live.get("swarm_type"):
        refreshed["swarm_type"] = live["swarm_type"]
    if challenge_is_gpu:
        refreshed["kernel_path"] = f"src/{new_challenge}/algorithm/kernels.cu"
        refreshed["is_gpu"] = True
    if sub:
        refreshed["tracks"] = sub.get("tracks", {})
        refreshed["scoring_direction"] = sub.get("scoring_direction", "max")
    # Mirror the client-relevant stagnation knobs so the driver can time
    # tacit-knowledge distillation (see _CACHE_FIELDS).
    for knob in ("stagnation_threshold", "stagnation_limit"):
        if live.get(knob) is not None:
            refreshed[knob] = live[knob]
    write_swarm_cache(refreshed)

    # Don't early-return when CHALLENGE.md is missing: a fresh fleet worktree
    # inherits .swarm-cache.json from the host clone (so local_challenge ==
    # new_challenge) but CHALLENGE.md is gitignored and gets left behind,
    # which would otherwise leave the LLM with an empty challenge spec.
    challenge_md = ROOT / "CHALLENGE.md"
    if new_challenge == local_challenge and challenge_md.exists():
        print(f"already in sync (active_challenge = {new_challenge}).")
        return 0

    template_files(
        server_url, challenge=new_challenge,
        algorithm_path=new_algo_path, prior=cache,
    )
    write_challenge_md(new_challenge)
    if new_challenge == local_challenge:
        print(f"refreshed CHALLENGE.md (active_challenge unchanged: {new_challenge}).")
        return 0
    print(f"\nSynced to {new_challenge} (was {local_challenge or '<none>'}).")
    print("  Your prior trajectory on this challenge (if any) will resume server-side.")
    print("  scripts/run_loop.py picks up the new CHALLENGE.md on its next iteration.")
    return 0
