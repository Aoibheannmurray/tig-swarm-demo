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
from .http import (
    _MAINNET_API,
    _mainnet_get,
    classify_http_error,
    looks_like_platform_error,
    post_json,
)
from .prompting import prompt, prompt_choice, prompt_int
from .railway import (
    _pick_workspace,
    _railway_add_volume,
    _railway_check_auth,
    _railway_check_installed,
    _railway_domain,
    _railway_get_variables,
    _railway_provision,
    _railway_set_variables,
    _railway_up,
    _wait_for_server,
)

DEFAULT_INSTANCES_PER_TRACK = 5
DEFAULT_TRACKS_PER_CHALLENGE = {
    "satisfiability": {"n_vars=100000,ratio=4150": 5},
    "vehicle_routing": {"n_nodes=600": 5},
    "knapsack": {"n_items=1000,budget=10": 5},
    "job_scheduling": {"n=50,s=flow_shop": 5},
    "energy_arbitrage": {"s=baseline": 5},
    "hypergraph": {"n_h_edges=10000": 5},
    "neuralnet_optimizer": {"n_hidden=4": 5},
    "vector_search": {"n_queries=7000": 5},
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


def _reshaped_mainnet_algo(ch: str) -> tuple[dict | None, str]:
    """Fetch challenge `ch`'s top-adoption compiled mainnet algorithm and
    reshape it into the swarm's file layout (in-memory; never touches
    initial_algorithms/). Shared by the inactive-pool and seed-pool seeders.

    Returns ``({algo_name, adoption, code_files, kernel_code}, "")`` on
    success — ``code_files`` is a ``{relpath: content}`` map with a ``mod.rs``
    entry, ``kernel_code`` is the single kernel for back-compat (None when
    there are zero or many). Returns ``(None, reason)`` on any skip/error so
    the caller can warn-and-continue."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from download_algorithm import fetch_algorithm, DownloadError
        from challenge_files import reshape_mainnet_for_swarm
    except Exception as e:
        return None, f"could not import seeding helpers: {e}"

    top = _top_mainnet_algorithm(ch)
    if top is None:
        return None, "no compiled mainnet algorithm found"
    algo_name, adoption = top
    try:
        files = fetch_algorithm(ch, algo_name)
    except DownloadError as e:
        return None, f"fetch of {algo_name} failed ({e})"

    # Keep only compilable source; drop README.md and other companions.
    code_files = {
        p: c for p, c in files.items() if p.endswith((".rs", ".cu", ".cuh"))
    }
    if "mod.rs" not in code_files:
        return None, f"upstream {algo_name} has no mod.rs entry (files={sorted(files)})"

    code_files, reshape_err = reshape_mainnet_for_swarm(ch, code_files)
    if reshape_err:
        return None, (f"mainnet '{algo_name}' does not fit the swarm format "
                      f"and could not be converted ({reshape_err})")

    cu_files = sorted(p for p in code_files if p.endswith((".cu", ".cuh")))
    kernel_code = code_files[cu_files[0]] if len(cu_files) == 1 else None
    return (
        {"algo_name": algo_name, "adoption": adoption,
         "code_files": code_files, "kernel_code": kernel_code},
        "",
    )


class AdminPostError(Exception):
    """An admin POST that exhausted its retries. `detail` is the operator-facing
    reason; `transient` says whether we gave up on a retryable condition (the
    platform never came back) rather than a hard rejection."""

    def __init__(self, detail: str, *, transient: bool):
        super().__init__(detail)
        self.detail = detail
        self.transient = transient


def _post_admin(
    url: str, payload: dict, *, timeout: int = 10, attempts: int = 5,
) -> dict:
    """POST to an admin endpoint, retrying through the deploy's rollout window.

    Every admin POST here is idempotent (seed deposits upsert by
    challenge+strategy_tag; listings are reads), so retrying is always safe.
    What we're riding out is Railway's edge answering `404 Application not
    found` while the service has no routable container — which happens AFTER
    `_wait_for_server` and `push_config_to_server` have both confirmed the
    server is up, because a rollout can replace the container underneath us.
    Backing off ~2s, 4s, 8s, 16s covers a normal rollout.

    `post_json` and `time` are looked up as module globals so the self-running
    tests can stub them."""
    delay = 2.0
    last: AdminPostError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return post_json(url, payload, timeout=timeout)
        except urllib.error.HTTPError as e:
            retryable, detail = classify_http_error(e)
            last = AdminPostError(f"HTTP {e.code}: {detail}", transient=retryable)
            if not retryable:
                raise last from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = AdminPostError(str(e), transient=True)
        if attempt < attempts:
            time.sleep(delay)
            delay = min(delay * 2, 16.0)
    raise last or AdminPostError("no attempts made", transient=True)


def seed_pool_from_mainnet(
    server_url: str, admin_key: str, challenges: set[str],
) -> list[str]:
    """Deposit each challenge's top mainnet algorithm into the SEED pool (the
    "initial pool" handed to agents on a fresh trajectory), under
    ``strategy_tag="mainnet"``, via ``POST /api/admin/seed_pool``. Multi-file
    aware (the full {relpath: content} map rides in ``algorithm_files``).

    Unlike ``seed_inactive_pool_from_mainnet`` (which loads the *inactive*
    reset pool), this makes fresh trajectories START from the mainnet
    algorithm. Idempotent — the server upserts by (challenge, strategy_tag).
    Best-effort per challenge; returns the labels that failed so the caller
    can verify/retry."""
    failed: list[str] = []
    targets = sorted(challenges)
    if not targets:
        return failed
    print(f"Seeding the seed pool from TIG mainnet ({len(targets)} challenge(s))…")
    for ch in targets:
        info, note = _reshaped_mainnet_algo(ch)
        if info is None:
            print(f"  {ch}: {note}; skipping seed.")
            continue
        code_files = info["code_files"]
        print(
            f"  {ch}: top algorithm '{info['algo_name']}' "
            f"(adoption {info['adoption'] / 1e16:.2f}%); depositing to seed pool…"
        )
        payload = {
            "admin_key": admin_key,
            "challenge": ch,
            "strategy_tag": "mainnet",
            "algorithm_code": code_files["mod.rs"],
            "algorithm_files": code_files,
            "kernel_code": info["kernel_code"],
        }
        label = f"{ch}/mainnet"
        try:
            body = _post_admin(
                f"{server_url.rstrip('/')}/api/admin/seed_pool", payload, timeout=10,
            )
            status = body.get("action") or ("added" if body.get("seeded") else "already present")
            print(f"  {label}: seed {status}")
        except AdminPostError as e:
            print(f"  {label}: seed FAILED ({e.detail}).")
            failed.append(label)
    return failed


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
    targets = sorted(challenges)
    if not targets:
        return

    for ch in targets:
        info, note = _reshaped_mainnet_algo(ch)
        if info is None:
            print(f"  {ch}: {note}; skipping seed.")
            continue
        algo_name = info["algo_name"]
        code_files = info["code_files"]
        kernel_code = info["kernel_code"]
        print(
            f"  {ch}: top algorithm '{algo_name}' "
            f"(adoption {info['adoption'] / 1e16:.2f}%); depositing to inactive pool…"
        )

        payload = {
            "admin_key": admin_key,
            "challenge": ch,
            "algorithm_code": code_files["mod.rs"],
            "algorithm_files": code_files,
            "kernel_code": kernel_code,
            "source_label": "tig-foundation",
            # Tell the server this seed IS the mainnet algorithm, so it can
            # register the challenge's baseline (the bar the dashboard shows
            # members they're clearing). Older servers ignore the extra
            # fields; the deposit is unaffected either way.
            "mainnet_algo_name": algo_name,
            "mainnet_adoption_pct": round(info["adoption"] / 1e16, 4),
        }
        try:
            body = _post_admin(
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
        except AdminPostError as e:
            print(f"  {ch}: inactive seed FAILED ({e.detail}); skipping.")


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

    `cfg["challenges"]` is a dict of {challenge: {tracks, timeout,
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
        "failed_attempts_archive": cfg.get("failed_attempts_archive", 0),
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
    """Read per-challenge starting code (broadcast to agents on a fresh
    trajectory).

    Layout (see initial_algorithms/README.md): each challenge's starting code
    lives at ``initial_algorithms/<ch>/stub/mod.rs`` — a bare placeholder for
    CPU challenges, a real working algorithm for GPU ones (and for any
    challenge where the host staged stronger code via
    ``scripts/download_algorithm.py``). Kernel code is the first ``*.cu`` in
    the same directory. The pre-restructure flat ``initial_algorithms/<ch>.rs``
    (+ ``.cu``) is kept as a read fallback so an older checkout still
    administers fine. Missing both maps to empty strings — agents start
    bare."""
    out: dict[str, dict[str, str]] = {}
    base = ROOT / "initial_algorithms"
    for ch in get_challenges():
        code, kernel = "", ""
        stub_mod = base / ch / "stub" / "mod.rs"
        legacy_rs = base / f"{ch}.rs"
        if stub_mod.is_file():
            code = stub_mod.read_text()
            cus = sorted(stub_mod.parent.glob("*.cu"))
            kernel = cus[0].read_text() if cus else ""
        elif legacy_rs.is_file():
            code = legacy_rs.read_text()
            legacy_cu = base / f"{ch}.cu"
            kernel = legacy_cu.read_text() if legacy_cu.is_file() else ""
        out[ch] = {"algorithm_code": code, "kernel_code": kernel}
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
    Idempotent — the server upserts by (challenge, strategy_tag): identical
    re-deposits are no-ops and an edited seed file replaces the pool copy on
    the next create run."""
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
            body = _post_admin(
                f"{server_url.rstrip('/')}/api/admin/seed_pool",
                payload, timeout=10,
            )
            # Newer servers report the upsert outcome; older ones only `seeded`.
            status = body.get("action") or ("added" if body.get("seeded") else "already present")
            print(f"  {label}: seed {status}")
        except AdminPostError as e:
            print(f"  {label}: seed FAILED ({e.detail}).")
            failed.append(label)
    return failed


