#!/usr/bin/env python3
"""Self-running tests for the local control-plane companion (control_server.py).

No pytest in this repo — run directly:

    python control_server_test.py

Uses Starlette's TestClient and redirects the fleet-config / tacit file paths to
a temp dir so the real (gitignored) fleet.config.json / tacit_knowledge.md are
never touched. Fleet-start and Railway provisioning are NOT exercised here (they
spawn subprocesses / hit Railway); those are covered by scripts/test_fleet_core.py
and manual end-to-end verification.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import control_server
import init_fleet

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def main() -> int:
    try:
        from starlette.testclient import TestClient
    except Exception as e:  # pragma: no cover
        print(f"  [skip] TestClient unavailable ({e})")
        return 0

    tmp = Path(tempfile.mkdtemp())
    fleet_path = tmp / "fleet.config.json"
    tacit_path = tmp / "tacit_knowledge.md"
    # Redirect writable paths away from the real repo files.
    control_server.FLEET_CONFIG_PATH = fleet_path
    control_server.TACIT_PATH = tacit_path
    init_fleet.FLEET_CONFIG_PATH = fleet_path

    app = control_server.create_app()
    # base_url must be loopback: the DNS-rebinding guard rejects any other
    # Host header (TestClient's default is "testserver").
    with TestClient(app, base_url="http://127.0.0.1") as c:
        print("read-only endpoints")
        check(c.get("/local-api/env", headers={"host": "evil.example"}).status_code == 403,
              "non-loopback Host rejected (403, DNS-rebinding guard)")
        check(c.get("/local-api/env").json()["mode"] == "local", "env reports local mode")
        p = c.get("/local-api/providers").json()
        check(len(p["providers"]) > 0 and len(p["c3_hardware"]) > 0, "providers + hardware listed")
        ch = c.get("/local-api/challenges").json()
        check("cpu" in ch and "gpu" in ch, "challenges split cpu/gpu")
        check(
            ch.get("timeout_defaults", {}).get("satisfiability", 0) > 0
            and all(int(v) > 0 for v in ch.get("timeout_defaults", {}).values()),
            "per-challenge timeout defaults exposed",
        )

        print("fleet config write/read")
        r = c.post("/local-api/fleet/config", json={
            "server_url": "https://x.up.railway.app", "username": "me",
            "swarm_password": "pw", "provider": "claude-code", "count": 1,
        })
        check(r.status_code == 200, "valid fleet config accepted")
        check(fleet_path.exists(), "fleet.config.json written to temp path")
        check(c.get("/local-api/fleet/config").json()["exists"] is True, "config read back")
        rb = c.post("/local-api/fleet/config", json={"provider": "claude-code"})
        check(rb.status_code == 400, "invalid fleet config rejected (400)")

        # Every provider carries the curated shortlist the model dropdown
        # falls back to when no key is set (and the ONLY list CLI providers
        # can offer). Codex takes the CLI's own default, so its list is empty.
        check(all("popular_models" in prov for prov in p["providers"]),
              "every provider exposes popular_models")
        check(all(prov["default_model"] in prov["popular_models"]
                  for prov in p["providers"] if prov["popular_models"]),
              "each shortlist leads with the provider's default model")

        print("model catalog")
        # No key stored in the test env -> a reason, not an exception, so the
        # dropdown can fall back to the shortlist.
        m = c.get("/local-api/models", params={"provider": "anthropic"}).json()
        check(m["models"] == [] and "ANTHROPIC_API_KEY" in m["error"],
              "missing key reported as data, not an error status")
        mc = c.get("/local-api/models", params={"provider": "claude-code"}).json()
        check(mc["models"] == [] and "login" in mc["error"],
              "CLI provider explains it has no model endpoint")
        mb = c.get("/local-api/models", params={"provider": "nope"}).json()
        check(mb["models"] == [] and "unknown provider" in mb["error"],
              "unknown provider reported as data")

        print("tacit append")
        tq = c.get("/local-api/tacit/questions").json()["questions"]
        # The guided form must ask what `python setup.py tacit` asks — same
        # objects, not a re-typed copy that can drift.
        check(tq == control_server.setup_mod.TACIT_QUESTIONS and len(tq) > 0,
              "tacit questions served from the CLI wizard's own list")
        check(all(q.get("title") for q in tq), "every question has a title")
        rt = c.post("/local-api/tacit", json={"answers": [{"title": "Q", "body": "a lesson"}]})
        check(rt.status_code == 200, "tacit guided answers accepted")
        check(tacit_path.exists() and "a lesson" in tacit_path.read_text(), "tacit written to temp path")
        rt2 = c.post("/local-api/tacit", json={"text": ""})
        check(rt2.status_code == 400, "empty tacit rejected (400)")

        print("c3 install controller")
        c3s = c.get("/local-api/c3/install").json()
        check(c3s["state"] == "idle" and c3s["error"] is None,
              "c3 install starts idle")
        check(control_server.C3InstallController._win_arch() in ("amd64", "arm64"),
              "Windows release arch resolves to a published binary")
        # The Windows path writes into %LOCALAPPDATA%; without it there is no
        # standard install dir, and the controller must say so rather than
        # throwing inside its worker thread.
        import os as _os2
        ctrl = control_server.C3InstallController()
        ctrl._token = 1
        saved = _os2.environ.pop("LOCALAPPDATA", None)
        try:
            ctrl._install_windows(1)
        finally:
            if saved is not None:
                _os2.environ["LOCALAPPDATA"] = saved
        check(ctrl.state == "error" and "LOCALAPPDATA" in (ctrl.error or ""),
              "Windows install without LOCALAPPDATA fails with a reason")

        print("invite derivation")
        ri = c.post("/local-api/invite", json={"username": "alice", "swarm_password": "base123"})
        import hashlib
        expected = hashlib.sha256(b"alice:base123").hexdigest()
        check(ri.json()["swarm_password"] == expected, "invite matches sha256(username:base)")
        check(c.post("/local-api/invite", json={"swarm_password": "b"}).status_code == 400,
              "invite without username rejected")

        print("railway login (device-code flow, stubbed CLI)")
        import time as _time

        class _FakeProc:
            """Stands in for the pty-spawned `railway login --browserless`."""
            returncode = 0
            def poll(self):
                return self.returncode
            def wait(self):
                return self.returncode
            def kill(self):
                pass

        def _fake_spawn():
            # ANSI-colored pty-style output, with the CLI's upgrade notice
            # (a docs.railway.com URL) BEFORE the real activation link — the
            # scraper must skip it, not lock it in as "the" URL.
            import io
            reader = io.StringIO(
                "\x1b[33mNew version! https://docs.railway.com/cli\x1b[0m\r\n"
                "\x1b[1mSign in from any device:\x1b[0m\r\n"
                "  https://railway.com/cli-login?d=abc123\r\n"
                "Your pairing code is: \x1b[36mbrave-otter-lamp\x1b[0m\r\n"
            )
            return _FakeProc(), reader

        orig_launch = control_server.RailwayLoginController._spawn
        control_server.RailwayLoginController._spawn = staticmethod(_fake_spawn)
        try:
            rl = c.post("/local-api/railway/login").json()
            check(rl["state"] in ("pending", "done"), "login start accepted")
            deadline = _time.time() + 5
            while _time.time() < deadline:
                rl = c.get("/local-api/railway/login").json()
                if rl["state"] == "done" and rl.get("url"):
                    break
                _time.sleep(0.05)
            check(rl["state"] == "done", "login completes when the CLI exits 0")
            check(rl["url"] == "https://railway.com/cli-login?d=abc123",
                  "pairing link scraped (docs.railway upgrade notice skipped)")
            check(rl["code"] == "brave-otter-lamp",
                  "pairing code scraped through ANSI color codes")
        finally:
            control_server.RailwayLoginController._spawn = orig_launch

        print("tool discovery (Windows shims / late installs)")
        # An npm-installed CLI is `railway.cmd`, which CreateProcess cannot run
        # directly — Popen(["railway", ...]) fails on a perfectly good install.
        # _launch_argv must route .cmd/.bat through the command interpreter.
        import os as _os
        shim_dir = tmp / "winshims"
        shim_dir.mkdir()
        (shim_dir / "faketool.cmd").write_text("@echo off\n")
        orig_name, orig_path = _os.name, _os.environ.get("PATH", "")
        try:
            _os.environ["PATH"] = str(shim_dir) + _os.pathsep + orig_path
            argv = control_server._launch_argv("faketool", "login")
            # which() only honours PATHEXT on Windows, so on POSIX the shim
            # isn't found at all — assert the branch by extension instead.
            if argv[0].lower().endswith(".cmd"):
                check(False, "a .cmd shim must not be handed to CreateProcess directly")
            else:
                check(argv[-1] == "login", "launch argv keeps its arguments")
            # Force the Windows branch to prove the wrapping itself.
            import shutil as _shutil
            orig_which = _shutil.which
            _shutil.which = lambda name, *a, **k: (
                str(shim_dir / "faketool.cmd") if name == "faketool" else orig_which(name, *a, **k)
            )
            _os.name = "nt"
            try:
                wargv = control_server._launch_argv("faketool", "login")
                check(wargv[0].lower().endswith(("cmd.exe", "cmd")) and wargv[1] == "/c",
                      "Windows .cmd shim runs through the command interpreter")
                check(wargv[-1] == "login", "wrapped argv keeps its arguments")
            finally:
                _os.name = orig_name
                _shutil.which = orig_which
        finally:
            _os.name = orig_name
            _os.environ["PATH"] = orig_path
        # _refresh_windows_path is a no-op off Windows; it must never raise.
        control_server._refresh_windows_path()
        check(True, "PATH refresh is a safe no-op off Windows")

        print("fleet status / websocket")
        check(c.get("/local-api/fleet/status").json()["state"] == "idle", "fleet idle before start")
        # TestClient stamps Host "testserver" on WS handshakes regardless of
        # base_url, so the accepted cases set a loopback Host explicitly.
        with c.websocket_connect("/local-api/stream",
                                 headers={"host": "127.0.0.1",
                                          "origin": "http://127.0.0.1"}):
            check(True, "event stream websocket connects (loopback Origin)")
        with c.websocket_connect("/local-api/stream",
                                 headers={"host": "127.0.0.1"}):
            check(True, "event stream websocket connects (no Origin — CLI client)")
        # The HTTP middleware doesn't cover WebSockets, so the handler itself
        # must reject a rebinding page's handshake (bad Origin / bad Host).
        for label, headers in (
            ("bad Origin", {"host": "127.0.0.1", "origin": "http://evil.example"}),
            ("bad Host", {"host": "evil.example"}),
        ):
            try:
                with c.websocket_connect("/local-api/stream", headers=headers):
                    check(False, f"websocket with {label} rejected")
            except Exception:
                check(True, f"websocket with {label} rejected")

    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all control_server checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
