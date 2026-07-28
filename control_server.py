#!/usr/bin/env python3
"""Local control-plane companion for the TIG swarm.

A small FastAPI app that a host or contributor runs on their own machine to
drive the setup/fleet operations that a remote browser tab physically can't:
Railway provisioning (host) and spawning the local agent fleet (contributor).
It serves the `control-ui/` Svelte bundle and exposes a thin `/local-api/*`
surface that wraps the *existing* orchestration functions — it never
re-implements them:

  - init_fleet.build_fleet_config / write_fleet_config   (contributor config)
  - run_fleet.cmd_run (foreground, stoppable, streamed)  (contributor fleet)
  - setup.create_swarm / switch_challenge                (host provisioning)

Launch it directly (`python control_server.py`) or via `python run.py --ui`.
The CLI wizards (`python run.py`, `python setup.py …`) keep working unchanged;
this is an alternative front door, not a replacement.

The fleet runs in the *foreground* relative to this process: closing the
companion (Ctrl-C) stops the fleet, exactly like `scripts/run_fleet.py` today.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

try:
    import pty  # POSIX only — see RailwayLoginController._spawn
except ImportError:  # pragma: no cover - Windows
    pty = None  # type: ignore[assignment]
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The scripts/ modules import each other by bare name via sys.path (they are not
# a package — see scripts/CLAUDE.md), so add scripts/, root, and server/ (setup
# and init_fleet both import server-side helpers like tiers.py).
for _p in (ROOT / "scripts", ROOT, ROOT / "server"):
    sys.path.insert(0, str(_p))

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import JSONResponse, HTMLResponse, Response
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    sys.stderr.write(
        f"control_server needs FastAPI + uvicorn ({exc.name} missing).\n"
        "Install them with:\n"
        "    pip install -r control-ui-requirements.txt\n"
        "or reuse the server deps:\n"
        "    pip install -r server/requirements.txt\n"
    )
    sys.exit(1)

import init_fleet
import run_fleet
import secrets_local
import setup as setup_mod
import ui_buildstamp

UI_SRC_ROOT = ui_buildstamp.UI_SRC_ROOT
UI_DIST = ui_buildstamp.UI_DIST
# Written into dist/ after every rebuild (and committed with it); lets startup
# tell "bundle matches the sources" from "someone edited src/ and forgot to
# rebuild" without trusting mtimes, which a git checkout scrambles. `npm run
# build` writes it too (postbuild), so a normal rebuild-and-commit leaves it
# current and contributors don't rebuild on first launch for nothing.
UI_BUILD_STAMP = ui_buildstamp.STAMP_PATH
FLEET_CONFIG_PATH = ROOT / "fleet.config.json"
TACIT_PATH = ROOT / "tacit_knowledge.md"
# Runtime record of where the companion for THIS checkout actually listens
# (host/port/pid). Re-runs read it to reopen the live session even when it sits
# on a non-default port (--port, or fallen forward past a collision) instead of
# starting a duplicate on 8787 and printing a link the user's session isn't on.
COMPANION_PORT_FILE = ROOT / ".companion-port.json"


def _ui_source_digest() -> str:
    """Content hash of everything that feeds the Svelte build. Delegates to
    scripts/ui_buildstamp.py, which `npm run build` also calls — the digest has
    to be identical on both sides or the stamp is worse than useless."""
    return ui_buildstamp.source_digest()


def _freshen_ui_bundle() -> None:
    """Rebuild control-ui/dist when the committed bundle no longer matches the
    sources. The companion serves the *built* bundle, so an edit under
    control-ui/ that isn't followed by `npm run build` silently keeps serving
    the old UI. A failed or impossible rebuild degrades to a loud warning and
    the stale bundle — never a startup failure."""
    try:
        digest = _ui_source_digest()
    except OSError as exc:
        print(f"  ⚠  couldn't fingerprint control-ui sources ({exc}); serving dist as-is.")
        return
    try:
        stamp = UI_BUILD_STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        stamp = None
    if stamp == digest and UI_DIST.exists():
        return
    npm = shutil.which("npm")
    if npm is None:
        print(
            "  ⚠  control-ui/dist is out of date with the control-ui sources "
            "and npm isn't installed, so the companion is serving the OLD UI. "
            "Rebuild with:\n"
            "        cd control-ui && npm install && npm run build"
        )
        return
    print("  control-ui sources changed — rebuilding the UI bundle…")
    try:
        if not (UI_SRC_ROOT / "node_modules").exists():
            subprocess.run([npm, "install"], cwd=str(UI_SRC_ROOT), check=True, timeout=600)
        subprocess.run([npm, "run", "build"], cwd=str(UI_SRC_ROOT), check=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"  ⚠  UI rebuild failed ({exc}); serving the previous bundle.")
        return
    try:
        UI_BUILD_STAMP.write_text(digest + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  ⚠  rebuilt, but couldn't write {UI_BUILD_STAMP.name} ({exc}); "
              "the next start will rebuild again.")
    print("  UI bundle rebuilt.")


# ── Event hub: bridge worker-thread callbacks → async WebSocket clients ──


class EventHub:
    """Fan-out for progress events. Worker threads (fleet supervisor, Railway
    deploy) push events; connected WebSocket clients drain them. A ring buffer
    replays recent history to clients that connect mid-run."""

    def __init__(self, history: int = 3000) -> None:
        self._history: deque[dict] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def history(self) -> list[dict]:
        return list(self._history)

    def emit(self, event: dict) -> None:
        """Thread-safe: schedule delivery on the server's event loop."""
        self._history.append(event)
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:  # loop already closed during shutdown
            pass

    def _deliver(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow client — drop rather than block
                pass


# ── Fleet supervisor ──────────────────────────────────────────────────


class FleetController:
    """Owns the single foreground fleet: starts run_fleet.cmd_run in a worker
    thread, mirrors its output/status to the EventHub, and stops it via a
    cooperative Event (cmd_run installs no signal handlers off the main
    thread)."""

    def __init__(self, hub: EventHub) -> None:
        self._hub = hub
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"  # idle | starting | running | stopping | stopped | error
        self.agents: dict[str, dict] = {}  # name -> {pid, returncode, state}
        self.error: str | None = None

    def is_running(self) -> bool:
        # Terminal states win over thread liveness. `stop()` returns a status
        # snapshot synchronously, and run_fleet emits its "stopped" event from
        # INSIDE the worker thread — in both cases the thread is still alive, so
        # a pure is_alive() check reported running=True after a stop. The UI took
        # that at face value and kept showing "Stop fleet" until a second press
        # produced a snapshot from outside the thread.
        if self.state in ("stopped", "error"):
            return False
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "state": self.state,
            "running": self.is_running(),
            "agents": self.agents,
            "error": self.error,
        }

    def _on_output(self, name: str, line: str) -> None:
        self._hub.emit({"type": "log", "name": name, "line": line})

    def _on_status(self, event: str, info: dict) -> None:
        if event == "spawned":
            self.agents[info["name"]] = {"pid": info.get("pid"), "state": "running"}
        elif event == "running":
            self.state = "running"
        elif event == "exited":
            entry = self.agents.setdefault(info["name"], {})
            entry["state"] = "exited"
            entry["returncode"] = info.get("returncode")
        elif event == "stopped":
            self.state = "stopped"
        elif event == "error":
            self.state = "error"
            self.error = info.get("error")
        self._hub.emit({"type": "status", "event": event, "info": info,
                        "fleet": self.status()})

    def start(self, only: list[str] | None = None) -> dict:
        # Thread liveness, not is_running(): that now reports False as soon as
        # the state goes terminal, which is deliberately earlier than the worker
        # actually winds down. Starting against a still-live thread would orphan
        # it and run two fleets at once.
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(
                "the previous fleet is still shutting down — try again in a moment"
            )
        if not FLEET_CONFIG_PATH.exists():
            raise RuntimeError("fleet.config.json not found — configure the fleet first")

        server_url, username, swarm_password, agents, fleet_tacit = (
            run_fleet._load_fleet()
        )

        # Local compute benchmarks in Docker. The fleet auto-starts the daemon,
        # but can't conjure an install — so if any agent is on local compute and
        # Docker isn't even installed, fail NOW with an actionable message
        # instead of letting the agent crash mid-benchmark in the log stream.
        uses_local = any((a.get("compute") or "local") == "local" for a in agents)
        if uses_local and shutil.which("docker") is None:
            raise RuntimeError(
                "This fleet has agents on local compute, but Docker isn't "
                "installed.\n\n" + _docker_manual_hint() + "\n\nOr switch those "
                "agents to C3 cloud compute (no local Docker needed)."
            )

        self._stop = threading.Event()
        self.state = "starting"
        self.error = None
        self.agents = {}

        def _target() -> None:
            try:
                run_fleet.cmd_run(
                    agents, only, server_url, username, swarm_password,
                    fleet_tacit,
                    stop_event=self._stop,
                    on_output=self._on_output,
                    on_status=self._on_status,
                )
            except (Exception, SystemExit) as exc:  # surface, don't crash the server
                # SystemExit too: cmd_run's key resolution (_resolve_api_key)
                # exits with an actionable message when a key is missing — a
                # bare `except Exception` would let that kill this thread
                # silently and leave the UI stuck on "starting".
                self._on_status("error", {"error": f"{type(exc).__name__}: {exc}"})

        self._thread = threading.Thread(target=_target, daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        if self.is_running():
            self.state = "stopping"
            self._stop.set()
        return self.status()


# ── Deploy (host create) job ──────────────────────────────────────────


class DeployController:
    """Runs setup.create_swarm in a worker thread, streaming Railway progress
    to the EventHub. One deploy at a time."""

    def __init__(self, hub: EventHub) -> None:
        self._hub = hub
        self._thread: threading.Thread | None = None
        self.state = "idle"  # idle | running | done | error
        self.result: dict | None = None
        self.error: str | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {"state": self.state, "running": self.is_running(),
                "result": self.result, "error": self.error}

    def start(self, params: dict) -> dict:
        if self.is_running():
            raise RuntimeError("a deploy is already in progress")
        self.state = "running"
        self.result = None
        self.error = None

        def progress(msg: str) -> None:
            self._hub.emit({"type": "deploy_log", "line": msg})

        def _target() -> None:
            try:
                res = setup_mod.create_swarm(params, progress_cb=progress)
                self.result = res
                self.state = "done"
                self._hub.emit({"type": "deploy_status", "event": "done",
                                "result": res})
                # Print a completion banner to the terminal too. create_swarm's
                # last line is a mundane "fleet.config.json already present", and
                # `run.py --ui` is a long-running server that never returns to a
                # shell prompt — so without this the terminal just stops mid-log
                # and reads as "hung", even though the deploy finished and the
                # browser has the result. (The CLI path prints its own summary.)
                url = (res or {}).get("server_url", "?")
                print(f"\n{'='*48}\nSWARM DEPLOYED — {url}\n{'='*48}\n"
                      f"  The browser UI now has the server URL + credentials.\n"
                      f"  This --ui process keeps running to serve that UI; leave\n"
                      f"  it up (Ctrl-C to stop).\n",
                      flush=True)
            except (Exception, SystemExit) as exc:
                # SystemExit too: setup's CLI-oriented helpers may still exit
                # instead of raising — a bare `except Exception` would let that
                # kill the worker thread silently and leave state "running"
                # forever.
                self.error = f"{type(exc).__name__}: {exc}"
                self.state = "error"
                self._hub.emit({"type": "deploy_status", "event": "error",
                                "error": self.error})
                print(f"\n  DEPLOY FAILED: {self.error}\n", file=sys.stderr,
                      flush=True)

        self._thread = threading.Thread(target=_target, daemon=True)
        self._thread.start()
        return self.status()


# ── Railway login (device-code flow) ──────────────────────────────────


class RailwayLoginController:
    """Drives `railway login --browserless` so the host can sign in from the
    UI: spawn the CLI, scrape the pairing link + code from its output, and let
    the UI poll until the process exits — /local-api/railway/status then
    reports authed. The device-code flow is the right one here: the companion
    often runs on a headless/remote box, where the CLI's own browser flow
    can't open anything.

    One attempt at a time; starting a new one kills a stale pending process
    (pairing codes expire after a few minutes, so "start over" must work)."""

    # Pairing codes seen from the CLI: word tuples ("brave-otter-lamp") or
    # grouped alphanumerics ("ABCD-1234"). Scraped loosely on lines that
    # mention "code"; the raw output is always returned as a fallback so an
    # unparsed format still leaves the UI usable.
    _CODE_RE = re.compile(r"\b([A-Za-z0-9]{2,}(?:-[A-Za-z0-9]{2,})+)\b")
    _URL_RE = re.compile(r"https://\S+")
    # The CLI colorizes when it sees a terminal (and it must see one — below);
    # escape codes glued to the URL would otherwise be captured by _URL_RE.
    _ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.state = "idle"  # idle | pending | done | error
        self.url: str | None = None
        self.code: str | None = None
        self.output = ""
        self.error: str | None = None

    def status(self) -> dict:
        return {"state": self.state, "url": self.url, "code": self.code,
                "output": self.output[-1500:], "error": self.error}

    @staticmethod
    def _spawn() -> tuple[subprocess.Popen, typing.IO[str]]:
        """Spawn the CLI, under a pty where the platform has one: the railway
        CLI refuses --browserless when it isn't attached to a terminal
        ("Browserless login requires an interactive terminal"), so plain pipes
        can never work on POSIX. Returns (proc, reader-for-its-output)."""
        # _launch_argv, not a bare "railway": an npm-installed CLI is
        # railway.cmd, which CreateProcess can't run directly.
        cmd = _launch_argv("railway", "login", "--browserless")
        if pty is None:  # Windows: no pty module; the CLI's TTY probe differs
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace",
            )
            assert proc.stdout is not None
            return proc, proc.stdout
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
            )
        except OSError:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        reader = os.fdopen(master_fd, "r", encoding="utf-8", errors="replace")
        return proc, reader

    def start(self) -> dict:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
            self.state = "pending"
            self.url = None
            self.code = None
            self.output = ""
            self.error = None
            try:
                proc, reader = self._spawn()
            except OSError as exc:
                self.state = "error"
                self.error = f"could not run the railway CLI: {exc}"
                return self.status()
            self._proc = proc

        def _watch() -> None:
            try:
                for line in reader:
                    line = self._ANSI_RE.sub("", line).replace("\r", "")
                    self.output += line
                    if self.url is None:
                        m = self._URL_RE.search(line)
                        # Not docs links: the CLI's "New version available"
                        # notice points at docs.railway.com and can precede
                        # the activation URL (https://railway.com/activate).
                        if m and "docs.railway" not in m.group(0):
                            self.url = m.group(0).rstrip(".,)…")
                    if self.code is None and "code" in line.lower():
                        m = self._CODE_RE.search(line)
                        if m:
                            self.code = m.group(1)
            except OSError:
                pass  # pty master raises EIO on Linux when the child exits
            finally:
                reader.close()
            proc.wait()
            # Only the thread watching the CURRENT process may update state —
            # a killed stale process's watcher must not clobber the new run.
            if proc is self._proc:
                if proc.returncode == 0:
                    self.state = "done"
                else:
                    self.state = "error"
                    self.error = (
                        f"railway login exited {proc.returncode} — the pairing "
                        "code may have expired; start again."
                    )

        self._thread = threading.Thread(target=_watch, daemon=True)
        self._thread.start()
        return self.status()


