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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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

UI_DIST = ROOT / "control-ui" / "dist"
FLEET_CONFIG_PATH = ROOT / "fleet.config.json"
TACIT_PATH = ROOT / "tacit_knowledge.md"


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
        if self.is_running():
            raise RuntimeError("fleet is already running")
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
                "installed. Install Docker Desktop "
                "(https://www.docker.com/products/docker-desktop/) and start it, "
                "or switch those agents to C3 cloud compute (no local Docker needed)."
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
                      f"  it up (Ctrl-C to stop). It is done, not hung.\n",
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
        cmd = ["railway", "login", "--browserless"]
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


# ── Railway CLI install ────────────────────────────────────────────────

# Where the vendor installer (railway.com/install.sh) drops the binary. Its
# precedence is  --bin-dir > RAILWAY_BIN_DIR > $RAILWAY_HOME/bin > ~/.railway/bin;
# we never override, so the default ~/.railway/bin is what lands. The installer
# only patches shell rc files, so a companion already running won't see the new
# binary — we prepend these dirs to this process's PATH ourselves.
_RAILWAY_CANDIDATE_BINDIRS = (
    Path.home() / ".railway" / "bin",
    Path("/usr/local/bin"),
    Path.home() / ".local" / "bin",
)


def _ensure_railway_on_path() -> bool:
    """Make `railway` findable by this process, and report whether it is.

    Handles both the post-install case (the installer wrote ~/.railway/bin but
    that dir isn't on the companion's PATH) and the pre-existing case (railway
    installed there, companion launched from a shell that never sourced it).
    Idempotent: prepends a candidate dir to os.environ['PATH'] only when it
    actually holds a railway binary and isn't already present."""
    if shutil.which("railway") is not None:
        return True
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in _RAILWAY_CANDIDATE_BINDIRS:
        exe = d / ("railway.exe" if os.name == "nt" else "railway")
        if exe.exists() and str(d) not in parts:
            os.environ["PATH"] = os.pathsep.join([str(d), *parts])
            parts = os.environ["PATH"].split(os.pathsep)
    return shutil.which("railway") is not None


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
        if os.name == "nt":
            self.state = "error"
            self.error = (
                "Automatic install isn't supported on Windows. Install the "
                "Railway CLI with one of:\n"
                "    scoop install railway\n"
                "    npm i -g @railway/cli\n"
                "then click Recheck."
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
            cmd = [
                "bash", "-c",
                "curl -fsSL https://railway.com/install.sh | bash -s -- -y",
            ]
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
                self.error = (
                    "The installer exited without leaving a usable `railway` "
                    "on PATH. Install it manually (see railway.com/install.sh) "
                    "and click Recheck."
                    if proc.returncode == 0
                    else f"installer exited {proc.returncode} — see the log below."
                )

        self._thread = threading.Thread(target=_watch, daemon=True)
        self._thread.start()
        return self.status()


# ── App factory ────────────────────────────────────────────────────────


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
                matters; `running` is extra signal.
    """
    docker_installed = shutil.which("docker") is not None
    c3_installed = shutil.which("c3") is not None
    return {
        "docker": {
            "installed": docker_installed,
            "running": docker_installed and _cmd_ok(["docker", "info"]),
        },
        "c3": {
            "cli_installed": c3_installed,
            "key_in_env": bool(os.environ.get("C3_API_KEY")),
        },
        "git": {"installed": shutil.which("git") is not None},
        # Coding-agent CLIs used by the login-based providers (claude-code*,
        # codex-agentic). We can cheaply see if the binary is on PATH; login
        # state isn't reliably introspectable, so the UI advises `<cli> login`.
        "clis": {
            "claude": shutil.which("claude") is not None,
            "codex": shutil.which("codex") is not None,
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
        for a in agents:
            if not isinstance(a, dict) or not str(a.get("name", "")).strip():
                return JSONResponse({"error": "every agent needs a name"}, status_code=400)
            names.append(a["name"])
        if len(names) != len(set(names)):
            return JSONResponse({"error": "agent names must be unique"}, status_code=400)
        init_fleet.write_fleet_config(config)
        return {"config": config}

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

    @app.post("/local-api/railway/install")
    async def railway_install_start() -> dict:
        """Install the Railway CLI via the vendor script. Returns immediately;
        the UI polls the GET below until it exits, then re-reads status."""
        return railway_install.start()

    @app.get("/local-api/railway/install")
    async def railway_install_status() -> dict:
        return railway_install.status()

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
            missing = setup_mod.verify_seed_pool(server_url, key, seeds) if seeds else []
            mainnet_failed = []
            if want_mainnet:
                mainnet_failed = setup_mod.seed_pool_from_mainnet(
                    server_url, key, only or set())
        except Exception as exc:
            return JSONResponse({"error": f"reseed failed: {exc}"}, status_code=502)
        return {
            "deposited": len(seeds) - len(failed),
            "total": len(seeds),
            "failed": failed,
            "missing": missing,
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
        base password from swarm.admin.json unless one is supplied."""
        admin = setup_mod.read_swarm_admin()
        base = payload.get("swarm_password") or admin.get("swarm_password")
        server_url = payload.get("server_url") or admin.get("server_url")
        username = (payload.get("username") or "").strip()
        if not base:
            return JSONResponse({"error": "no base swarm_password available"}, status_code=400)
        if not username:
            return JSONResponse({"error": "username is required"}, status_code=400)
        derived = hashlib.sha256(f"{username}:{base}".encode()).hexdigest()
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
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost).")
    p.add_argument("--port", type=int, default=8787, help="Port (default: 8787).")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open a browser tab.")
    args = p.parse_args()

    # The user drives everything from the browser; this process's terminal may
    # exist but nobody is watching it. Tell secrets_local never to input() —
    # a missing key must fail fast with "add it in the Keys panel", not hang
    # the fleet launch on an invisible terminal prompt.
    os.environ["TIG_SWARM_NO_PROMPT"] = "1"

    # Binding anything other than loopback exposes host credentials and fleet
    # controls to the network. Treat it as an explicit opt-out of the
    # DNS-rebinding/Host guard, and make the risk loud.
    is_loopback_bind = _host_is_loopback(args.host)

    # Port collision handling — re-running the join one-liner must be
    # idempotent, not "[Errno 48] address already in use":
    #   * if the occupant is a companion serving THIS repo checkout, just
    #     reopen the browser at it and exit;
    #   * otherwise (another app, or a companion for a different checkout)
    #     fall forward to the next free port.
    port = args.port
    if not _port_free(args.host, port):
        occupant = _probe_companion(args.host, port)
        if occupant and _same_root(occupant.get("cwd")):
            existing = f"http://{args.host}:{port}/"
            print(f"TIG Swarm Control is already running — opening {existing}")
            print("  (Ctrl-C in ITS terminal stops it.)")
            if not args.no_browser:
                try:
                    webbrowser.open(existing)
                except Exception:
                    pass
            return 0
        for candidate in range(port + 1, port + 21):
            if _port_free(args.host, candidate):
                print(f"  port {port} is in use — using {candidate} instead.")
                port = candidate
                break
        else:
            sys.exit(
                f"Ports {port}-{port + 20} are all in use. Free one "
                f"(macOS/Linux: `lsof -ti :{port} | xargs kill`) or pass --port."
            )

    url = f"http://{args.host}:{port}/"
    print(f"TIG Swarm Control — {url}")
    print("  (Ctrl-C stops the companion; a running fleet stops with it.)")
    if not is_loopback_bind:
        print(
            "  ⚠  WARNING: binding a non-loopback address exposes host "
            "credentials (admin_key, swarm_password) and fleet controls to "
            "anyone who can reach this host. The DNS-rebinding guard is "
            "DISABLED for this bind. Only do this on a trusted network."
        )
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        create_app(allow_remote=not is_loopback_bind),
        host=args.host,
        port=port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
