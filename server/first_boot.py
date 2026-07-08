"""First-boot seeding from a bundled algorithm snapshot (onboarding P5).

Enables a **browser-only host deploy** ("Deploy on Railway"): with no host
clone to run `setup.py create`, the server can still come up with real
starting code + a seed pool by reading algorithm sources baked into its image.

Runs once, under init_db's first-boot sentinel (same gate as
`_apply_env_swarm_config`), and is safe to run on every deploy:

  * `initial_algorithm_code` is only filled when empty — so a swarm already
    configured via the env blob (`setup.py create`) is never overwritten.
  * seed-pool inserts dedupe by (challenge, strategy_tag, source) — so a later
    `setup.py create` POST can't pile up duplicates.

Absent the bundle (image built without `initial_algorithms/`), this is a
no-op, so nothing changes for deploys that don't ship it.
"""

from __future__ import annotations

import os
from pathlib import Path

import challenges as _challenges


def _bundle_dir() -> Path | None:
    """Locate the bundled initial-algorithms snapshot, or None when absent."""
    candidates = [
        os.environ.get("TIG_INITIAL_ALGORITHMS_DIR"),
        "/app/initial_algorithms",                      # server image WORKDIR
        str(Path(__file__).resolve().parent.parent / "initial_algorithms"),  # repo
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None


async def seed_from_bundle(db) -> dict:
    """Populate initial algorithm code + the authored seed pool from the
    bundled snapshot. Returns a small summary for logging. Never raises for
    a missing/partial bundle — a browser deploy must still boot."""
    import db as _db  # local import: first_boot is imported by db at boot

    bundle = _bundle_dir()
    if bundle is None:
        return {"bundle": None, "initial_code": 0, "seeds": 0}

    initial_filled = 0
    seeds_added = 0

    for challenge in _challenges.CHALLENGE_NAMES:
        # 1) Initial algorithm (the stub every agent starts from) — fill only
        #    when the challenge has no code yet, so we never clobber a
        #    create-provisioned or admin-edited stub.
        rs = bundle / f"{challenge}.rs"
        cu = bundle / f"{challenge}.cu"
        if rs.is_file():
            existing = await _db.get_challenge_config(db, challenge)
            if not (existing and (existing.get("initial_algorithm_code") or "").strip()):
                try:
                    await _db.upsert_challenge_config(
                        db, challenge,
                        initial_algorithm_code=rs.read_text(encoding="utf-8"),
                        initial_kernel_code=(cu.read_text(encoding="utf-8")
                                             if cu.is_file() else None),
                    )
                    initial_filled += 1
                except OSError:
                    pass

        # 2) Authored seed pool — one seed per `<ch>/seeds/<tag>.rs`
        #    (strategy_tag = filename stem). The old (challenge, strategy_tag,
        #    source) UNIQUE index was dropped (diversity is now similarity-based),
        #    so insert_seed no longer dedupes — we guard here to stay idempotent
        #    across re-boots and alongside a later `setup.py create` POST.
        seeds_dir = bundle / challenge / "seeds"
        if seeds_dir.is_dir():
            for seed_rs in sorted(seeds_dir.glob("*.rs")):
                if await _authored_seed_exists(db, challenge, seed_rs.stem):
                    continue
                seed_cu = seed_rs.with_suffix(".cu")
                try:
                    await _db.insert_seed(
                        db, challenge, seed_rs.stem,
                        seed_rs.read_text(encoding="utf-8"),
                        created_at=_timestamp(),
                        source="authored", feasible=True,
                        kernel_code=(seed_cu.read_text(encoding="utf-8")
                                     if seed_cu.is_file() else None),
                    )
                    seeds_added += 1
                except OSError:
                    pass

    return {"bundle": str(bundle), "initial_code": initial_filled, "seeds": seeds_added}


async def _authored_seed_exists(db, challenge: str, strategy_tag: str) -> bool:
    """True if an authored seed with this (challenge, strategy_tag) is already
    in the pool — the idempotency guard (the DB no longer enforces it)."""
    cursor = await db.execute(
        "SELECT 1 FROM seed_pool "
        "WHERE challenge = ? AND strategy_tag = ? AND source = 'authored' LIMIT 1",
        (challenge, strategy_tag),
    )
    return await cursor.fetchone() is not None


def _timestamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
