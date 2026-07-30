#!/usr/bin/env python3
"""Single contributor entry point for the TIG swarm demo.

One command per session (use `python` instead of `python3` on Windows):

    python3 run.py

Phases (each only runs when it has something to do):

  1. Preflight     - check `docker` is on PATH when any agent benchmarks
                     locally (compute "local" or omitted).
  2. Init wizard   - if fleet.config.json is missing.
  3. Tacit prompt  - ask whether to add/edit tacit knowledge (default No,
                     append-mode so existing notes are preserved).
  4. Launch fleet  - same logic as `python3 scripts/run_fleet.py`.
  5. Sync-back     - on shutdown, any `- LLM:` notes appended by the agent
                     are copied from the worktree back to the source file.

The underlying scripts still work for power-user / scripted flows:

    python3 scripts/init_fleet.py        # just the wizard
    python3 setup.py tacit [<name>]      # just the tacit wizard (append)
    python3 scripts/run_fleet.py --list  # fleet status
    python3 scripts/run_fleet.py --clean # tear down worktrees
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import init_fleet
import run_fleet
import hostadmin


def _ensure_ui_deps() -> None:
    """Make FastAPI + uvicorn importable for the web companion, installing them
    on first use so `python run.py --ui` is a SINGLE command (no separate
    `pip install` step). No-op once they're present — the common case after the
    first run.

    Two-stage install so it 'just works' regardless of how Python was obtained:
      1. Install into the current interpreter — but ONLY when that's a private
         environment (venv/conda) or a python.org install. A distro/Homebrew
         Python marks itself 'externally managed' per PEP 668 and refuses, so
         we skip straight to stage 2 rather than spew its error at the user.
      2. Create a project-local .venv, install there, and re-exec into it.
         Never touches system site.
    """
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    import os
    import importlib
    import shutil
    import subprocess
    import sysconfig

    req = str(ROOT / "control-ui-requirements.txt")
    print("First run: installing the web companion's dependencies…")

    def _has_pip(py) -> bool:
        """A venv built without ensurepip has bin/python but no pip — existence
        of the interpreter is not proof the environment is usable."""
        try:
            return subprocess.run(
                [str(py), "-m", "pip", "--version"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
        except OSError:
            return False

    def _externally_managed() -> bool:
        # PEP 668: the marker sits next to the stdlib. In a venv, pip is allowed
        # regardless — the venv's own prefix has no marker.
        if sys.prefix != sys.base_prefix:
            return False
        stdlib = sysconfig.get_path("stdlib")
        return bool(stdlib) and os.path.exists(os.path.join(stdlib, "EXTERNALLY-MANAGED"))

    try:
        # Stage 1 — current interpreter, when it's ours to install into.
        if not _externally_managed() and _has_pip(sys.executable):
            if subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", req]
            ).returncode == 0:
                importlib.invalidate_caches()
                try:
                    import fastapi  # noqa: F401
                    import uvicorn  # noqa: F401
                    return
                except ModuleNotFoundError:
                    pass  # installed somewhere not on this process's path — use a venv

        # Stage 2 — self-contained .venv + re-exec.
        venv_dir = ROOT / ".venv"
        bindir = "Scripts" if os.name == "nt" else "bin"
        vpy = venv_dir / bindir / ("python.exe" if os.name == "nt" else "python")
        if vpy.exists() and not _has_pip(vpy):
            # Half-built .venv from an earlier run that died in ensurepip. Left
            # in place it poisons every retry, so clear it and start over.
            print("Discarding an incomplete .venv from a previous run…")
            shutil.rmtree(venv_dir, ignore_errors=True)
        if not vpy.exists():
            if not _venv_capable() and not _offer_venv_package():
                _exit_needs_venv_package()
            print("Creating a local Python environment (.venv)…")
            proc = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)])
            if proc.returncode != 0 or not _has_pip(vpy):
                # venv can fail *after* creating bin/python — never leave the
                # husk behind, it's what poisons the next run.
                shutil.rmtree(venv_dir, ignore_errors=True)
                _exit_needs_venv_package()
        subprocess.run([str(vpy), "-m", "pip", "install", "-q", "-r", req], check=True)
        print("Relaunching inside the local environment…")
        # execv discards buffered stdout, which is block-buffered whenever
        # output is piped — without this the whole bootstrap runs silently.
        sys.stdout.flush()
        # Preserve the user's flags (--port/--host/--no-browser). main() has
        # already stripped --ui from sys.argv, so re-add it.
        os.execv(str(vpy), [str(vpy), str(ROOT / "run.py"), "--ui", *sys.argv[1:]])
    except (subprocess.CalledProcessError, OSError) as exc:
        sys.exit(
            f"Couldn't auto-install the companion's dependencies ({exc}).\n"
            f"Create an environment by hand and retry:\n"
            f"    python3 -m venv .venv\n"
            f"    .venv/bin/python -m pip install -r control-ui-requirements.txt\n"
            f"    .venv/bin/python run.py --ui"
        )


def _venv_package() -> str:
    """The apt package carrying ensurepip for THIS interpreter. Version-specific
    (python3.14-venv), since the unversioned metapackage tracks the distro's
    default python — which isn't necessarily the one running us."""
    return f"python{sys.version_info.major}.{sys.version_info.minor}-venv"


