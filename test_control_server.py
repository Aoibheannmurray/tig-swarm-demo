#!/usr/bin/env python3
"""Self-running tests for the local control-plane companion (control_server.py).

No pytest in this repo — run directly:

    python test_control_server.py

Uses Starlette's TestClient and redirects the fleet-config / tacit file paths to
a temp dir so the real (gitignored) fleet.config.json / tacit_knowledge.md are
never touched. Fleet-start and Railway provisioning are NOT exercised here (they
spawn subprocesses / hit Railway); those are covered by scripts/test_fleet_core.py
and manual end-to-end verification.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import control_server
import init_fleet
import llm_backends

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
        # falls back to when no key/catalog is available. Codex normally
        # replaces this at runtime with the installed CLI's live catalog.
        check(all("popular_models" in prov for prov in p["providers"]),
              "every provider exposes popular_models")
        check(all(prov["default_model"] in prov["popular_models"]
                  for prov in p["providers"] if prov["popular_models"]),
              "each shortlist leads with the provider's default model")

        pf = c.get("/local-api/preflight").json()
        # node/npm decide what the Windows Railway step looks like: the host
        # page shows the "install Node first" command only when npm is missing.
        check({"claude", "codex", "node", "npm"} <= set(pf["clis"]),
              "preflight reports node + npm alongside the coding CLIs")
        check(all(isinstance(v, bool) for v in pf["clis"].values()),
              "every CLI probe is a plain bool the UI can branch on")

        print("model catalog")
        # No key stored in the test env -> a reason, not an exception, so the
        # dropdown can fall back to the shortlist.
        m = c.get("/local-api/models", params={"provider": "anthropic"}).json()
        check(m["models"] == [] and "ANTHROPIC_API_KEY" in m["error"],
              "missing key reported as data, not an error status")
        mc = c.get("/local-api/models", params={"provider": "claude-code"}).json()
        check(mc["models"] == [] and "login" in mc["error"],
              "CLI provider explains it has no model endpoint")
        # Keep this deterministic: exercise the control-plane integration with
        # a fake refreshed Codex result instead of depending on login/network.
        original_list_models = llm_backends.list_models
        try:
            llm_backends.list_models = lambda provider, **_: (
                ["gpt-next", "gpt-current"] if provider == "codex-agentic" else []
            )
            control_server._MODELS_CACHE.pop("codex-agentic", None)
            mcd = c.get("/local-api/models", params={"provider": "codex-agentic"}).json()
        finally:
            llm_backends.list_models = original_list_models
            control_server._MODELS_CACHE.pop("codex-agentic", None)
        check(mcd == {"models": ["gpt-next", "gpt-current"], "error": None},
              "Codex CLI live catalog is exposed to the dropdown")
        mb = c.get("/local-api/models", params={"provider": "nope"}).json()
        check(mb["models"] == [] and "unknown provider" in mb["error"],
              "unknown provider reported as data")

        catalog = json.dumps({"models": [
            {"slug": "hidden", "visibility": "hide", "priority": 0},
            {"slug": "gpt-later", "visibility": "list", "priority": 9},
            {"slug": "gpt-latest", "visibility": "list", "priority": 1},
            {"slug": "gpt-latest", "visibility": "list", "priority": 2},
        ]})
        check(llm_backends._parse_codex_model_catalog(catalog)
              == ["gpt-latest", "gpt-later"],
              "Codex catalog filters hidden models, orders priority, and deduplicates")

        # A rejected key is reported at the Provider step; the wizard shows the
        # provider's sentence, not four lines of JSON around it.
        readable = control_server._readable_api_error
        check(readable(RuntimeError(
            'HTTP 401: {"type":"error","error":{"type":"authentication_error",'
            '"message":"invalid x-api-key"},"request_id":"req_011"}'
        )) == "HTTP 401: invalid x-api-key",
              "provider error keeps the status and drops the JSON envelope")
        check(readable(OSError("[Errno -3] Temporary failure in name resolution"))
              == "[Errno -3] Temporary failure in name resolution",
              "non-JSON errors pass through unchanged")
        check(len(readable(RuntimeError("HTTP 400: " + "y" * 900))) <= 301,
              "an enormous upstream body is capped")

        print("tacit append")
        tq = c.get("/local-api/tacit/questions").json()["questions"]
        # The guided form must ask what `python setup.py tacit` asks — same
        # objects, not a re-typed copy that can drift.
        check(tq == control_server.hostadmin.TACIT_QUESTIONS and len(tq) > 0,
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
        # POSIX only. On Windows the contributor page shows the PowerShell
        # commands and never calls this — start() must say so rather than
        # shelling out to a `bash` that isn't there.
        import os as _os2
        ctrl = control_server.C3InstallController()
        saved_name = _os2.name
        try:
            _os2.name = "nt"
            st = ctrl.start()
        finally:
            _os2.name = saved_name
        check(st["state"] == "error" and "PowerShell" in (st["error"] or ""),
              "Windows install refers to the copy commands instead of running one")

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

        # Capture must not depend on the CLI printing the literal word "code":
        # a reworded notice used to leave `code` None forever, so the UI showed
        # a login that never completed. It also must never mistake `cli-login`
        # inside the sign-in URL for the pairing code — that hyphenated token
        # is exactly what the loose pattern would otherwise grab first.
        _C = control_server.RailwayLoginController

        def _scrape(*lines):
            code = None
            for raw in lines:
                line = _C._ANSI_RE.sub("", raw).replace("\r", "")
                if code is None:
                    m = _C._CODE_LABELLED_RE.search(line)
                    if m is None and "http" not in line:
                        m = _C._CODE_RE.search(line)
                    if m:
                        code = m.group(1)
            return code

        check(_scrape("  https://railway.com/cli-login?d=abc123\r\n") is None,
              "a sign-in URL alone yields no pairing code")
        check(_scrape("  https://railway.com/cli-login?d=abc123\r\n",
                      "Your pairing phrase: brave-otter-lamp\r\n")
              == "brave-otter-lamp",
              "pairing code still scraped when the notice omits the word 'code'")
        check(_scrape("Your pairing code is: \x1b[36mbrave-otter-lamp\x1b[0m\r\n")
              == "brave-otter-lamp",
              "labelled form still wins")

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

        print("custom provider (self-hosted LLM endpoint)")
        import llm_backends as _llm
        import secrets_local as _secrets
        orig_list, orig_sresolve = _llm.list_models, _secrets.resolve
        seen: list[dict] = []

        def _fake_list(provider, api_key=None, api_base=None):
            seen.append({"provider": provider, "api_key": api_key, "api_base": api_base})
            return ["Qwen3-Coder-Next-Q8_0", "gpt-oss-120b"]

        try:
            _llm.list_models = _fake_list
            _secrets.resolve = lambda name, *a, **k: "local-token" if name == "xxxx" else ""

            # No URL yet: say what to do, don't call anything.
            res = control_server._live_models("custom")
            check(res["models"] == [] and "endpoint URL" in (res["error"] or ""),
                  "custom without an api_base asks for one")
            check(not seen, "custom without an api_base makes no request")

            res = control_server._live_models(
                "custom", api_base="http://127.0.0.1:8000/v1", api_key_env="xxxx")
            check(res["error"] is None and len(res["models"]) == 2,
                  "custom lists the models its endpoint serves")
            check(seen[-1] == {"provider": "openai", "api_key": "local-token",
                               "api_base": "http://127.0.0.1:8000/v1"},
                  "custom calls the given endpoint as an OpenAI-compatible one")

            # No key configured is the normal case for a local server.
            _secrets.resolve = lambda name, *a, **k: ""
            control_server._live_models("custom", api_base="http://127.0.0.1:8000/v1")
            check(seen[-1]["api_key"] is None, "custom lists models without a key")

            # Unreachable server: a reason and a hint, never an exception.
            def _boom(*a, **k):
                raise OSError("Connection refused")
            _llm.list_models = _boom
            res = control_server._live_models("custom", api_base="http://127.0.0.1:8000/v1")
            check(res["models"] == [] and "http://127.0.0.1:8000/v1" in (res["error"] or ""),
                  "unreachable endpoint reports the URL it tried")

            # The endpoint args must not leak into the vendor providers.
            _llm.list_models = _fake_list
            seen.clear()
            res = control_server._live_models(
                "anthropic", api_base="http://127.0.0.1:8000/v1")
            check(not seen and res["models"] == [],
                  "a vendor provider ignores an api_base it was handed")
        finally:
            _llm.list_models, _secrets.resolve = orig_list, orig_sresolve

        print("browser launch (WSL / headless)")
        import io as _io
        import contextlib as _contextlib
        import shutil as _shutil
        import subprocess as _subprocess
        orig_which, orig_run = _shutil.which, _subprocess.run
        orig_environ = dict(_os.environ)

        def _fake_platform(available: set[str], rc: dict[str, int]):
            """Stub which()/run() so only `available` tools exist, each exiting
            with the rc given. Returns the list that records what was run."""
            calls: list[list[str]] = []
            _shutil.which = lambda name, *a, **k: (
                f"/fake/{name}" if name in available else None
            )

            def _run(argv, *a, **k):
                calls.append(list(argv))
                return _subprocess.CompletedProcess(argv, rc.get(argv[0], 0))

            _subprocess.run = _run
            return calls

        try:
            for var in ("WSL_DISTRO_NAME", "WSL_INTEROP", "DISPLAY", "WAYLAND_DISPLAY"):
                _os.environ.pop(var, None)
            _os.environ["WSL_DISTRO_NAME"] = "Ubuntu"
            check(control_server._is_wsl(), "WSL detected from WSL_DISTRO_NAME")

            # wslu installed: the URL goes to wslview and nothing else is tried.
            calls = _fake_platform({"wslview", "cmd.exe", "explorer.exe"}, {})
            check(control_server._open_browser("http://127.0.0.1:8787/"),
                  "WSL: opens via wslview")
            check(len(calls) == 1 and calls[0][0] == "wslview",
                  "WSL: wslview is preferred and stops the chain")

            # No wslu — falls through to the Windows shell openers.
            calls = _fake_platform({"cmd.exe", "explorer.exe"}, {})
            check(control_server._open_browser("http://127.0.0.1:8787/"),
                  "WSL: falls back to cmd.exe when wslview is absent")
            check(calls[-1][0] == "cmd.exe" and "start" in calls[-1],
                  "WSL: cmd.exe start carries the URL")

            # explorer.exe is the last resort AND exits 1 on success — the
            # launch must still count, or the user gets a bogus warning.
            calls = _fake_platform({"explorer.exe"}, {"explorer.exe": 1})
            check(control_server._open_browser("http://127.0.0.1:8787/"),
                  "WSL: explorer.exe exit code 1 still counts as opened")

            # Interop disabled: no opener exists, so report failure rather than
            # claiming a tab was opened.
            calls = _fake_platform(set(), {})
            check(not control_server._open_browser("http://127.0.0.1:8787/"),
                  "WSL: no opener available reports failure")

            # ...and the caller tells the user to click the link themselves.
            buf = _io.StringIO()
            with _contextlib.redirect_stdout(buf):
                control_server._announce_browser("http://127.0.0.1:8787/", False)
            check("127.0.0.1:8787" in buf.getvalue() and "yourself" in buf.getvalue(),
                  "failed launch prints the URL to visit manually")
            buf = _io.StringIO()
            with _contextlib.redirect_stdout(buf):
                control_server._announce_browser("http://127.0.0.1:8787/", True)
            check(buf.getvalue() == "", "--no-browser stays silent")

            # Headless Linux (no WSL, no display): skip xdg-open entirely.
            if sys.platform not in ("darwin", "win32"):
                _os.environ.pop("WSL_DISTRO_NAME", None)
                orig_read = control_server.Path.read_text
                control_server.Path.read_text = lambda self, *a, **k: (
                    "Linux version 6.8.0-71-generic"
                    if str(self) == "/proc/version" else orig_read(self, *a, **k)
                )
                try:
                    check(not control_server._is_wsl(), "plain Linux is not WSL")
                    check(not control_server._open_browser("http://127.0.0.1:8787/"),
                          "headless Linux: no launch attempted")
                finally:
                    control_server.Path.read_text = orig_read
        finally:
            _shutil.which, _subprocess.run = orig_which, orig_run
            _os.environ.clear()
            _os.environ.update(orig_environ)

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