# ── Finding tools this process didn't inherit ──────────────────────────
#
# The companion inherits the PATH of whatever shell started it. Anything
# installed AFTERWARDS — by our own install buttons, or by the user in a second
# terminal — is invisible to `shutil.which` until the companion restarts, which
# is exactly how "I installed Railway but the UI still says not installed"
# happens. The two helpers below close that gap: we re-read the durable PATH
# (Windows registry) and probe the standard per-tool install dirs ourselves.

# Where each tool's installer actually drops its binary.
#   railway: vendor installer precedence is --bin-dir > RAILWAY_BIN_DIR >
#            $RAILWAY_HOME/bin > ~/.railway/bin; we never override, so the
#            default lands in ~/.railway/bin. npm -g puts railway.cmd in
#            %APPDATA%\npm on Windows.
#   c3:      install.sh → ~/.local/bin (or /usr/local/bin); on Windows our own
#            installer writes %LOCALAPPDATA%\Programs\c3.
def _candidate_bindirs(tool: str) -> tuple[Path, ...]:
    home = Path.home()
    common = [home / ".local" / "bin", Path("/usr/local/bin")]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        localapp = os.environ.get("LOCALAPPDATA")
        programfiles = os.environ.get("ProgramFiles")
        common = []
        if appdata:
            common.append(Path(appdata) / "npm")
        if programfiles:
            common.append(Path(programfiles) / "nodejs")
        if localapp:
            common.append(Path(localapp) / "Programs" / "c3")
            common.append(Path(localapp) / "Microsoft" / "WindowsApps")
    per_tool = {
        "railway": [home / ".railway" / "bin"],
        "c3": [home / ".c3" / "bin"],
    }.get(tool, [])
    return tuple(per_tool + common)


def _refresh_windows_path() -> None:
    """Merge the PERSISTED Windows PATH (user + machine) into this process's.

    Installers write PATH to the registry; already-running processes keep the
    copy they were born with. Without this, a user who installs Node/Railway/c3
    in another PowerShell window has to restart the companion before Recheck
    can ever succeed. No-op off Windows."""
    if os.name != "nt":
        return
    try:
        import winreg  # Windows-only stdlib
    except ImportError:  # pragma: no cover - non-Windows
        return
    found: list[str] = []
    for root, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        found.extend(p for p in str(value).split(os.pathsep) if p.strip())
    if not found:
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    lowered = {p.rstrip("\\").lower() for p in current if p}
    # %VAR% references are literal in the registry value — expand them, and drop
    # anything that still can't resolve rather than poisoning PATH with junk.
    extra = []
    for p in found:
        expanded = os.path.expandvars(p).rstrip("\\")
        if not expanded or "%" in expanded:
            continue
        if expanded.lower() not in lowered:
            extra.append(expanded)
            lowered.add(expanded.lower())
    if extra:
        os.environ["PATH"] = os.pathsep.join([*current, *extra])


def _ensure_on_path(tool: str) -> bool:
    """Make `tool` findable by this process, and report whether it is.

    Idempotent: refreshes the durable PATH (Windows), then prepends a candidate
    install dir to os.environ['PATH'] only when it actually holds the binary and
    isn't already there."""
    if shutil.which(tool) is not None:
        return True
    _refresh_windows_path()
    if shutil.which(tool) is not None:
        return True
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in _candidate_bindirs(tool):
        # On Windows the shim may be .exe or .cmd (npm -g); check both.
        names = (f"{tool}.exe", f"{tool}.cmd", f"{tool}.bat") if os.name == "nt" else (tool,)
        if any((d / n).exists() for n in names) and str(d) not in parts:
            os.environ["PATH"] = os.pathsep.join([str(d), *parts])
            parts = os.environ["PATH"].split(os.pathsep)
    return shutil.which(tool) is not None


def _ensure_railway_on_path() -> bool:
    """Back-compat alias — several call sites read as "is railway usable?"."""
    return _ensure_on_path("railway")