def verify_seed_pool(
    server_url: str, admin_key: str, seeds: list[dict], *, deadline_s: int = 90,
) -> tuple[list[str], bool]:
    """Verify every authored seed is actually IN the server's seed pool,
    re-depositing missing ones until the deadline. Same belt-and-suspenders
    rationale as `push_config_to_server`: a POST during the deploy's
    health-rollout window can land on a doomed container and vanish, leaving
    the pool empty with create's output looking successful.

    Reads back via ``POST /api/admin/seeds`` (metadata listing). Returns
    ``(missing_labels, verified)``:

    - ``([], True)``  — read back clean; the pool really is populated.
    - ``(labels, True)`` — we could read the pool and those seeds aren't in it.
    - ``([], False)`` — we could not read the pool at all (a server genuinely
      predating ``/api/admin/seeds``). NOT a success: callers must say so
      rather than printing a verified message.

    The `verified` flag exists because the old `list[str]` return conflated
    "nothing missing" with "couldn't check", so an unverifiable run printed
    `all N seeds present` over an empty pool. Distinguishing a real app 404
    from the platform edge's transient 404 (see `looks_like_platform_error`)
    is the other half of that fix — the edge kind is retried, not surrendered
    to."""
    if not seeds:
        return [], True
    wanted: dict[str, set[str]] = {}
    for s in seeds:
        wanted.setdefault(s["challenge"], set()).add(s["strategy_tag"])
    # Only seeds we hold the code for can be re-deposited. Callers may also
    # pass code-less markers (e.g. {challenge, strategy_tag: "mainnet"}) purely
    # to have a deposit made elsewhere read back — those are verified, and
    # reported if absent, but never re-sent from here.
    by_label = {
        f"{s['challenge']}/{s['strategy_tag']}": s
        for s in seeds if s.get("algorithm_code")
    }

    deadline = time.time() + deadline_s
    missing: list[str] = []
    while True:
        missing = []
        read_failed = False
        for challenge, tags in wanted.items():
            try:
                body = _post_admin(
                    f"{server_url.rstrip('/')}/api/admin/seeds",
                    {"admin_key": admin_key, "challenge": challenge},
                    timeout=10,
                )
            except AdminPostError as e:
                missing.extend(f"{challenge}/{t}" for t in sorted(tags))
                read_failed = True
                if e.transient:
                    continue
                # A hard rejection won't fix itself: an old server image
                # (404 from our app) or a bad admin key (401). Stop rather
                # than hammering the same wall until the deadline.
                if "HTTP 404" in e.detail:
                    print(
                        "  note: this server predates /api/admin/seeds — "
                        "cannot verify the seed pool; check it in the Admin "
                        "Console after redeploying."
                    )
                    return [], False
                print(f"  seed pool read rejected ({e.detail}) — cannot verify.")
                return missing, False
            present = {
                s["strategy_tag"] for s in body.get("seeds", [])
                if s.get("source") == "authored"
            }
            missing.extend(f"{challenge}/{t}" for t in sorted(tags - present))
        redepositable = [by_label[m] for m in missing if m in by_label]
        if not missing or time.time() >= deadline or not redepositable:
            # `verified` claims only what we actually observed: a read we never
            # got back can't testify that the pool is fine OR that it's empty.
            return missing, not read_failed
        print(f"  seed pool incomplete ({len(missing)} missing) — re-depositing…")
        seed_pool_from_authored(server_url, admin_key, redepositable)
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
        # Per-instance wall-clock timeout: each solver/GPU instance is killed
        # at this hard deadline (see benchmark.py). Defaults come from the
        # challenge registry; not prompted in the wizard.
        timeout = ch_def.default_timeout
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
            "timeout": timeout,
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