def _venv_capable() -> bool:
    """Can this interpreter actually build a venv? Asked in a subprocess so the
    answer stays true right after an apt install (no import-cache staleness)."""
    import subprocess
    try:
        return subprocess.run(
            [sys.executable, "-c", "import ensurepip"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def _offer_venv_package() -> bool:
    """Offer to apt-install the missing python3.X-venv, then report whether the
    interpreter can build a venv now.

    We ASK rather than just doing it: this is a system-wide package change, and
    sudo may block on a password prompt. Only ever offered on a TTY — a piped or
    CI run gets the printed instructions instead, because silently mutating
    system packages in an unattended context is not ours to decide.
    """
    import os
    import shutil as _shutil
    import subprocess

    if os.name != "posix" or not _shutil.which("apt-get"):
        return False  # not a Debian-ish box — no idea what to install
    if not sys.stdin.isatty():
        return False
    sudo = [] if os.geteuid() == 0 else (["sudo"] if _shutil.which("sudo") else None)
    if sudo is None:
        return False

    pkg = _venv_package()
    cmd = [*sudo, "apt-get", "install", "-y", pkg]
    print(
        f"\nPython here can't create virtual environments — Debian/Ubuntu split\n"
        f"the 'ensurepip' module into a separate package that isn't installed yet.\n"
        f"\n    {' '.join(cmd)}\n"
    )
    try:
        if input("Run that now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return False
    except EOFError:
        return False

    if subprocess.run(cmd).returncode != 0:
        # Commonly just a stale package index on a fresh box — the reason
        # `apt update && apt install` is the folk remedy. Try it once.
        print("Refreshing the package index and retrying…")
        subprocess.run([*sudo, "apt-get", "update"])
        subprocess.run(cmd)
    return _venv_capable()


def _exit_needs_venv_package() -> None:
    """Last resort when we couldn't install it ourselves (not Debian, no sudo,
    unattended run, or the user declined) — name the exact package rather than
    failing cryptically."""
    sys.exit(
        "\nPython here can't create virtual environments — Debian/Ubuntu split\n"
        "the 'ensurepip' module into a separate package that isn't installed yet.\n"
        f"\nInstall it, then retry:\n"
        f"\n    sudo apt install {_venv_package()}\n"
        f"    python3 run.py --ui"
    )


def _launch_ui() -> int:
    """`python run.py --ui` — open the local control-plane web UI instead of the
    terminal wizard. Delegates to control_server.py (the same companion a host
    would run). The CLI flow below is unchanged and still the default."""
    _ensure_ui_deps()
    import control_server
    return control_server.main()


def _apply_join_link(link: str) -> None:
    """`python run.py --join "<join-link>"` — one-command onboarding.

    A join link (`https://<server>/join#u=<username>&p=<password>`, issued by
    `setup.py invite` / the Admin Console) carries the server URL and the
    contributor's credentials. We write a minimal fleet.config.json that points
    at the server-hosted fleet plan (`config_source: server`); the agents are
    then authored in the swarm's web console and fetched at launch (see
    run_fleet._load_server_config). An existing local `agents` array is
    preserved — only the top-level credentials are refreshed.
    """
    import json
    import os
    import urllib.parse

    parsed = urllib.parse.urlsplit(link.strip())
    if not parsed.scheme or not parsed.netloc:
        sys.exit(
            "That doesn't look like a join link. Expected something like:\n"
            "    https://your-swarm.up.railway.app/join#u=you&p=<password>"
        )
    frag = urllib.parse.parse_qs(parsed.fragment)
    username = (frag.get("u") or [""])[0]
    password = (frag.get("p") or [""])[0]
    if not username or not password:
        sys.exit(
            "Join link is missing credentials (the #u=…&p=… part). Ask the "
            "host to resend the full link."
        )
    server_url = f"{parsed.scheme}://{parsed.netloc}"

    fleet_path = ROOT / "fleet.config.json"
    existing: dict = {}
    if fleet_path.exists():
        try:
            existing = json.loads(fleet_path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            existing = {}
    existing["server_url"] = server_url
    existing["username"] = username
    existing["swarm_password"] = password
    # Only default to server-hosted config when the contributor hasn't already
    # authored a local fleet — a hand-written agents array stays authoritative.
    if not existing.get("agents"):
        existing["config_source"] = "server"
    tmp = fleet_path.with_name(fleet_path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, fleet_path)
    print(f"Joined {server_url} as {username}.")
    # `--join <link> --ui` is the guided flow: credentials saved, then the
    # local setup app opens (configure agents + keys there, launch from it).
    if "--ui" in sys.argv:
        print("Saved fleet.config.json — opening the setup app in your browser.\n")
    else:
        print("Saved fleet.config.json — launching your fleet.\n")


def _tacit_phase(agents: list[dict], fleet_tacit: str | None) -> None:
    """Tacit-knowledge phase.

    Skipped entirely when stdin isn't a TTY — coding-agent / piped flows
    can't drive the interactive wizard (the guided capture asks six
    questions and the edit menu opens $EDITOR), so the right pattern
    there is for the assistant to write `tacit_knowledge.md` directly via
    its file-write tools before launching `run.py`.

    First-run interactive experience (no source file has real content
    yet): skip the y/N preamble and go straight to the create wizard —
    there's nothing to be "adding to" yet, so the question is just noise.

    Returning-contributor experience (at least one source file has real
    content): ask "Add or edit tacit knowledge? (y/N)" first. On yes, run
    the per-path wizard, which auto-picks the edit menu (with Open in
    $EDITOR, etc.) for files that already have content and the create
    menu for any that don't yet.
    """
    if not sys.stdin.isatty():
        return

    # Dedup by destination path: agents that share a source file (the
    # default) edit it once together.
    by_source: dict[Path, list[str]] = {}
    for agent in agents:
        src, _ = run_fleet._resolve_tacit_source(agent, fleet_tacit)
        by_source.setdefault(src, []).append(agent.get("name", "?"))

    stagnation_threshold = hostadmin.read_swarm_admin().get(
        "stagnation_threshold", 2,
    )

    any_existing = any(
        hostadmin._has_user_content(p) for p in by_source.keys()
    )

    if any_existing:
        try:
            answer = input(
                "\nAdd or edit tacit knowledge for your agent(s)? (y/N): "
            ).strip().lower()
        except EOFError:
            return
        if answer not in ("y", "yes"):
            return
    # else: skip the y/N — go straight to the create menu below.

    for tk_path, names in by_source.items():
        if len(names) > 1:
            print(
                f"\n=== Tacit knowledge — shared by: {', '.join(names)} ==="
            )
        elif len(agents) > 1:
            print(f"\n=== Tacit knowledge for agent: {names[0]} ===")

        if not tk_path.exists():
            tk_path.parent.mkdir(parents=True, exist_ok=True)
            tk_path.write_text(
                hostadmin.tacit_header(stagnation_threshold)
                + "- (replace this with your own hint, or run setup again)\n",
                encoding="utf-8",
            )
            try:
                shown = tk_path.relative_to(ROOT)
            except ValueError:
                shown = tk_path
            print(f"  created {shown} (gitignored)")

        hostadmin.gather_tacit_knowledge(
            tk_path, stagnation_threshold, append=True,
        )


def _gpu_local_preflight(agents: list[dict]) -> str | None:
    """When a local-compute agent is locked onto a GPU challenge, verify the
    host can actually pass a GPU into Docker. Returns an actionable message if
    it can't, else None.

    The plain docker-on-PATH check is not enough for GPU challenges:
    `benchmark.py` launches the GPU container with `--gpus all`, which needs
    the NVIDIA Container Toolkit wired into Docker. Without it the benchmark
    dies mid-run with `could not select device driver "" with capabilities:
    [[gpu]]`. This surfaces the same problem as a startup message instead.

    Best-effort and NVIDIA-focused (the only GPU path the benchmark uses). We
    gate on the locked challenge's `is_gpu` from .swarm-cache.json; before the
    first sync that flag is unknown, so we skip (the mid-run error still
    applies then, as before). A false pass just falls back to today's
    behavior — it never blocks a CPU fleet or a working GPU host.
    """
    uses_local = any((a.get("compute") or "local") == "local" for a in agents)
    if not uses_local:
        return None
    try:
        cached = json.loads((ROOT / ".swarm-cache.json").read_text())
        is_gpu = bool(cached.get("is_gpu"))
    except (OSError, ValueError):
        return None  # no locked challenge yet — can't tell; skip
    if not is_gpu:
        return None

    if shutil.which("nvidia-smi") is None:
        return (
            "This fleet has a local-compute agent on a GPU challenge, but no "
            "NVIDIA GPU is visible on this host (`nvidia-smi` not found).\n"
            "Run it on a machine with an NVIDIA GPU + driver, or switch the "
            "agent to C3 cloud compute (no local GPU needed)."
        )
    # GPU + driver present, but `--gpus all` also needs the NVIDIA Container
    # Toolkit wired into Docker; its absence is exactly what produces the
    # device-driver error, so check for the binaries it installs.
    has_toolkit = any(
        shutil.which(b) is not None
        for b in ("nvidia-ctk", "nvidia-container-runtime", "nvidia-container-cli")
    )
    if not has_toolkit:
        return (
            "This fleet has a local-compute agent on a GPU challenge and an "
            "NVIDIA GPU is present, but Docker can't pass it through: the "
            "NVIDIA Container Toolkit isn't installed.\nWithout it the "
            'benchmark fails with `could not select device driver "" with '
            "capabilities: [[gpu]]`.\n\nInstall it and wire it into Docker:\n"
            "  sudo apt-get install -y nvidia-container-toolkit\n"
            "  sudo nvidia-ctk runtime configure --runtime=docker\n"
            "  sudo systemctl restart docker\n"
            "(see NVIDIA's install guide for non-apt distros), or switch the "
            "agent to C3 cloud compute."
        )
    return None


def main() -> int:
    # `--join <link>` writes fleet.config.json from an invite link and then
    # launches — the one-command onboarding path. Consume the flag + its value
    # before anything else so the wizard/launch below sees a ready config.
    if "--join" in sys.argv:
        idx = sys.argv.index("--join")
        link = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not link:
            sys.exit("Usage: python run.py --join \"<join-link>\"")
        del sys.argv[idx:idx + 2]
        _apply_join_link(link)

    # `--ui` opens the web companion instead of the terminal flow. Strip the
    # flag so control_server's own argparse sees only its options (--port etc.).
    if "--ui" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--ui"]
        return _launch_ui()

    fleet_path = ROOT / "fleet.config.json"
    if not fleet_path.exists():
        print("No fleet.config.json found — running setup wizard.\n")
        rc = init_fleet.run_wizard(force=False)
        if rc != 0:
            return rc
    elif sys.stdin.isatty():
        # Only ask interactive contributors. Coding-agent / piped-stdin
        # callers want a deterministic launch and would have to answer No
        # blind anyway.
        try:
            ans = input(
                "\nUpdate your fleet config (provider / model / agent count)? (y/N): "
            ).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            rc = init_fleet.run_wizard(force=True)
            if rc != 0:
                return rc

    server_url, username, swarm_password, agents, fleet_tacit = (
        run_fleet._load_fleet()
    )

    # Preflight: agents on local compute (or with compute omitted, which
    # defaults to local) benchmark in Docker. The fleet can auto-start the
    # daemon but not conjure an install — fail now with an actionable message
    # instead of mid-benchmark. Same check the --ui companion performs.
    uses_local = any((a.get("compute") or "local") == "local" for a in agents)
    if uses_local and shutil.which("docker") is None:
        # Name the right product for the platform: Docker Desktop is a GUI app
        # for Mac/Windows, while Linux wants Docker Engine — pointing a headless
        # Linux box at the Desktop download is a dead end. (control_server.py has
        # the same hint for the --ui path; it can't be shared, since this CLI is
        # stdlib-only and that module pulls in FastAPI.)
        if sys.platform.startswith("linux"):
            how = ("Install Docker Engine with:\n"
                   "    curl -fsSL https://get.docker.com | sudo sh\n"
                   "    sudo systemctl enable --now docker\n"
                   "(or run `python run.py --ui` and install it from the wizard)")
        else:
            how = ("Install Docker Desktop "
                   "(https://www.docker.com/products/docker-desktop/) and start it")
        print(
            "This fleet has agents on local compute, but Docker isn't "
            "installed.\n" + how + ",\nor switch those agents to C3 cloud "
            "compute (no local Docker needed).",
            file=sys.stderr,
        )
        return 1

    # GPU challenges additionally need the NVIDIA Container Toolkit so Docker
    # can pass a GPU into the benchmark container (`--gpus all`). Catch a
    # missing toolkit / GPU now instead of mid-benchmark.
    gpu_problem = _gpu_local_preflight(agents)
    if gpu_problem:
        print(gpu_problem, file=sys.stderr)
        return 1

    try:
        _tacit_phase(agents, fleet_tacit)
    except KeyboardInterrupt:
        print("\n  tacit prompt cancelled — continuing to launch")

    print()
    return run_fleet.cmd_run(
        agents, only=None,
        server_url=server_url,
        username=username,
        swarm_password=swarm_password,
        fleet_tacit=fleet_tacit,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
