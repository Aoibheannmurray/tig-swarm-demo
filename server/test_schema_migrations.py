#!/usr/bin/env python3
"""Self-running tests for the versioned schema migrations.

No pytest in this repo (see server/CLAUDE.md) — run directly:

    python server/test_schema_migrations.py

init_db used to re-run ~24 idempotent statements on every boot, with no record
of what had run, no ordering guarantee, and no way to distinguish a fully
migrated database from a half-migrated one. Each is now a numbered Migration
recorded in `schema_version`.

The risk this covers is upgrade, not greenfield: a live swarm's DB predates
versioning, so the first boot after deploy runs every migration against a
populated database. That must be a no-op for data.

Covers:
  - a fresh DB applies everything and stamps to head
  - a second boot applies nothing (the steady state)
  - adopting an UNSTAMPED populated DB re-runs every migration without
    touching rows, and the data-fix migration still does its job
  - ordering: the trajectory_bests rebuild runs after the columns it copies
  - _validate_migrations rejects gaps and duplicates
  - _verify_schema refuses to serve a half-applied schema
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import db as dbmod

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


class _TempDB:
    """Point db.DB_PATH at a throwaway file."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = dbmod.DB_PATH
        dbmod.DB_PATH = Path(self._tmp.name) / "swarm.db"
        return dbmod.DB_PATH

    def __exit__(self, *exc) -> None:
        dbmod.DB_PATH = self._orig
        self._tmp.cleanup()


def _versions(path: Path) -> list[int]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute(
            "SELECT version FROM schema_version ORDER BY version")]
    finally:
        con.close()


def test_fresh_database() -> None:
    print("fresh database")
    with _TempDB() as path:
        asyncio.run(dbmod.init_db())
        got = _versions(path)
        expected = [m.version for m in dbmod.MIGRATIONS]
        check(got == expected, f"every migration recorded ({len(got)}/{len(expected)})")
        check(got == sorted(set(got)), "versions unique and ordered")


def test_second_boot_is_a_noop() -> None:
    print("second boot")
    with _TempDB() as path:
        asyncio.run(dbmod.init_db())
        before = _versions(path)

        async def again():
            async with dbmod.aiosqlite.connect(dbmod.DB_PATH) as conn:
                return await dbmod._apply_migrations(conn)

        applied = asyncio.run(again())
        check(applied == [], f"nothing re-applied (got {applied})")
        check(_versions(path) == before, "schema_version unchanged")


def test_adopting_an_unstamped_populated_db() -> None:
    """The upgrade path: a live swarm's DB has the columns (the old boot-time
    statements added them) but no schema_version. Dropping the table reproduces
    that shape exactly, and re-running must not disturb data."""
    print("adopting an unstamped, populated DB")
    with _TempDB() as path:
        asyncio.run(dbmod.init_db())

        con = sqlite3.connect(path)
        con.execute(
            "INSERT INTO agents (id, name, registered_at, last_heartbeat, status)"
            " VALUES ('a1', 'legacy-agent', '2026-01-01', '2026-01-01', 'active')")
        # Two authored seeds sharing (challenge, strategy_tag) — migration 23
        # keeps only the newest. A harvested seed must survive untouched.
        for i in (1, 2):
            con.execute(
                "INSERT INTO seed_pool (challenge, strategy_tag, source,"
                " algorithm_code, score, created_at) VALUES"
                " ('knapsack', 'greedy', 'authored', ?, 1.0, ?)",
                (f"code-v{i}", f"2026-01-0{i}"))
        con.execute(
            "INSERT INTO seed_pool (challenge, strategy_tag, source,"
            " algorithm_code, score, created_at) VALUES"
            " ('knapsack', 'dp', 'harvested', 'harvest', 2.0, '2026-01-01')")
        con.execute("DROP TABLE schema_version")   # <- now it looks pre-versioning
        con.commit()
        con.close()

        asyncio.run(dbmod.init_db())

        con = sqlite3.connect(path)
        try:
            agents = con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            authored = con.execute(
                "SELECT algorithm_code FROM seed_pool WHERE source='authored'"
            ).fetchall()
            harvested = con.execute(
                "SELECT COUNT(*) FROM seed_pool WHERE source='harvested'"
            ).fetchone()[0]
        finally:
            con.close()

        check(agents == 1, "existing agent row survived")
        check(harvested == 1, "harvested seed untouched")
        check(len(authored) == 1 and authored[0][0] == "code-v2",
              f"duplicate authored seeds collapsed to the newest (got {authored})")
        check(_versions(path) == [m.version for m in dbmod.MIGRATIONS],
              "re-stamped to head")