def _launch_argv(tool: str, *args: str) -> list[str]:
    """argv for running `tool`, correct for Windows shims.

    npm installs console scripts as `railway.cmd` / `npm.cmd`. CreateProcess —
    what subprocess.Popen uses — cannot execute a .cmd/.bat directly, so
    Popen(["railway", ...]) fails on a perfectly good npm install. That is why
    an npm-installed Railway CLI "didn't register". shutil.which honours
    PATHEXT and finds the shim; we then run it through the command interpreter."""
    _ensure_on_path(tool)
    exe = shutil.which(tool)
    if exe and os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *args]
    return [exe or tool, *args]


class RailwayInstallController:
    """Installs the Railway CLI via the vendor script so a host with no CLI can
    get unblocked from the UI — no separate terminal step. Runs
    `curl -fsSL railway.com/install.sh | bash -s -- -y` (the `-y` skips the
    installer's confirmation prompt); the default bin dir is ~/.railway/bin,
    which is under $HOME and needs no sudo. The UI polls status() until the
    process exits, then re-checks railway/status (now `installed: true`).

    POSIX only. Windows has no bash/curl-pipe-bash contract, so start() there
    fails fast with the manual-install hint instead of a confusing shell error.

    One attempt at a time; a new start() kills any pending run."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.state = "idle"  # idle | pending | done | error
        self.output = ""
        self.error: str | None = None

    def status(self) -> dict:
        return {"state": self.state, "output": self.output[-2000:],
                "error": self.error}

    def start(self) -> dict:
        # Windows has no curl-pipe-bash contract. It does have npm, which ships
        # the same CLI, but driving that from here proved unreliable — so the
        # host page shows the npm command instead and never calls this. Fail
        # fast with that hint rather than a confusing shell error.
        if os.name == "nt":
            self.state = "error"
            self.error = (
                "On Windows the Railway CLI comes from npm. In PowerShell:\n"
                "    npm.cmd install -g @railway/cli\n"
                "(install Node first with `winget install OpenJS.NodeJS.LTS` "
                "and open a NEW PowerShell window), then click Recheck."
            )
            return self.status()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
            self.state = "pending"
            self.output = ""
            self.error = None
            # `-s --` forwards `-y` to the piped script; without a controlling
            # TTY the installer would otherwise wait on a confirmation prompt.
            cmd = ["bash", "-c",
                   "curl -fsSL https://railway.com/install.sh | bash -s -- -y"]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True,
                    encoding="utf-8", errors="replace", cwd=str(ROOT),
                )
            except OSError as exc:
                self.state = "error"
                self.error = f"could not start the installer: {exc}"
                return self.status()
            self._proc = proc

        def _watch() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    # The installer colorizes; strip ANSI so the UI's log pane
                    # shows plain text, not escape gibberish.
                    self.output += RailwayLoginController._ANSI_RE.sub("", line)
            finally:
                proc.stdout.close()
            proc.wait()
            if proc is not self._proc:
                return  # a newer run superseded this one
            if proc.returncode == 0 and _ensure_railway_on_path():
                self.state = "done"
            else:
                self.state = "error"
                manual = (
                    "npm.cmd install -g @railway/cli"
                    if os.name == "nt" else "see railway.com/install.sh"
                )
                self.error = (
                    "The installer exited without leaving a usable `railway` "
                    f"on PATH. Install it manually ({manual}) and click Recheck."
                    if proc.returncode == 0
                    else f"installer exited {proc.returncode} — see the log below."
                )

        self._thread = threading.Thread(target=_watch, daemon=True)
        self._thread.start()
        return self.status()


# ── Docker install ─────────────────────────────────────────────────────

# Docker's official convenience script — the same contract as
# railway.com/install.sh above: vendor-maintained, and it detects the distro and
# codename itself, so a brand-new Ubuntu works the day Docker publishes for it
# and we never carry a table of release names here.
#
# Unlike Railway's, it needs root: Docker Engine is a system daemon, not a binary
# dropped in $HOME. That single fact drives everything below.
_DOCKER_GET_URL = "https://get.docker.com"


def _docker_manual_hint() -> str:
    """Copy-pasteable fallback for when we can't install it ourselves. Platform
    specific, because the right *product* differs: Desktop is a GUI app for
    Mac/Windows, Engine is the daemon on Linux — telling a headless Linux box to
    install Docker Desktop is a dead end."""
    if os.name == "nt":
        return ("Install Docker Desktop "
                "(https://www.docker.com/products/docker-desktop/), start it, "
                "then click Recheck.")
    if sys.platform == "darwin":
        return ("Install Docker Desktop "
                "(https://www.docker.com/products/docker-desktop/) or OrbStack, "
                "start it, then click Recheck.")
    return ("Install Docker Engine with:\n"
            "    curl -fsSL https://get.docker.com | sudo sh\n"
            "    sudo systemctl enable --now docker\n"
            "then click Recheck.")


def _docker_privilege() -> str:
    """How this process can reach root: 'root', 'sudo' (passwordless), or 'none'.

    `sudo -n true` is the only honest probe. sudo with no controlling TTY can't
    prompt, so if it isn't already passwordless (or the timestamp is cached) the
    install would block forever on a password nobody can see — we'd rather find
    that out here and refuse than hang the UI on 'pending'."""
    if os.name == "nt":
        return "none"
    if os.geteuid() == 0:
        return "root"
    if _cmd_ok(["sudo", "-n", "true"]):
        return "sudo"
    return "none"


def docker_install_support() -> dict:
    """Can we install Docker for the user on THIS machine, and how?

    Advisory, for the UI: `supported` gates the install button and `reason`
    explains a refusal, so the wizard can offer the one-click path where it
    genuinely works and honest instructions everywhere else. Linux is the only
    platform we can drive unattended — macOS/Windows mean a GUI installer."""
    if os.name == "nt" or sys.platform == "darwin":
        return {"supported": False, "method": None,
                "reason": "Automatic install is Linux-only.",
                "manual": _docker_manual_hint()}
    priv = _docker_privilege()
    if priv == "none":
        return {"supported": False, "method": None,
                "reason": ("Installing Docker Engine needs root, and this "
                           "companion can't get it without a password prompt "
                           "it has no way to show you."),
                "manual": _docker_manual_hint()}
    return {"supported": True, "method": priv, "reason": None,
            "manual": _docker_manual_hint()}


class DockerInstallController:
    """Installs Docker Engine via Docker's convenience script so a Linux
    contributor on local compute can get unblocked from the UI. Mirrors
    RailwayInstallController: POST starts it, the UI polls status() until the
    process exits, then re-reads preflight (now `docker.installed`).

    Two things differ from the Railway install, both because this needs root:

      - start() refuses up front (see docker_install_support) instead of hanging
        on an invisible sudo password prompt.
      - The script installs the engine, but a *non-root* installer leaves the
        calling user unable to talk to the socket until their new `docker` group
        membership is picked up — which takes a fresh login. We add the group,
        then report `needs_relogin` rather than pretending installed == usable
        and letting the fleet die mid-benchmark on a permission denial.

    One attempt at a time; a new start() kills any pending run."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.state = "idle"  # idle | pending | done | error
        self.output = ""
        self.error: str | None = None
        self.needs_relogin = False

    def status(self) -> dict:
        return {"state": self.state, "output": self.output[-4000:],
                "error": self.error, "needs_relogin": self.needs_relogin}

    def start(self) -> dict:
        support = docker_install_support()
        if not support["supported"]:
            self.state = "error"
            self.error = f"{support['reason']}\n\n{support['manual']}"
            return self.status()
        sudo = "" if support["method"] == "root" else "sudo -n "
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
            self.state = "pending"
            self.output = ""
            self.error = None
            self.needs_relogin = False
            # get.docker.com enables the unit on systemd hosts, but not on every
            # init/container combo — enabling again is idempotent and cheap, so
            # do it explicitly and tolerate failure (`|| true`) rather than fail
            # an otherwise-good install on a non-systemd box.
            lines = [
                "set -e",
                f"curl -fsSL {_DOCKER_GET_URL} | {sudo}sh",
                f"{sudo}systemctl enable --now docker || true",
            ]
            if support["method"] == "sudo":
                lines.append(f'{sudo}usermod -aG docker "$(id -un)" || true')
            try:
                proc = subprocess.Popen(
                    ["bash", "-c", "\n".join(lines)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True,
                    encoding="utf-8", errors="replace", cwd=str(ROOT),
                )
            except OSError as exc:
                self.state = "error"
                self.error = f"could not start the installer: {exc}"
                return self.status()
            self._proc = proc

        def _watch() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    # The script colorizes; strip ANSI so the UI's log pane shows
                    # plain text, not escape gibberish.
                    self.output += RailwayLoginController._ANSI_RE.sub("", line)
            finally:
                proc.stdout.close()
            proc.wait()
            if proc is not self._proc:
                return  # a newer run superseded this one
            if proc.returncode != 0:
                self.state = "error"
                self.error = (
                    f"installer exited {proc.returncode} — see the log below.\n\n"
                    + support["manual"]
                )
                return
            if shutil.which("docker") is None:
                self.state = "error"
                self.error = ("The installer finished but left no `docker` on "
                              "PATH.\n\n" + support["manual"])
                return
            self.needs_relogin = (
                support["method"] == "sudo" and not _cmd_ok(["docker", "info"])
            )
            self.state = "done"

        self._thread = threading.Thread(target=_watch, daemon=True)
        self._thread.start()
        return self.status()


# ── C3 CLI install / update ────────────────────────────────────────────

_C3_INSTALL_SH = "https://cthree.cloud/install.sh"


def c3_version() -> str | None:
    """`c3 --version` output, or None when the CLI isn't installed/runnable."""
    if not _ensure_on_path("c3"):
        return None
    try:
        res = subprocess.run(
            _launch_argv("c3", "--version"), capture_output=True, timeout=8,
            text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or res.stderr or "").strip()
    return out.splitlines()[0] if out else None


class C3InstallController:
    """Installs — or updates — the c3 CLI from the UI.

    C3 is a young platform shipping often, so this is deliberately an *update*
    path too, not just a first install: re-running it overwrites the binary in
    place. Mirrors the Railway/Docker controllers (POST starts, the UI polls
    status()).

    POSIX only, like the Railway installer. Windows has no curl-pipe-sh
    contract; downloading the release binary and editing PATH from here was
    tried and didn't hold up, so the contributor page shows the documented
    PowerShell commands instead and never calls this — start() there fails
    fast with the same hint rather than pretending.

    One attempt at a time; a new start() supersedes any pending run."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._token = 0
        self.state = "idle"  # idle | pending | done | error
        self.output = ""
        self.error: str | None = None
        self.version: str | None = None

    def status(self) -> dict:
        return {"state": self.state, "output": self.output[-4000:],
                "error": self.error, "version": self.version}

    # ── POSIX: the vendor one-liner ──
    def _install_posix(self, token: int) -> None:
        cmd = ["bash", "-c", f"curl -fsSL {_C3_INSTALL_SH} | sh"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", cwd=str(ROOT),
            )
        except OSError as exc:
            self._fail(token, f"could not start the installer: {exc}")
            return
        self._proc = proc
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                self._log(RailwayLoginController._ANSI_RE.sub("", line))
        finally:
            proc.stdout.close()
        proc.wait()
        if proc.returncode != 0:
            self._fail(token, f"installer exited {proc.returncode} — see the log below.")
            return
        self._finish(token)

    # ── shared ──
    def _log(self, text: str) -> None:
        self.output += text

    def _fail(self, token: int, message: str) -> None:
        if token != self._token:
            return  # superseded
        self.state = "error"
        self.error = message

    def _finish(self, token: int) -> None:
        if token != self._token:
            return
        version = c3_version()
        if version is None:
            self.state = "error"
            self.error = (
                "The install finished but `c3` still isn't runnable from this "
                "process. Open a new terminal and run `c3 --version`; if that "
                "works, restart this companion."
            )
            return
        self.version = version
        self._log(f"\n{version}\n")
        self.state = "done"

    def start(self) -> dict:
        if os.name == "nt":
            self.state = "error"
            self.error = (
                "On Windows, install c3 from PowerShell — the page shows the "
                "commands — then click Recheck."
            )
            return self.status()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
            self._token += 1
            token = self._token
            self.state = "pending"
            self.output = ""
            self.error = None
            self.version = None

        def _run() -> None:
            try:
                self._install_posix(token)
            except Exception as exc:  # never leave the UI stuck on "pending"
                self._fail(token, f"unexpected error: {exc}")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return self.status()


# ── App factory ────────────────────────────────────────────────────────


# ── Live model catalogs ────────────────────────────────────────────────

# provider key -> (fetched_at, models). The wizard re-enters the provider step
# freely (Back/Continue), and each miss is a network round trip to someone
# else's API, so hold the answer briefly. Short enough that a key added in
# another tab shows up without a restart.
_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}
_MODELS_TTL = 600.0


def _readable_api_error(exc: Exception) -> str:
    """The human sentence inside a provider's error, not its raw JSON body.

    list_models raises `HTTP 401: {"type":"error","error":{...,"message":"invalid
    x-api-key"},"request_id":"..."}`. Shown verbatim in the wizard that is four
    lines of punctuation around one useful phrase, so dig the message out and
    keep the status code that tells the user WHICH kind of failure it is."""
    raw = str(exc)
    head, _, body = raw.partition(": ")
    body = body.strip()
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for value in (data.get("error"), data.get("detail"), data):
                if isinstance(value, dict) and isinstance(value.get("message"), str):
                    return f"{head}: {value['message']}"
                if isinstance(value, str) and value:
                    return f"{head}: {value}"
    # Not a JSON body (network error, plain text) — cap it so one enormous
    # upstream blob can't push the form off the screen.
    return raw if len(raw) <= 300 else raw[:300] + "…"


def _live_models_custom(api_base: str | None, api_key_env: str | None) -> dict:
    """Model catalog for a contributor's own OpenAI-compatible endpoint.

    Worth doing rather than making them type an id from memory: local servers
    name models after the weights file they loaded (`Qwen3-Coder-Next-Q8_0`),
    which nobody recalls exactly. Not cached — the URL is a live form field and
    a server that just loaded a different model must show it immediately."""
    base = (api_base or "").strip()
    if not base:
        return {"models": [], "error": (
            "Enter your endpoint URL above to list the models it serves."
        )}
    # Optional by design: llama.cpp/vLLM/Ollama default to no auth at all.
    api_key = secrets_local.resolve((api_key_env or "").strip()) or None
    try:
        from llm_backends import list_models
        found = list_models("openai", api_key=api_key, api_base=base)
    except ValueError as exc:
        return {"models": [], "error": str(exc)}
    except (RuntimeError, OSError) as exc:
        return {"models": [], "error": (
            f"Could not reach {base}: {_readable_api_error(exc)}. Check the "
            "server is running and the URL includes the right port and path "
            "(usually ending in /v1)."
        )}
    return {"models": found, "error": None}


def _live_models(
    provider: str,
    *,
    refresh: bool = False,
    api_base: str | None = None,
    api_key_env: str | None = None,
) -> dict:
    """`{"models": [...], "error": str | None}` for one provider key.

    Wraps llm_backends.list_models (the same call `scripts/list_models.py`
    exposes on the CLI) with the two things the UI needs: the API key resolved
    from the local secret store, and failure expressed as data rather than an
    exception — the dropdown always has its curated shortlist to fall back on.

    `api_base` / `api_key_env` are the custom-provider path: that endpoint is
    the contributor's own, so its URL and key name arrive with the request
    instead of coming from the provider table."""
    spec = next((p for p in init_fleet.PROVIDERS if p[0] == provider), None)
    if spec is None:
        return {"models": [], "error": f"unknown provider: {provider}"}
    if provider in init_fleet.NEEDS_API_BASE:
        return _live_models_custom(api_base, api_key_env)
    api_key_env = spec[3]
    # The Claude CLI has no `models list` command (Codex does). But it drives
    # the same vendor, so an ANTHROPIC_API_KEY the contributor happens to have
    # stored lists the right model ids — indicative rather than authoritative,
    # since the CLI runs on a subscription whose entitlements can differ. Only
    # a borrowed key, never a required one: with none, the shortlist (aliases
    # first) stands on its own.
    borrowed_key_env = (
        "ANTHROPIC_API_KEY" if api_key_env is None and provider.startswith("claude-code")
        else None
    )
    if api_key_env is None and borrowed_key_env and not secrets_local.resolve(borrowed_key_env):
        return {"models": [], "error": (
            f"{spec[1]} uses its own login, so there's no catalog to fetch. "
            "Pick an alias — opus / sonnet / haiku always resolve to the newest "
            "model in that family — or a dated id to pin one version."
        )}
    if api_key_env is None and not borrowed_key_env and provider != "codex-agentic":
        return {"models": [], "error": (
            f"{spec[1]} uses its own login rather than an API key, so there is "
            "no model list to fetch — it accepts any model the CLI knows."
        )}
    now = time.time()
    if not refresh:
        hit = _MODELS_CACHE.get(provider)
        if hit and now - hit[0] < _MODELS_TTL:
            return {"models": hit[1], "error": None}
    api_key = secrets_local.resolve(api_key_env or borrowed_key_env or "") or None
    # OpenRouter's catalog is public — listing it needs no key (same exemption
    # scripts/list_models.py makes).
    if not api_key and provider not in ("openrouter", "codex-agentic"):
        return {"models": [], "error": (
            f"Add your {api_key_env} to see every model this account can use."
        )}
    # OpenRouter / DeepSeek are reached as OpenAI-compatible endpoints — the
    # same remap build_fleet_config applies when writing the agent config.
    call_provider, api_base = provider, None
    if provider == "openrouter":
        api_base = init_fleet._OPENROUTER_API_BASE
    elif provider == "deepseek":
        call_provider, api_base = "openai", init_fleet._DEEPSEEK_API_BASE
    elif borrowed_key_env:
        # Ask Anthropic's HTTP API on the CLI provider's behalf.
        call_provider = "anthropic"
    try:
        from llm_backends import list_models
        found = list_models(call_provider, api_key=api_key, api_base=api_base)
    except ValueError as exc:
        return {"models": [], "error": str(exc)}
    except (RuntimeError, OSError) as exc:
        # Bad key, rate limit, offline — all recoverable from the UI's side.
        if borrowed_key_env:
            # Don't blame the CLI for a borrowed key's failure.
            return {"models": [], "error": (
                f"Couldn't list models with your {borrowed_key_env}: "
                f"{_readable_api_error(exc)}. {spec[1]} still accepts any model "
                "the CLI knows, including the aliases above."
            )}
        action = (
            "load the model catalog from"
            if provider == "codex-agentic" else "reach"
        )
        return {"models": [], "error":
                f"Could not {action} {spec[1]}: {_readable_api_error(exc)}"}
    _MODELS_CACHE[provider] = (now, found)
    return {"models": found, "error": None}


def _cmd_ok(args: list[str], timeout: float = 4.0) -> bool:
    """Run a command and report whether it exited 0. Swallows missing-binary,
    timeout, and OS errors — used only for best-effort capability probes."""
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout).returncode == 0
    except Exception:
        return False


def preflight_status() -> dict:
    """Best-effort probe of what the fleet needs to run, so the wizard can guide
    a non-technical contributor BEFORE they hit a mid-run failure. All fields are
    advisory — nothing here blocks; the UI decides what to surface.

      - c3:     the default compute. `key_in_env` or a `c3 login` session (or a
                key pasted in the wizard) means no local Docker is needed at all.
      - docker: only needed for `compute: local`. The fleet auto-starts the
                daemon if Docker is *installed*, so `installed` is the check that
                matters; `running` is extra signal. `install_support` tells the
                UI whether we can offer the one-click install here (Linux + root
                or passwordless sudo) or must fall back to `manual` instructions.
    """
    # _ensure_on_path, not a bare which(): the user may have installed these in
    # another terminal after this companion started, and Recheck must be able to
    # see that without a restart.
    docker_installed = _ensure_on_path("docker")
    c3_installed = _ensure_on_path("c3")
    return {
        "docker": {
            "installed": docker_installed,
            "running": docker_installed and _cmd_ok(["docker", "info"]),
            "install_support": docker_install_support(),
        },
        "c3": {
            "cli_installed": c3_installed,
            # Shown next to the Update button — C3 ships often, so "which
            # version am I on" is the question that follows "is it installed".
            "version": c3_version() if c3_installed else None,
            "key_in_env": bool(os.environ.get("C3_API_KEY")),
        },
        "git": {"installed": shutil.which("git") is not None},
        # Coding-agent CLIs used by the login-based providers (claude-code*,
        # codex-agentic). We can cheaply see if the binary is on PATH; login
        # state isn't reliably introspectable, so the UI advises `<cli> login`.
        "clis": {
            "claude": shutil.which("claude") is not None,
            "codex": shutil.which("codex") is not None,
            # Node/npm: the Railway CLI's only install route on Windows. The
            # host page shows the "install Node first" step only when npm is
            # actually missing, and can run the install itself when it isn't.
            # _ensure_on_path, not which(): it knows %APPDATA%\npm and
            # %ProgramFiles%\nodejs and re-reads the persisted PATH, so a Node
            # installed in another terminal is seen without a restart.
            "node": _ensure_on_path("node"),
            "npm": _ensure_on_path("npm"),
        },
    }


def _origin_is_loopback(origin: str) -> bool:
    """True if an Origin header names a loopback host. An absent Origin is
    fine (non-browser clients don't send one); "null" and any web origin are
    not — a rebinding/attacker page must never read the stream."""
    if not origin:
        return True
    try:
        hostname = urllib.parse.urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def _host_is_loopback(host_header: str) -> bool:
    """True if the Host header names a loopback address. Handles an optional
    :port and the bracketed IPv6 form ([::1]:8787)."""
    if not host_header:
        return False
    if host_header.startswith("["):  # [::1] or [::1]:port
        end = host_header.find("]")
        hostname = host_header[1:end] if end != -1 else host_header
    elif host_header.count(":") == 1:  # host:port (IPv4 / name)
        hostname = host_header.rsplit(":", 1)[0]
    else:  # bare name, or unbracketed IPv6 literal (e.g. ::1)
        hostname = host_header
    return hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def create_app(allow_remote: bool = False) -> FastAPI:
    app = FastAPI(title="TIG Swarm Control", docs_url=None, redoc_url=None)
    hub = EventHub()
    fleet = FleetController(hub)
    deploy = DeployController(hub)
    railway_login = RailwayLoginController()
    railway_install = RailwayInstallController()
    docker_install = DockerInstallController()
    c3_install = C3InstallController()

    # DNS-rebinding guard. This companion serves host credentials (e.g.
    # /local-api/swarm/admin returns the admin_key) and can start/stop fleets,
    # so it must only ever answer requests actually destined for localhost.
    # A malicious web page can rebind its own domain to 127.0.0.1 and make the
    # victim's browser hit this server — but the request still carries the
    # attacker's Host header, which we reject here. Bypassed only when the
    # operator explicitly binds a non-loopback address (--host), an opt-in
    # they're warned about at startup.
    @app.middleware("http")
    async def _guard_host(request: Request, call_next):
        if not allow_remote and not _host_is_loopback(request.headers.get("host", "")):
            return JSONResponse(
                {"error": "refused: non-loopback Host header (DNS-rebinding guard)"},
                status_code=403,
            )
        response = await call_next(request)
        # Anti-clickjacking: the Host guard above stops DNS-rebinding, but a page
        # can still *directly* iframe http://127.0.0.1:<port> (that request
        # carries a loopback Host and passes) and overlay the fleet/admin
        # controls to steal clicks. This companion never frames itself, so deny
        # framing outright. `nosniff` blocks content-type confusion on the
        # /local-api JSON. Both are set on every response, static bundle included.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.on_event("startup")
    async def _startup() -> None:
        hub.attach_loop(asyncio.get_running_loop())

    # ── Environment / capabilities ──
    @app.get("/local-api/env")
    def env() -> dict:
        admin = setup_mod.read_swarm_admin()
        return {
            "mode": "local",
            "cwd": str(ROOT),
            "has_fleet_config": FLEET_CONFIG_PATH.exists(),
            "has_swarm_admin": bool(admin.get("admin_key")),
            "server_url": admin.get("server_url") or setup_mod.resolve_server_url(),
        }

    @app.get("/local-api/preflight")
    def preflight() -> dict:
        """Capability probe (Docker / C3 / git) for the wizard's readiness UI."""
        return preflight_status()

    @app.get("/local-api/providers")
    def providers() -> dict:
        return {
            "providers": init_fleet.get_providers(),
            "c3_hardware": init_fleet.get_c3_hardware_choices(),
        }

    @app.get("/local-api/models")
    def models(
        provider: str,
        refresh: bool = False,
        api_base: str | None = None,
        api_key_env: str | None = None,
    ) -> dict:
        """The models `provider` exposes, fetched live from its API or CLI.

        Powers the wizard's model dropdown, so a contributor picks from what
        their account actually has today instead of typing an id from memory.
        `api_base`/`api_key_env` carry the custom provider's own endpoint.
        Never an error response: a missing key, unsupported CLI provider, an
        unreachable local server, or a catalog hiccup returns an empty list
        plus a reason, and the UI falls back to typing an id by hand."""
        return _live_models(
            provider, refresh=refresh, api_base=api_base, api_key_env=api_key_env,
        )

    @app.get("/local-api/challenges")
    def challenges() -> dict:
        # track_defaults powers the host UI's "customize instances" editor:
        # per challenge, the track keys and the instance count `create` would
        # use by default (mirrors collect_per_challenge_configs).
        # Same seed values the CLI wizard's per-track prompts use: the
        # DEFAULT_TRACKS_PER_CHALLENGE count where one exists, else 0.
        track_defaults = {
            ch: {
                key: setup_mod.DEFAULT_TRACKS_PER_CHALLENGE.get(ch, {}).get(key, 0)
                for key in meta["track_keys"]
            }
            for ch, meta in setup_mod.CHALLENGES.items()
        }
        return {
            "cpu": list(setup_mod.CPU_CHALLENGES.keys()),
            "gpu": list(setup_mod.GPU_CHALLENGES.keys()),
            "all": list(setup_mod.CHALLENGES.keys()),
            "track_defaults": track_defaults,
            # Per-challenge solver timeout `create` uses by default — powers
            # the host UI's per-challenge timeout field.
            "timeout_defaults": {
                ch: meta.get("default_timeout", 30)
                for ch, meta in setup_mod.CHALLENGES.items()
            },
        }

    # ── Contributor: fleet config + tacit ──
    @app.get("/local-api/fleet/config")
    def get_fleet_config() -> dict:
        if not FLEET_CONFIG_PATH.exists():
            return {"exists": False, "config": None}
        try:
            return {"exists": True, "config": json.loads(FLEET_CONFIG_PATH.read_text())}
        except (json.JSONDecodeError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/local-api/fleet/config")
    async def set_fleet_config(payload: dict) -> dict:
        try:
            config = init_fleet.build_fleet_config(payload)
            init_fleet.write_fleet_config(config)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"config": config}

    @app.post("/local-api/fleet/config/save")
    async def save_fleet_config(payload: dict) -> dict:
        """Write a full fleet.config.json verbatim (the direct editor path).

        Unlike /fleet/config (which regenerates agents from wizard params via
        build_fleet_config), this preserves every field the editor passes
        through — role, api_base, detailed_prompts, hpo knobs, per-agent tacit
        paths — so editing one value never silently drops the rest. Validates
        the same invariants run_fleet._load_fleet enforces at launch."""
        config = payload.get("config") if "config" in payload else payload
        if not isinstance(config, dict):
            return JSONResponse({"error": "config must be an object"}, status_code=400)
        for key in ("server_url", "username", "swarm_password"):
            if not str(config.get(key, "")).strip():
                return JSONResponse({"error": f"{key} is required"}, status_code=400)
        agents = config.get("agents")
        if not isinstance(agents, list) or not agents:
            return JSONResponse({"error": "at least one agent is required"}, status_code=400)
        names = []
        legal = init_fleet.wire_providers()
        for a in agents:
            if not isinstance(a, dict) or not str(a.get("name", "")).strip():
                return JSONResponse({"error": "every agent needs a name"}, status_code=400)
            # A setup-level key (deepseek / openrouter / custom) is a valid
            # answer to "which vendor?" but NOT a valid config value — run_fleet
            # exits with "unknown provider" on it. Catch it here, where we can
            # say what to write instead, rather than at launch.
            prov = str(a.get("provider", "")).strip()
            if prov and prov not in legal:
                wire, base = init_fleet.resolve_wire_provider(prov)
                hint = (
                    f" — write provider {wire!r}"
                    + (f" with api_base {base!r}" if base else "")
                    if wire in legal else ""
                )
                return JSONResponse(
                    {"error": f"agent {a['name']!r}: unknown provider {prov!r}{hint}"},
                    status_code=400,
                )
            names.append(a["name"])
        if len(names) != len(set(names)):
            return JSONResponse({"error": "agent names must be unique"}, status_code=400)
        init_fleet.write_fleet_config(config)
        return {"config": config}

    @app.get("/local-api/tacit/questions")
    def tacit_questions() -> dict:
        """The prompts the CLI wizard asks (hostadmin/tacit.py), served so the
        setup app asks exactly the same ones — the guided form and
        `python setup.py tacit` must not drift into two different interviews."""
        return {"questions": setup_mod.TACIT_QUESTIONS}

    @app.post("/local-api/tacit")
    async def set_tacit(payload: dict) -> dict:
        """Append tacit-knowledge to the fleet-default tacit file.

        `payload`: either {"text": "..."} (a raw paste block) or
        {"answers": [{"title": "...", "body": "..."}, ...]} from the guided
        form. Composed the same way as setup.py's guided capture."""
        threshold = setup_mod.read_swarm_admin().get("stagnation_threshold", 2)
        path = TACIT_PATH
        body = (payload.get("text") or "").strip()
        if not body and payload.get("answers"):
            sections = [
                f"### {a['title']}\n\n{a['body'].strip()}"
                for a in payload["answers"]
                if a.get("title") and a.get("body", "").strip()
            ]
            body = "\n\n".join(sections)
        if not body:
            return JSONResponse({"error": "no tacit content provided"}, status_code=400)
        if not path.exists():
            path.write_text(setup_mod.tacit_header(threshold), encoding="utf-8")
        existing = path.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        path.write_text(existing + sep + body + "\n", encoding="utf-8")
        try:
            shown = str(path.relative_to(ROOT))
        except ValueError:
            shown = str(path)
        return {"ok": True, "path": shown}

    # ── Contributor: fleet lifecycle ──
    @app.get("/local-api/fleet/status")
    def fleet_status() -> dict:
        return fleet.status()

    @app.post("/local-api/fleet/start")
    async def fleet_start(payload: dict | None = None) -> dict:
        only = (payload or {}).get("only")
        try:
            return fleet.start(only=only)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except SystemExit as exc:
            # run_fleet._load_fleet is CLI-oriented and exits with an
            # actionable message (e.g. server-sourced config with no agents
            # saved yet). Surface it as a 400, not a 500 traceback.
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/local-api/fleet/stop")
    async def fleet_stop() -> dict:
        return fleet.stop()

    # ── Host: railway + provisioning ──
    @app.get("/local-api/railway/status")
    def railway_status() -> dict:
        # Pick up a railway installed in ~/.railway/bin even when the companion's
        # shell never sourced it — otherwise a valid install still reads as
        # "not installed". `installed` lets the UI branch: missing CLI → offer to
        # install; present-but-unauthed → offer to log in.
        installed = _ensure_railway_on_path()
        if not installed:
            return {"available": False, "installed": False, "authed": False,
                    "message": "The Railway CLI isn't installed."}
        try:
            user = setup_mod._railway_check_auth()
            who = user.get("email") or user.get("name") or "unknown"
            workspaces = [
                w["name"] for w in (user.get("workspaces") or []) if w.get("name")
            ]
            return {"available": True, "installed": True, "authed": True,
                    "user": who, "workspaces": workspaces}
        except (Exception, SystemExit) as exc:
            # CLI present but not logged in (or a transient whoami failure) —
            # translate to a soft status so the UI shows the login flow.
            return {"available": False, "installed": True, "authed": False,
                    "message": str(exc)}

    @app.get("/local-api/railway/name-check")
    def railway_name_check(name: str, workspace: str | None = None) -> dict:
        """Does a Railway project by this name already exist?

        Lets the host UI warn BEFORE provisioning. Re-provisioning an existing
        name is adoption, not replacement — the data volume and (since
        `_resolve_swarm_credentials`) the credentials are preserved — but it
        does redeploy a live server, so it should never happen by accident.

        `exists: false` on any lookup failure: this gate must not block a
        legitimate create just because the Railway API blipped."""
        try:
            project = setup_mod._railway_find_project(name, workspace)
        except (Exception, SystemExit):
            return {"exists": False, "checked": False}
        if not project:
            return {"exists": False, "checked": True}
        admin = setup_mod.read_swarm_admin()
        return {
            "exists": True,
            "checked": True,
            # True when this companion holds the swarm's admin file, i.e. the
            # host is re-provisioning their OWN swarm rather than colliding
            # with someone else's name in a shared workspace.
            "is_yours": admin.get("swarm_name") == name,
            "server_url": admin.get("server_url") if admin.get("swarm_name") == name else None,
        }

    @app.post("/local-api/railway/install")
    async def railway_install_start() -> dict:
        """Install the Railway CLI via the vendor script. Returns immediately;
        the UI polls the GET below until it exits, then re-reads status."""
        return railway_install.start()

    @app.get("/local-api/railway/install")
    async def railway_install_status() -> dict:
        return railway_install.status()

    @app.post("/local-api/docker/install")
    async def docker_install_start() -> dict:
        """Install Docker Engine (Linux, root or passwordless sudo). Returns
        immediately; the UI polls the GET below until it exits, then re-reads
        preflight."""
        return docker_install.start()

    @app.get("/local-api/docker/install")
    async def docker_install_status() -> dict:
        return docker_install.status()

    @app.post("/local-api/c3/install")
    async def c3_install_start() -> dict:
        """Install — or update — the c3 CLI. Same endpoint for both: C3 ships
        often, and re-running overwrites the binary in place. Returns
        immediately; the UI polls the GET below, then re-reads preflight."""
        return c3_install.start()

    @app.get("/local-api/c3/install")
    async def c3_install_status() -> dict:
        return c3_install.status()

    @app.post("/local-api/railway/login")
    async def railway_login_start() -> dict:
        """Start (or restart) a device-code Railway login. Returns the pairing
        link + code once the CLI prints them; the UI polls the GET below."""
        return railway_login.start()

    @app.get("/local-api/railway/login")
    def railway_login_status() -> dict:
        return railway_login.status()

    @app.get("/local-api/swarm/admin")
    def swarm_admin() -> dict:
        """Host-only: the local admin creds so the UI can deep-link into the
        hosted Admin Console pre-filled."""
        admin = setup_mod.read_swarm_admin()
        return {
            "server_url": admin.get("server_url"),
            "admin_key": admin.get("admin_key"),
            "swarm_password": admin.get("swarm_password"),
            "active_challenge": admin.get("active_challenge"),
        }

    @app.post("/local-api/swarm/create")
    async def swarm_create(payload: dict) -> dict:
        """Provision a swarm. Builds challenges_cfg (defaults) then streams the
        Railway deploy via the /local-api/stream WebSocket (type deploy_log)."""
        swarm_type = payload.get("swarm_type", "cpu")
        challenge_set = (
            setup_mod.GPU_CHALLENGES if swarm_type == "gpu" else setup_mod.CPU_CHALLENGES
        )
        active_challenge = payload.get("active_challenge") or next(iter(challenge_set))
        if active_challenge not in challenge_set:
            return JSONResponse(
                {"error": f"{active_challenge} not available in a {swarm_type} swarm"},
                status_code=400,
            )
        # Resolve the Railway workspace BEFORE starting the deploy thread.
        # With multiple workspaces and none chosen, `railway init` (run with
        # captured output, i.e. non-interactive) fails after "Provisioning on
        # Railway…" with a message the UI never used to surface — fail fast
        # with an actionable error instead.
        workspace = payload.get("workspace") or None
        try:
            whoami = setup_mod._railway_check_auth()
        except (Exception, SystemExit) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        ws_names = [
            w["name"] for w in (whoami.get("workspaces") or []) if w.get("name")
        ]
        if workspace is None and len(ws_names) > 1:
            return JSONResponse(
                {"error": "your Railway account has multiple workspaces — "
                          f"pick one of: {', '.join(ws_names)}"},
                status_code=400,
            )
        if workspace is not None and ws_names and workspace not in ws_names:
            return JSONResponse(
                {"error": f"unknown Railway workspace {workspace!r} — "
                          f"pick one of: {', '.join(ws_names)}"},
                status_code=400,
            )

        initial_algorithms = setup_mod.read_initial_algorithms()
        challenges_cfg = setup_mod.collect_per_challenge_configs(
            initial_algorithms, use_defaults=True, challenge_set=challenge_set,
        )
        # Optional per-challenge instance overrides from the UI's "customize
        # instances" editor: {challenge: {track_key: count}}. Mirrors the CLI
        # wizard's non-defaults path — a challenge with an override gets its
        # tracks rebuilt from the submitted counts (seed track preserved);
        # unknown challenges/track keys and negative counts are rejected.
        overrides = payload.get("tracks") or {}
        for ch, track_counts in overrides.items():
            if ch not in challenges_cfg:
                return JSONResponse(
                    {"error": f"tracks override for unknown challenge {ch!r}"},
                    status_code=400,
                )
            valid_keys = set(challenge_set[ch]["track_keys"])
            new_tracks: dict = {"seed": "test"}
            for key, count in track_counts.items():
                if key not in valid_keys:
                    return JSONResponse(
                        {"error": f"unknown track {key!r} for {ch}"},
                        status_code=400,
                    )
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    n = -1
                if n < 0:
                    return JSONResponse(
                        {"error": f"invalid instance count for {ch}/{key}: {count!r}"},
                        status_code=400,
                    )
                new_tracks[key] = n
            challenges_cfg[ch]["tracks"] = new_tracks

        # Optional per-challenge solver-timeout overrides from the UI's
        # customize editor: {challenge: seconds}. Same validation posture as
        # the tracks overrides — unknown challenges and non-positive values
        # are rejected.
        timeout_overrides = payload.get("timeouts") or {}
        for ch, secs in timeout_overrides.items():
            if ch not in challenges_cfg:
                return JSONResponse(
                    {"error": f"timeout override for unknown challenge {ch!r}"},
                    status_code=400,
                )
            try:
                n = int(secs)
            except (TypeError, ValueError):
                n = -1
            if n < 1:
                return JSONResponse(
                    {"error": f"invalid timeout for {ch}: {secs!r}"},
                    status_code=400,
                )
            challenges_cfg[ch]["timeout"] = n

        params = {
            "swarm_name": payload.get("swarm_name", "my-tig-swarm"),
            "workspace": workspace,
            "swarm_type": swarm_type,
            "active_challenge": active_challenge,
            "challenges_cfg": challenges_cfg,
            "stagnation_threshold": int(payload.get("stagnation_threshold", 2)),
            "stagnation_limit": int(payload.get("stagnation_limit", 4)),
            "hypothesis_recall_threshold": int(payload.get("hypothesis_recall_threshold", 3)),
            "seed_inactive_pool": bool(payload.get("seed_inactive_pool", False)),
            "seed_pool_mainnet": bool(payload.get("seed_pool_mainnet", False)),
        }
        try:
            return deploy.start(params)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/local-api/swarm/create/status")
    def swarm_create_status() -> dict:
        return deploy.status()

    @app.post("/local-api/swarm/switch")
    async def swarm_switch(payload: dict) -> dict:
        challenge = payload.get("challenge", "")
        try:
            return setup_mod.switch_challenge(challenge)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # ── Seed pool: status + one-click re-seed ──
    # The seed pool lives ONLY in the swarm's DB and is written ONLY by
    # create's authored-seed deposit. A server DB reset (e.g. a redeploy onto
    # a non-persistent volume) re-applies config from env but leaves the pool
    # empty — so agents get the bare `unimplemented!` stub and nothing scores.
    # These let the host see the pool state and re-deposit without re-running
    # a full `create`.
    def _swarm_challenges(admin: dict) -> set[str] | None:
        chs = admin.get("challenges")
        return set(chs.keys()) if isinstance(chs, dict) and chs else None

    @app.get("/local-api/swarm/seed_status")
    def swarm_seed_status() -> dict:
        """Per-challenge seed-pool counts on the configured swarm server,
        alongside the authored seeds available locally to (re)deposit. A
        challenge whose count is 0 but which has an authored seed is the
        "empty pool" the UI warns about."""
        admin = setup_mod.read_swarm_admin()
        server_url = admin.get("server_url")
        key = admin.get("admin_key")
        only = _swarm_challenges(admin)
        authored: dict[str, list[str]] = {}
        for s in setup_mod.read_authored_seeds():
            if only is not None and s["challenge"] not in only:
                continue
            authored.setdefault(s["challenge"], []).append(s["strategy_tag"])
        pool_counts: dict[str, int | None] = {}
        if server_url and key:
            for ch in sorted(authored):
                try:
                    body = setup_mod.post_json(
                        f"{server_url.rstrip('/')}/api/admin/seeds",
                        {"admin_key": key, "challenge": ch}, timeout=10,
                    )
                    pool_counts[ch] = body.get("count", 0)
                except Exception:
                    pool_counts[ch] = None  # unreachable / old server
        return {
            "configured": bool(server_url and key),
            "authored": authored,
            "pool_counts": pool_counts,
            "empty": [ch for ch, tags in authored.items()
                      if tags and pool_counts.get(ch) == 0],
        }

    @app.post("/local-api/swarm/reseed")
    async def swarm_reseed(payload: dict) -> dict:
        """Re-deposit seeds into the swarm's seed pool. Always re-deposits the
        host's authored seeds (idempotent upsert by challenge+strategy_tag);
        with ``{"mainnet": true}`` ALSO deposits each challenge's top TIG
        mainnet algorithm (strategy_tag="mainnet"), so an existing swarm can
        gain a mainnet starting point without a full re-create."""
        admin = setup_mod.read_swarm_admin()
        server_url = admin.get("server_url")
        key = admin.get("admin_key")
        if not (server_url and key):
            return JSONResponse(
                {"error": "no swarm.admin.json with server_url + admin_key — "
                          "create or join a swarm first."},
                status_code=400,
            )
        only = _swarm_challenges(admin)
        seeds = [s for s in setup_mod.read_authored_seeds()
                 if only is None or s["challenge"] in only]
        want_mainnet = bool(payload.get("mainnet"))
        if not seeds and not want_mainnet:
            return JSONResponse(
                {"error": "no authored seeds found under "
                          "initial_algorithms/<challenge>/seeds/."},
                status_code=400,
            )
        try:
            failed = setup_mod.seed_pool_from_authored(server_url, key, seeds) if seeds else []
            mainnet_failed = []
            if want_mainnet:
                mainnet_failed = setup_mod.seed_pool_from_mainnet(
                    server_url, key, only or set())
            # Verify last, so mainnet deposits are covered too. `verified` is
            # False when the pool could not be read back at all — the UI must
            # not render that as a clean reseed.
            to_verify = list(seeds) + [
                {"challenge": ch, "strategy_tag": "mainnet"}
                for ch in sorted(only or set())
                if f"{ch}/mainnet" not in set(mainnet_failed)
            ]
            missing, verified = (
                setup_mod.verify_seed_pool(server_url, key, to_verify)
                if to_verify else ([], True)
            )
        except Exception as exc:
            return JSONResponse({"error": f"reseed failed: {exc}"}, status_code=502)
        return {
            "deposited": len(seeds) - len(failed),
            "total": len(seeds),
            "failed": failed,
            "missing": missing,
            "verified": verified,
            "mainnet": want_mainnet,
            "mainnet_failed": mainnet_failed,
        }

    # ── Local secrets (API keys) — no `export` needed ──
    @app.get("/local-api/secrets")
    def secrets_status() -> dict:
        """Which API-key env vars are set and where they win from (env vs the
        local secrets.local.json). Values are never returned."""
        return {"secrets": secrets_local.status()}

    @app.post("/local-api/secrets")
    async def secrets_set(payload: dict) -> dict:
        """Store (or clear, with an empty value) one API key locally. Keyed by
        environment-variable NAME so the runner injects it the same way an
        `export` would. Loopback-only surface (see the DNS-rebinding guard),
        so a plaintext value over localhost is acceptable — it lands in a 0600
        file, not the shell history an `export` leaves behind."""
        name = (payload.get("name") or "").strip()
        value = payload.get("value") or ""
        if not name or not name.replace("_", "").isalnum() or not name.isupper():
            return JSONResponse(
                {"error": "name must be an ENV_VAR_NAME (e.g. ANTHROPIC_API_KEY)"},
                status_code=400,
            )
        secrets_local.store(name, value)
        return {"ok": True, "secrets": secrets_local.status()}

    # ── Invite (host, local): derive per-contributor password ──
    @app.post("/local-api/invite")
    async def invite(payload: dict) -> dict:
        """Generate a contributor invite from the base swarm password.

        Same derivation as setup.py run_invite: sha256(username:base). Uses the
        base password from swarm.admin.json unless one is supplied. Backs the
        "Also run agents yourself" button on the host's done-screen, so a host
        can join their own swarm without a round-trip through a join link."""
        admin = setup_mod.read_swarm_admin()
        base = payload.get("swarm_password") or admin.get("swarm_password")
        server_url = payload.get("server_url") or admin.get("server_url")
        username = (payload.get("username") or "").strip()
        if not base:
            return JSONResponse({"error": "no base swarm_password available"}, status_code=400)
        if not username:
            return JSONResponse({"error": "username is required"}, status_code=400)
        if username in (admin.get("revoked_contributors") or []):
            # Re-deriving the same hash won't help: the server revokes by
            # username, not by hash. Same guard as hostadmin.run_invite.
            return JSONResponse(
                {"error": f"{username!r} is on the revoked list — pick a different name"},
                status_code=400,
            )
        derived = setup_mod.derive_password(username, base)
        # Without this the invite is invisible to `setup.py list`, which
        # cross-checks issued names against the server's joined ones.
        setup_mod.record_issued(admin, username)
        return {"server_url": server_url, "username": username,
                "swarm_password": derived,
                # One-link form of the same credentials (fragment-encoded so
                # they never reach server logs); None without a server_url.
                "join_link": setup_mod.build_join_link(server_url, username, derived)}

    # ── Proxy /api/* to the swarm's hosted server ──
    #
    # The Admin Console (served here at /admin/) makes same-origin /api/admin/*
    # calls. Those endpoints live on the *hosted* swarm server, not this
    # companion — so forward them there. This lets a host manage their swarm
    # from the local UI without deploying the admin bundle to the server or
    # wrestling with cross-origin CORS (the cross-origin hop happens here,
    # server-side). The target comes from swarm.admin.json / the local cache.
    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    async def proxy_api(path: str, request: Request) -> Response:
        admin = setup_mod.read_swarm_admin()
        base = admin.get("server_url") or setup_mod.resolve_server_url()
        if not base:
            return JSONResponse(
                {"error": "no swarm server_url known locally — create/join a swarm first"},
                status_code=502,
            )
        target = f"{base.rstrip('/')}/api/{path}"
        if request.url.query:
            target += f"?{request.url.query}"
        body = await request.body()
        req = urllib.request.Request(
            target,
            data=body if request.method == "POST" else None,
            method=request.method,
        )
        req.add_header("Content-Type", request.headers.get("content-type", "application/json"))

        def _fetch() -> tuple[int, bytes, str]:
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as e:  # forward the server's status + body
                return e.code, e.read(), e.headers.get("Content-Type", "application/json")

        try:
            status, data, ctype = await asyncio.to_thread(_fetch)
        except Exception as exc:  # network error reaching the swarm server
            return JSONResponse({"error": f"could not reach {base} ({exc})"}, status_code=502)
        return Response(content=data, status_code=status, media_type=ctype)

    # ── Live event stream ──
    @app.websocket("/local-api/stream")
    async def stream(ws: WebSocket) -> None:
        # The HTTP middleware above does NOT run for WebSocket handshakes, so
        # enforce the same DNS-rebinding guard here: loopback Host, and (for
        # browsers, which always send one) a loopback Origin. Otherwise any
        # web page could open ws://127.0.0.1:<port>/local-api/stream and read
        # the replayed event history.
        if not allow_remote and not (
            _host_is_loopback(ws.headers.get("host", ""))
            and _origin_is_loopback(ws.headers.get("origin", ""))
        ):
            await ws.close(code=1008)  # policy violation — reject pre-accept
            return
        await ws.accept()
        q = hub.subscribe()
        try:
            # Replay recent history so a late connector catches up.
            for event in hub.history():
                await ws.send_json(event)
            while True:
                event = await q.get()
                await ws.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(q)

    # ── Static bundle (must be last: catches all unmatched routes) ──
    if UI_DIST.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIST), html=True), name="ui")
    else:
        @app.get("/")
        def _placeholder() -> HTMLResponse:
            return HTMLResponse(
                "<h1>TIG Swarm Control</h1><p>The UI bundle isn't built yet. "
                "Run <code>cd control-ui &amp;&amp; npm install &amp;&amp; npm run build</code>, "
                "then reload.</p>",
                status_code=200,
            )

    return app


