"""Multi-fleet supervisor for the hosted runner (P3).

Generalizes control_server.py's single-foreground-fleet `FleetManager` into a
keyed supervisor: one fleet per enrolled contributor, each isolated in its own
working copy so their git worktrees and `fleet/<name>` branches never collide.

Isolation strategy: `git clone --local` the runner's repo checkout into
`RUNNER_WORKSPACES/<username>/`. A local clone shares objects via hardlinks
(cheap) but gives each contributor a distinct repo root — so the existing
run_fleet machinery works unchanged per workspace, with no naming refactor.

The launcher is injected (`Launcher` protocol) so the service uses the real
`ProcessLauncher` while tests drive a fake — the lifecycle logic is identical.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Protocol

RUNNER_REPO_ROOT = Path(os.environ.get("RUNNER_REPO_ROOT", Path(__file__).parent.parent))
WORKSPACES_DIR = Path(
    os.environ.get("RUNNER_WORKSPACES", Path(__file__).parent / "workspaces")
)
_LOG_RING = 500  # per-fleet log lines retained for the console stream


class FleetHandle:
    """A running (or finished) fleet: the child process, its workspace, and a
    bounded log ring the service streams to the contributor console."""

    def __init__(self, username: str, workspace: Path):
        self.username = username
        self.workspace = workspace
        self.proc: subprocess.Popen | None = None
        self.log: collections.deque[str] = collections.deque(maxlen=_LOG_RING)
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Launcher(Protocol):
    def start(self, username: str, config: dict, keys: dict[str, str]) -> FleetHandle: ...
    def stop(self, handle: FleetHandle) -> None: ...


class ProcessLauncher:
    """Real launcher: isolated clone + `python run.py` subprocess per fleet."""

    def _prepare_workspace(self, username: str, config: dict, keys: dict[str, str]) -> Path:
        WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
        workspace = WORKSPACES_DIR / username
        if not (workspace / ".git").exists():
            subprocess.run(
                ["git", "clone", "--local", "--no-hardlinks" if os.name == "nt" else "--shared",
                 str(RUNNER_REPO_ROOT), str(workspace)],
                check=True, capture_output=True, text=True,
            )
        # Complete fleet.config.json — agents inline so the child never needs
        # to re-fetch (and never needs the contributor's password for that).
        # config_source is intentionally absent: the plan is authoritative here.
        fleet_cfg = {k: v for k, v in config.items()}
        (workspace / "fleet.config.json").write_text(
            json.dumps(fleet_cfg, indent=2) + "\n", encoding="utf-8",
        )
        # Decrypted keys land in the workspace's own gitignored secrets store,
        # which run_fleet reads (env still wins, but the runner sets none).
        secrets_path = workspace / "secrets.local.json"
        tmp = secrets_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, secrets_path)
        return workspace

    def start(self, username: str, config: dict, keys: dict[str, str]) -> FleetHandle:
        workspace = self._prepare_workspace(username, config, keys)
        handle = FleetHandle(username, workspace)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Keys go through the workspace secrets file, NOT the child env, so a
        # process listing / crash dump on the runner never exposes them.
        for name in ("C3_API_KEY", *(k for k in keys)):
            env.pop(name, None)
        handle.proc = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=str(workspace), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )

        def _pump() -> None:
            assert handle.proc and handle.proc.stdout
            for line in handle.proc.stdout:
                handle.log.append(line.rstrip("\n"))

        handle._thread = threading.Thread(target=_pump, daemon=True)
        handle._thread.start()
        return handle

    def stop(self, handle: FleetHandle) -> None:
        if handle.proc and handle.proc.poll() is None:
            handle.proc.terminate()
            try:
                handle.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                handle.proc.kill()


class Supervisor:
    """Thread-safe registry of running fleets keyed by contributor username."""

    def __init__(self, launcher: Launcher | None = None):
        self._launcher = launcher or ProcessLauncher()
        self._fleets: dict[str, FleetHandle] = {}
        self._lock = threading.Lock()

    def start(self, username: str, config: dict, keys: dict[str, str]) -> None:
        """Start (or restart) a contributor's fleet. Idempotent: an existing
        running fleet is stopped first so config/key updates take effect."""
        with self._lock:
            existing = self._fleets.pop(username, None)
            if existing:
                self._launcher.stop(existing)
            self._fleets[username] = self._launcher.start(username, config, keys)

    def stop(self, username: str) -> bool:
        """Stop a contributor's fleet. Returns False if none was running."""
        with self._lock:
            handle = self._fleets.pop(username, None)
        if handle is None:
            return False
        self._launcher.stop(handle)
        return True

    def is_running(self, username: str) -> bool:
        with self._lock:
            handle = self._fleets.get(username)
        return bool(handle and handle.running())

    def logs(self, username: str) -> list[str]:
        with self._lock:
            handle = self._fleets.get(username)
        return list(handle.log) if handle else []

    def running_usernames(self) -> list[str]:
        with self._lock:
            return [u for u, h in self._fleets.items() if h.running()]

    def stop_all(self) -> None:
        with self._lock:
            handles = list(self._fleets.values())
            self._fleets.clear()
        for handle in handles:
            self._launcher.stop(handle)