def test_rebuild_runs_after_the_columns_it_copies() -> None:
    """The trajectory_bests rebuild copies hyperparameters/algorithm_files. If
    it ran before the migrations that add them, the first boot after upgrade
    would silently drop both columns."""
    print("migration ordering")
    by_name = {m.name: m.version for m in dbmod.MIGRATIONS}
    rebuild = by_name["trajectory_bests.experiment_id nullable"]
    check(rebuild > by_name["trajectory_bests.hyperparameters"],
          "rebuild runs after trajectory_bests.hyperparameters")
    check(rebuild > by_name["trajectory_bests.algorithm_files"],
          "rebuild runs after trajectory_bests.algorithm_files")
    check(rebuild == max(m.version for m in dbmod.MIGRATIONS),
          "rebuild is last")

    with _TempDB() as path:
        asyncio.run(dbmod.init_db())
        con = sqlite3.connect(path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(trajectory_bests)")}
            notnull = {r[1]: r[3] for r in con.execute(
                "PRAGMA table_info(trajectory_bests)")}
        finally:
            con.close()
        check({"hyperparameters", "algorithm_files"} <= cols,
              "rebuild preserved the columns added before it")
        check(not notnull.get("experiment_id"), "experiment_id is nullable after rebuild")


def test_numbering_is_validated() -> None:
    print("migration numbering")
    original = dbmod.MIGRATIONS
    try:
        dbmod.MIGRATIONS = original + (
            dbmod.Migration(len(original) + 2, "gap", lambda db: None),)
        try:
            dbmod._validate_migrations()
            check(False, "a numbering gap is rejected")
        except ValueError:
            check(True, "a numbering gap is rejected")

        dbmod.MIGRATIONS = original + (
            dbmod.Migration(1, "dupe", lambda db: None),)
        try:
            dbmod._validate_migrations()
            check(False, "a duplicate version is rejected")
        except ValueError:
            check(True, "a duplicate version is rejected")
    finally:
        dbmod.MIGRATIONS = original
        dbmod._validate_migrations()


def test_half_applied_schema_is_refused() -> None:
    print("schema verification")
    with _TempDB():
        asyncio.run(dbmod.init_db())

        async def verify_missing():
            async with dbmod.aiosqlite.connect(dbmod.DB_PATH) as conn:
                orig = dbmod._EXPECTED_COLUMNS
                dbmod._EXPECTED_COLUMNS = orig + (("agents", "definitely_absent"),)
                try:
                    await dbmod._verify_schema(conn)
                    return None
                except RuntimeError as e:
                    return str(e)
                finally:
                    dbmod._EXPECTED_COLUMNS = orig

        msg = asyncio.run(verify_missing())
        check(msg is not None, "a missing column raises")
        check(msg is not None and "agents.definitely_absent" in msg,
              "the error names the missing column")


def main() -> int:
    test_fresh_database()
    test_second_boot_is_a_noop()
    test_adopting_an_unstamped_populated_db()
    test_rebuild_runs_after_the_columns_it_copies()
    test_numbering_is_validated()
    test_half_applied_schema_is_refused()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all schema-migration checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
