"""The volume-attach race guard (2026-07-31 gpu-finl-test data loss).

A Railway deployment submitted while the /data volume attach is still
registering runs WITHOUT the mount: swarm.db goes to the container's
ephemeral disk and every row published before the next redeploy dies with
the container. Observed live: volume mkfs 09:10:14, `railway up` one second
later, 12 published experiments destroyed by the adopt redeploy at 10:24.

Pins the three layers of the guard:
  1. `_railway_add_volume` waits for the control plane to list the attach
     before returning (so `railway up` can't race it).
  2. `_railway_db_on_volume` reads the volume's file listing (tolerating the
     CLI's "> Select a volume" preamble) and distinguishes False (DB
     missing = ephemeral writes) from None (couldn't confirm).
  3. `_ensure_db_on_volume` redeploys exactly once when the DB stays
     missing, treats a late-arriving DB as success, and only warns when the
     listing is inconclusive.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hostadmin.railway as railway
import hostadmin.swarm as swarm


def _result(stdout: str = "", returncode: int = 0) -> types.SimpleNamespace:
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class FakeTime:
    """Stands in for the `time` module: sleep() advances the clock instantly,
    so the guard's real-minute polling windows run in microseconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += max(float(secs), 1.0)


VOLUME_LIST = (
    '{"volumes": [{"id": "vol-1", "mountPath": "/data",'
    ' "serviceName": "sw", "name": "sw-volume", "status": "Ready"}]}'
)
FILES_WITH_DB = (
    '> Select a volume sw-volume\n'
    '{"files": [{"name": "lost+found"}, {"name": "swarm.db"}], "remotePath": "/"}'
)
FILES_WITHOUT_DB = (
    '> Select a volume sw-volume\n'
    '{"files": [{"name": "lost+found"}], "remotePath": "/"}'
)


class FakeCli:
    """Stub for railway._railway_run: answers by subcommand, records calls."""

    def __init__(self, files_stdout: str, volume_list: str = VOLUME_LIST):
        self.files_stdout = files_stdout
        self.volume_list = volume_list
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("volume", "list"):
            return _result(self.volume_list)
        if args[:2] == ("volume", "files"):
            return _result(self.files_stdout)
        if args[:2] == ("volume", "add"):
            return _result()
        if args[0] == "redeploy":
            return _result()
        raise AssertionError(f"unexpected railway call: {args}")


def run() -> None:
    real_run = railway._railway_run
    real_rw_time, real_sw_time = railway.time, swarm.time
    railway.time = FakeTime()
    swarm.time = FakeTime()
    try:
        # ── 1. add_volume waits for the attach to be listed ──
        cli = FakeCli(FILES_WITH_DB)
        railway._railway_run = cli
        railway._railway_add_volume("sw", "/data")
        assert ("volume", "list", "--json") in cli.calls, \
            "add_volume must confirm the attach via `volume list` before returning"
        print("[ok  ] add_volume confirms the attach before returning")

        # ── 2. db_on_volume parses the listing (with CLI preamble) ──
        cli = FakeCli(FILES_WITH_DB)
        railway._railway_run = cli
        assert railway._railway_db_on_volume("sw") is True
        cli.files_stdout = FILES_WITHOUT_DB
        assert railway._railway_db_on_volume("sw") is False
        cli.volume_list = "not json"
        assert railway._railway_db_on_volume("sw") is None, \
            "unparseable listing must read as 'could not confirm', not False"
        print("[ok  ] db_on_volume: True / False / None all classified")

        # ── 3. the create-time guard ──
        real_db, real_redeploy, real_wait = (
            swarm._railway_db_on_volume, swarm._railway_redeploy,
            swarm._wait_for_server,
        )
        try:
            events: list[str] = []
            redeploys: list[str] = []
            # DB stays missing until a redeploy happens, then appears — the
            # raced-deploy shape. Must trigger exactly one redeploy.
            swarm._railway_db_on_volume = lambda name: bool(redeploys)
            swarm._railway_redeploy = lambda name: redeploys.append(name)
            swarm._wait_for_server = lambda url: True
            swarm._ensure_db_on_volume("sw", "http://x", events.append)
            assert redeploys == ["sw"], f"expected exactly one redeploy, got {redeploys}"
            assert any("after redeploy" in e for e in events), events

            # DB shows up late within the first window (eventual consistency):
            # success, no redeploy.
            redeploys.clear()
            polls = iter([False, True])
            swarm._railway_db_on_volume = lambda name: next(polls)
            swarm._ensure_db_on_volume("sw", "http://x", events.append)
            assert redeploys == [], "a late-arriving DB must not trigger a redeploy"

            # Inconclusive listing: warn, never redeploy.
            events.clear()
            swarm._railway_db_on_volume = lambda name: None
            swarm._ensure_db_on_volume("sw", "http://x", events.append)
            assert redeploys == [], "must not redeploy on an inconclusive listing"
            assert any("could not confirm" in e for e in events), events
            print("[ok  ] ensure_db_on_volume: one redeploy on missing, none on late/unknown")
        finally:
            swarm._railway_db_on_volume = real_db
            swarm._railway_redeploy = real_redeploy
            swarm._wait_for_server = real_wait
    finally:
        railway._railway_run = real_run
        railway.time, swarm.time = real_rw_time, real_sw_time

    print("\nall volume-attach race checks passed")


if __name__ == "__main__":
    run()