def _port_free(host: str, port: int) -> bool:
    """True when nothing is accepting connections on (host, port)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def _probe_companion(host: str, port: int) -> dict | None:
    """If the occupant of (host, port) is a TIG companion, return its
    /local-api/env payload; None for anything else (or on any error)."""
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/local-api/env", timeout=2
        ) as resp:
            body = json.load(resp)
        return body if isinstance(body, dict) and body.get("mode") == "local" else None
    except Exception:
        return None


def _read_companion_portfile() -> dict | None:
    try:
        body = json.loads(COMPANION_PORT_FILE.read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else None
    except (OSError, ValueError):
        return None


def _write_companion_portfile(host: str, port: int) -> None:
    try:
        COMPANION_PORT_FILE.write_text(
            json.dumps({"host": host, "port": port, "pid": os.getpid()}) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # purely quality-of-life; never block startup on it


def _clear_companion_portfile() -> None:
    """Remove the portfile on exit — but only if it still records THIS process
    (a younger companion may have overwritten it; its record must survive)."""
    body = _read_companion_portfile()
    if body and body.get("pid") == os.getpid():
        try:
            COMPANION_PORT_FILE.unlink()
        except OSError:
            pass


def _is_wsl() -> bool:
    """Running under the Windows Subsystem for Linux? The browser lives on the
    Windows side, so xdg-open (and every browser it probes for) is absent."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _open_browser_wsl(url: str) -> bool:
    """Hand the URL to Windows. Tries the openers in preference order and
    returns whether one of them took it."""
    # cmd.exe warns ("UNC paths are not supported") and falls back to C:\Windows
    # when its cwd is inside the Linux filesystem; starting it from /mnt/c keeps
    # that off the user's screen.
    cwd = "/mnt/c" if os.path.isdir("/mnt/c") else None
    candidates: list[tuple[list[str], bool]] = [
        # (argv, trust the exit code)
        (["wslview", url], True),  # wslu — the purpose-built one, if installed
        (["powershell.exe", "-NoProfile", "-NonInteractive",
          "-Command", "Start-Process", f"'{url}'"], True),
        (["cmd.exe", "/c", "start", "", url], True),
        # explorer.exe opens the URL and *then* exits 1 — a long-standing quirk.
        # Its exit code says nothing, so take the launch as success.
        (["explorer.exe", url], False),
    ]
    for argv, trust_rc in candidates:
        if shutil.which(argv[0]) is None:
            continue
        try:
            rc = subprocess.run(
                argv, cwd=cwd, timeout=15,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            continue
        if rc == 0 or not trust_rc:
            return True
    return False


def _open_browser(url: str) -> bool:
    """Best-effort browser launch, reporting whether it actually happened.

    webbrowser.open() is not enough on its own: under WSL it finds xdg-open,
    which probes ~16 Linux browsers that aren't installed, prints a wall of
    "not found" to stderr, and gives up — while webbrowser still reports
    success (it only checks that the child spawned). So route WSL to the
    Windows side, skip the attempt entirely where there's no GUI to open into,
    and keep xdg-open's spam off the terminal in the remaining cases.
    """
    if _is_wsl():
        return _open_browser_wsl(url)
    if sys.platform not in ("darwin", "win32") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False  # headless box / container — nothing to open into
    try:
        # The browser child inherits whatever fd 2 is at spawn time, so silence
        # it at the fd level. Nothing else writes to stderr here: this runs
        # before uvicorn starts, on the main thread.
        with open(os.devnull, "w") as devnull:
            saved = os.dup(2)
            try:
                os.dup2(devnull.fileno(), 2)
                return bool(webbrowser.open(url))
            finally:
                os.dup2(saved, 2)
                os.close(saved)
    except Exception:
        return False


def _announce_browser(url: str, no_browser: bool) -> None:
    """Open the tab, or tell the user to click the link themselves."""
    if no_browser:
        return
    if not _open_browser(url):
        print(f"  (couldn't open a browser automatically — visit {url} yourself)")


def _reopen_running(url: str, no_browser: bool) -> int:
    print(f"TIG Swarm Control is already running — opening {url}")
    print("  (Ctrl-C in ITS terminal stops it.)")
    _announce_browser(url, no_browser)
    return 0


def _same_root(cwd: object) -> bool:
    """Does a probed companion serve THIS repo checkout? Distinguishes 'reopen
    the one that's already running' from 'a companion for some other clone is
    on that port' (which must not be reused — different config, code)."""
    try:
        return Path(str(cwd)).resolve() == ROOT.resolve()
    except (OSError, ValueError):
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="Local control-plane UI for the TIG swarm.")
    p.add_argument("--host", default=None, help="Bind address (default: localhost).")
    p.add_argument("--port", type=int, default=None, help="Port (default: 8787).")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open a browser tab.")
    args = p.parse_args()

    # None defaults let us tell "user asked for this exact placement" from
    # "just open my companion" — only the latter may be redirected to an
    # already-running instance on some other port (portfile check below).
    explicit_placement = args.host is not None or args.port is not None
    host = args.host if args.host is not None else "127.0.0.1"

    # The user drives everything from the browser; this process's terminal may
    # exist but nobody is watching it. Tell secrets_local never to input() —
    # a missing key must fail fast with "add it in the Keys panel", not hang
    # the fleet launch on an invisible terminal prompt.
    os.environ["TIG_SWARM_NO_PROMPT"] = "1"

    # Serve a bundle that matches the sources — rebuild (or warn) BEFORE the
    # app mounts dist, or an edited control-ui silently ships the old UI.
    _freshen_ui_bundle()

    # Binding anything other than loopback exposes host credentials and fleet
    # controls to the network. Treat it as an explicit opt-out of the
    # DNS-rebinding/Host guard, and make the risk loud.
    is_loopback_bind = _host_is_loopback(host)

    # A companion for this checkout may already be live on a NON-default port
    # (started with --port, or fallen forward past a collision below). Its
    # portfile records where it actually listens — reopen THAT link instead of
    # starting a duplicate on 8787 and printing a URL the user's session isn't
    # on. Stale records (dead pid, foreign occupant) just fall through.
    if not explicit_placement:
        recorded = _read_companion_portfile()
        if recorded:
            r_host = str(recorded.get("host") or "127.0.0.1")
            probe_host = "127.0.0.1" if r_host in ("0.0.0.0", "::") else r_host
            r_port = recorded.get("port")
            if isinstance(r_port, int) and not _port_free(probe_host, r_port):
                occupant = _probe_companion(probe_host, r_port)
                if occupant and _same_root(occupant.get("cwd")):
                    return _reopen_running(
                        f"http://{probe_host}:{r_port}/", args.no_browser
                    )

    # Port collision handling — re-running the join one-liner must be
    # idempotent, not "[Errno 48] address already in use":
    #   * if the occupant is a companion serving THIS repo checkout, just
    #     reopen the browser at it and exit;
    #   * otherwise (another app, or a companion for a different checkout)
    #     fall forward to the next free port.
    port = args.port if args.port is not None else 8787
    if not _port_free(host, port):
        occupant = _probe_companion(host, port)
        if occupant and _same_root(occupant.get("cwd")):
            return _reopen_running(f"http://{host}:{port}/", args.no_browser)
        for candidate in range(port + 1, port + 21):
            if _port_free(host, candidate):
                print(f"  port {port} is in use — using {candidate} instead.")
                port = candidate
                break
        else:
            sys.exit(
                f"Ports {port}-{port + 20} are all in use. Free one "
                f"(macOS/Linux: `lsof -ti :{port} | xargs kill`) or pass --port."
            )

    url = f"http://{host}:{port}/"
    print(f"TIG Swarm Control — {url}")
    print("  (Ctrl-C stops the companion; a running fleet stops with it.)")
    if not is_loopback_bind:
        print(
            "  ⚠  WARNING: binding a non-loopback address exposes host "
            "credentials (admin_key, swarm_password) and fleet controls to "
            "anyone who can reach this host. The DNS-rebinding guard is "
            "DISABLED for this bind. Only do this on a trusted network."
        )
    _announce_browser(url, args.no_browser)

    _write_companion_portfile(host, port)
    try:
        uvicorn.run(
            create_app(allow_remote=not is_loopback_bind),
            host=host,
            port=port,
            log_level="warning",
        )
    finally:
        _clear_companion_portfile()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