def _resolve_swarm_credentials(
    swarm_name: str, *, resumed: bool, emit=print,
) -> tuple[str, str]:
    """The `(admin_key, swarm_password)` a create run should deploy with.

    A FRESH swarm gets new random secrets. An ADOPTED one must keep the ones
    it is already running with — rotating them leaves the data volume intact
    but locks out the host and every contributor (see the call site).

    Recovery order for an adopted swarm:

    1. Railway's own service variables — what the running server actually
       booted with. Authoritative, and survives a lost `swarm.admin.json` or a
       re-provision from a different machine.
    2. The local `swarm.admin.json`, if it names this same swarm.
    3. Give up and generate — but say so loudly. A create that fails outright
       would leave the host with no way forward; a rotation they've been
       WARNED about is recoverable with `setup.py invite`.
    """
    if not resumed:
        return secrets.token_urlsafe(16), secrets.token_urlsafe(16)

    live = _railway_get_variables(swarm_name)
    key, password = live.get("ADMIN_KEY"), live.get("SWARM_PASSWORD")
    if key and password:
        emit("  reusing this swarm's existing credentials (read from Railway) — "
             "contributor passwords and invites stay valid")
        return key, password

    admin = read_swarm_admin()
    if admin.get("swarm_name") == swarm_name:
        key = key or admin.get("admin_key")
        password = password or admin.get("swarm_password")
        if key and password:
            emit("  reusing this swarm's existing credentials (from "
                 "swarm.admin.json) — contributor passwords stay valid")
            return key, password

    emit(
        "  WARNING: adopting an existing swarm, but its credentials could not "
        "be recovered\n"
        "  from Railway or swarm.admin.json — issuing NEW ones. Every invite "
        "already\n"
        "  issued will stop working; re-invite contributors with "
        "`python setup.py invite <username>`."
    )
    return key or secrets.token_urlsafe(16), password or secrets.token_urlsafe(16)


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
    # Failed-attempts archive (0/1). Defaults off for callers that don't
    # set it (e.g. the control-ui host companion) — toggleable later from
    # the Admin Console's Settings tab.
    failed_attempts_archive = 1 if params.get("failed_attempts_archive") else 0
    # HPO knobs default in for other callers (e.g. the control-ui companion)
    # that don't set them explicitly.
    hpo_first_tune_improvements = params.get("hpo_first_tune_improvements", 10)
    hpo_min_improvements = params.get("hpo_min_improvements", 4)
    hpo_search_budget = params.get("hpo_search_budget", 13)
    hpo_num_suggested_configs = params.get("hpo_num_suggested_configs", 5)
    seed_inactive_pool = params.get("seed_inactive_pool", False)
    # Seed the SEED (initial) pool from the top mainnet algorithm too, so fresh
    # trajectories START from it — independent of seed_inactive_pool (which
    # loads the inactive reset pool). Either, both, or neither.
    seed_pool_mainnet = params.get("seed_pool_mainnet", False)
    seedable = params.get("seedable")
    if seedable is None:
        seedable = set(challenge_set.keys())

    initial_algorithms = read_initial_algorithms()

    railway_dir = ROOT / ".railway"
    if railway_dir.exists():
        emit(f"Removing existing {railway_dir.relative_to(ROOT)} from a prior run.")
        shutil.rmtree(railway_dir)

    emit("Provisioning on Railway…")
    project, service, resumed = _railway_provision(swarm_name, workspace)
    if resumed:
        emit("  (resuming a prior half-finished run — adopting existing resources)")

    # Credentials are decided AFTER provisioning, because an adopted swarm must
    # keep the ones it already has. Generating them earlier (as this used to)
    # rotated ADMIN_KEY/SWARM_PASSWORD on every re-provision of an existing
    # name: the server re-asserts env vars into its DB on every boot
    # (server/db.py), and each contributor's password is derived from the base
    # (sha256(username:base)), so a rotation silently invalidated every invite
    # ever issued. The data volume survives — only access to it was lost.
    admin_key, swarm_password = _resolve_swarm_credentials(
        swarm_name, resumed=resumed, emit=emit,
    )

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
        "FAILED_ATTEMPTS_ARCHIVE": str(failed_attempts_archive),
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
        "failed_attempts_archive": failed_attempts_archive,
        "scoring_direction": challenge_meta["scoring_direction"],
        "tracks": active_sub["tracks"],
        "timeout": active_sub["timeout"],
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

        # Mainnet deposits are verified alongside the authored ones: they land
        # in the same seed_pool table under strategy_tag "mainnet", and losing
        # them to a rollout is just as silent (fresh trajectories quietly start
        # from the stub instead of a mainnet-grade algorithm).
        mainnet_deposited: list[str] = []
        if seed_pool_mainnet:
            emit("\nSeeding the initial (seed) pool from TIG mainnet…")
            mainnet_failed = seed_pool_from_mainnet(server_url, admin_key, seedable)
            mainnet_deposited = [
                ch for ch in sorted(seedable)
                if f"{ch}/mainnet" not in set(mainnet_failed)
            ]

        authored_seeds = read_authored_seeds()
        # A mainnet seed we attempted is a pool row we expect to read back.
        to_verify = authored_seeds + [
            {"challenge": ch, "strategy_tag": "mainnet"} for ch in mainnet_deposited
        ]
        if authored_seeds:
            emit("")
            seed_pool_from_authored(server_url, admin_key, authored_seeds)
        if to_verify:
            emit("  verifying the seed pool on the server…")
            still_missing, verified = verify_seed_pool(server_url, admin_key, to_verify)
            if not verified:
                # Unverifiable is NOT success — never print a green line here.
                seeds_ok = False
                emit(
                    "  WARNING: could not read the seed pool back — the deposits "
                    "above are UNCONFIRMED. Check the Admin Console, or re-run "
                    "`python setup.py create` (idempotent) after redeploying."
                )
            elif still_missing:
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
                emit(f"  seed pool verified: all {len(to_verify)} seed(s) present "
                     f"on the server.")
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

    # Independently, seed the INITIAL (seed) pool from mainnet so fresh
    # trajectories start from the top algorithm rather than the stub/authored
    # seed. Same resolution: flag > wizard prompt.
    seed_pool_mainnet = _arg_enabled(args, "seed_pool_mainnet")
    if seedable and not seed_pool_mainnet and not yes:
        ans = prompt(
            f"Seed the INITIAL pool (fresh-trajectory start) with the current "
            f"top-earning TIG mainnet algorithm for "
            f"{', '.join(sorted(seedable))}? [y/N]",
            default="N",
        )
        seed_pool_mainnet = ans.strip().lower() in ("y", "yes")
    if seed_pool_mainnet and not seedable:
        print(
            "  --seed-pool-mainnet requested but this swarm has no "
            "challenges; nothing to seed."
        )
        seed_pool_mainnet = False

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
        failed_attempts_archive = 1 if _arg_enabled(args, "failed_attempts_archive") else 0
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
        if _arg_enabled(args, "failed_attempts_archive"):
            failed_attempts_archive = 1
        else:
            ans = prompt(
                "Store failed attempts in the server DB? Agents' failure "
                "retrospectives + distilled lessons are archived per-agent and "
                "served back as a stagnation hint (\"you tried this before\"), "
                "instead of appended to local tacit_knowledge files. "
                "Toggleable later in the Admin Console. [y/N]",
                default="N",
            )
            failed_attempts_archive = 1 if ans.strip().lower() in ("y", "yes") else 0
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
        "failed_attempts_archive": failed_attempts_archive,
        "hpo_first_tune_improvements": hpo_first_tune_improvements,
        "hpo_min_improvements": hpo_min_improvements,
        "hpo_search_budget": hpo_search_budget,
        "hpo_num_suggested_configs": hpo_num_suggested_configs,
        "seed_inactive_pool": seed_inactive_pool,
        "seed_pool_mainnet": seed_pool_mainnet,
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
    Skipped if a fleet.config.json already exists — never clobbers user edits.

    `swarm_password` is the swarm's BASE password, which never authenticates on
    its own: the server compares against sha256("<username>:<base>"). So the
    host is self-invited here exactly as `setup.py invite` would do it —
    deriving their own password and recording the name in issued_contributors.
    (Writing the base password with no username, as this used to, produced a
    config that run_fleet.py rejects outright for the missing username, and
    that the server would reject anyway.)
    """
    from .contributors import derive_password, record_issued

    path = ROOT / "fleet.config.json"
    if path.exists():
        print(f"  fleet.config.json already present — leaving as-is")
        return
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "host"
    starter = {
        "server_url": server_url,
        "username": username,
        "swarm_password": derive_password(username, swarm_password),
        "agents": [
            {
                "name": f"{username}-1",
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "api_key_env": "ANTHROPIC_API_KEY",
                # C3 needs no local Docker/Rust — see build_fleet_config.
                "compute": "c3",
                "hardware": "auto",
            }
        ],
    }
    path.write_text(json.dumps(starter, indent=2) + "\n")
    record_issued(read_swarm_admin(), username)
    print(f"  scaffolded {path.relative_to(ROOT)} as {username!r} (one agent — edit before launching)")


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
        # Only mirror a timeout the server actually sent: a server running
        # pre-timeout code omits it, and benchmark.py's own 30s fallback is
        # the right behavior then — NOT the legacy 5s that would silently
        # starve every solver.
        if sub.get("timeout") is not None:
            refreshed["timeout"] = sub["timeout"]
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
        # Only mirror a timeout the server actually sent: a server running
        # pre-timeout code omits it, and benchmark.py's own 30s fallback is
        # the right behavior then — NOT the legacy 5s that would silently
        # starve every solver.
        if sub.get("timeout") is not None:
            refreshed["timeout"] = sub["timeout"]
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
