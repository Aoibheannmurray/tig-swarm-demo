"""Railway CLI helpers — provisioning, deploys, and RailwayError.

Moved verbatim from the root setup.py ("Railway CLI helpers" section)."""

from __future__ import annotations

import json
import shutil
import subprocess as sp
import time
import urllib.request

from .config_io import ROOT
from .prompting import prompt_choice


class RailwayError(RuntimeError):
    """A Railway CLI step failed (missing binary, bad auth, rejected mutation,
    repeated transient timeouts). Raised by the _railway_* helpers so
    programmatic callers (control_server.py's deploy worker) surface the
    failure instead of dying on SystemExit; the CLI entry point catches it
    and exits 2 as before."""


_RAILWAY_INSTALL_HINT = (
    "Install one of these, then re-run:\n"
    "    bash <(curl -fsSL cli.new)        # vendor installer (any OS with bash)\n"
    "    npm i -g @railway/cli             # if you have node\n"
    "    brew install railway              # macOS\n"
    "    cargo install railwayapp --locked # rust\n"
)


# Substrings that mark a *transient* Railway failure — the GraphQL API
# (backboard.railway.com) momentarily unreachable or slow — as opposed to a
# real rejection (bad auth, name taken, build error). Matched case-insensitively
# against combined stderr+stdout to decide whether a retry is worth attempting.
_RAILWAY_TRANSIENT_MARKERS = (
    "operation timed out",
    "error sending request for url",
    "failed to fetch",
    "connection reset",
    "connection refused",
    "temporary failure in name resolution",
    "503 service",
    "502 bad gateway",
    "gateway time-out",
)


def _railway_is_transient(result: sp.CompletedProcess) -> bool:
    blob = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
    return any(m in blob for m in _RAILWAY_TRANSIENT_MARKERS)


def _railway_run(
    *args: str, check: bool = True, retries: int = 0, backoff: int = 4
) -> sp.CompletedProcess:
    """Run `railway <args>` and capture output. Exit on non-zero unless check=False.

    With `retries > 0`, transient network failures (the Railway GraphQL API
    momentarily timing out — see `_RAILWAY_TRANSIENT_MARKERS`) are retried with
    linear backoff (`backoff`, `2*backoff`, … seconds) before giving up. Real
    rejections (bad auth, duplicate name, build failure) are never retried.

    Only pass `retries` for IDEMPOTENT calls. A timed-out Railway *mutation*
    frequently still lands server-side, so blindly retrying a create (`init` /
    `add --service`) would spawn a duplicate project/service — exactly the
    orphans this repo's create flow now resumes instead. Reads (`whoami`,
    `list`, `domain`) and upserts (`variable set`, `volume add`) are safe."""
    for attempt in range(retries + 1):
        try:
            result = sp.run(
                ["railway", *args],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
        except FileNotFoundError:
            raise RailwayError(
                "Railway CLI not found in PATH.\n" + _RAILWAY_INSTALL_HINT
            ) from None
        if result.returncode == 0:
            return result
        if attempt < retries and _railway_is_transient(result):
            wait = backoff * (attempt + 1)
            print(
                f"  railway {args[0] if args else ''} hit a transient network "
                f"error; retrying in {wait}s ({attempt + 1}/{retries})…"
            )
            time.sleep(wait)
            continue
        break
    if check and result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise RailwayError(f"railway {' '.join(args)} failed: {msg}")
    return result


def _railway_check_installed() -> None:
    if shutil.which("railway") is None:
        raise RailwayError("Railway CLI not found in PATH.\n" + _RAILWAY_INSTALL_HINT)


def _railway_check_auth() -> dict:
    """Return whoami JSON, or raise RailwayError telling the user to
    `railway login`."""
    result = _railway_run("whoami", "--json", check=False, retries=3)
    if result.returncode != 0:
        raise RailwayError(
            "Not logged in to Railway. Run this in another terminal, complete the\n"
            "browser flow, then re-run `python setup.py create`:\n"
            "    railway login"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _pick_workspace(whoami: dict) -> str | None:
    """Return a workspace name (or None for single/no workspace).

    `railway init --json` requires `--workspace` when the user has more
    than one. Surface a prompt so the wizard can route the new project to
    the right workspace."""
    workspaces = whoami.get("workspaces") or []
    if len(workspaces) <= 1:
        return None
    names = [w.get("name", "") for w in workspaces if w.get("name")]
    if not names:
        return None
    print("\nMultiple Railway workspaces found. Pick one for this swarm:")
    return prompt_choice("  workspace", names, default=names[0])


def _railway_init_project(name: str, workspace: str | None = None) -> dict | None:
    """Create the project. Returns the project dict on success, or None when a
    transient timeout aborts the CLI mid-create — the mutation usually still
    lands server-side, so the caller resumes by re-querying rather than
    re-running init (which would spawn a duplicate). A real rejection (bad
    auth, name taken) still hard-exits."""
    args = ["init", "-n", name, "--json"]
    if workspace:
        args += ["--workspace", workspace]
    result = _railway_run(*args, check=False)
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"name": name}
    if _railway_is_transient(result):
        print("  railway init hit a transient network error mid-create.")
        return None
    msg = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    raise RailwayError(f"railway {' '.join(args)} failed: {msg}")


def _railway_add_service(name: str) -> dict | None:
    """Add the service to the linked project. Returns the service dict on
    success, or None on a transient timeout (caller re-queries / retries).
    Real rejections hard-exit."""
    result = _railway_run("add", "--service", name, "--json", check=False)
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"name": name}
    if _railway_is_transient(result):
        print("  railway add --service hit a transient network error mid-create.")
        return None
    msg = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    raise RailwayError(f"railway add --service {name} failed: {msg}")


def _railway_wait_for_project(name: str, workspace: str | None) -> dict | None:
    """Poll `railway list` for a project a timed-out init may have landed.

    Railway often takes a moment to surface a project created by a CLI call
    that itself timed out. Re-query a few times with backoff before giving up;
    each `_railway_find_project` already retries the underlying `list`."""
    for attempt in range(4):
        time.sleep(4 * (attempt + 1))
        found = _railway_find_project(name, workspace)
        if found is not None:
            return found
    return None


def _railway_ensure_service(name: str, workspace: str | None) -> dict:
    """Add the service, recovering from a transient timeout in the same run.

    On a timeout the add may have landed server-side, so re-query the project
    and adopt the service if it's there; otherwise retry the add (confirmed
    absent → no duplicate). Hard-exits only after repeated timeouts."""
    service = _railway_add_service(name)
    if service is not None:
        return service
    for attempt in range(4):
        time.sleep(4 * (attempt + 1))
        proj = _railway_find_project(name, workspace)
        svc = _railway_service_in_project(proj, name) if proj else None
        if svc:
            print(f"  adopted service created by the timed-out add: {svc.get('name', name)}")
            return svc
        service = _railway_add_service(name)
        if service is not None:
            return service
    raise RailwayError(
        "railway add --service timed out repeatedly; re-run "
        "`python setup.py create` to resume once the Railway API recovers."
    )


def _railway_find_project(name: str, workspace: str | None) -> dict | None:
    """Return the live (non-deleted) project named `name`, or None.

    A timed-out `railway init` usually creates the project server-side even
    though the CLI errors out, so on a re-run we adopt that project rather
    than spawning a duplicate. Projects already scheduled for deletion
    (`deletedAt` set — e.g. earlier empty orphans) are skipped. When
    `workspace` is given, projects in it win; ties break on most-recent
    update."""
    result = _railway_run("list", "--json", check=False, retries=4)
    try:
        projects = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(projects, list):
        return None
    matches = [
        p for p in projects
        if p.get("name") == name and not p.get("deletedAt")
    ]
    if workspace:
        scoped = [
            p for p in matches
            if (p.get("workspace") or {}).get("name") == workspace
        ]
        if scoped:
            matches = scoped
    if not matches:
        return None
    matches.sort(
        key=lambda p: p.get("updatedAt") or p.get("createdAt") or "",
        reverse=True,
    )
    return matches[0]


def _railway_project_environment(project: dict) -> str:
    """Pick the environment to link to — prefer `production` (the init default)."""
    edges = (project.get("environments") or {}).get("edges") or []
    names = [(e.get("node") or {}).get("name") for e in edges]
    names = [n for n in names if n]
    if "production" in names:
        return "production"
    return names[0] if names else "production"


def _railway_service_in_project(project: dict, name: str) -> dict | None:
    """Return the service node named `name` within `project`, or None."""
    edges = (project.get("services") or {}).get("edges") or []
    for e in edges:
        node = e.get("node") or {}
        if node.get("name") == name:
            return node
    return None


def _railway_provision(name: str, workspace: str | None) -> tuple[dict, dict, bool]:
    """Idempotently ensure the project + service exist and are linked to cwd.

    Fresh run → `railway init` + `railway add --service`. If a create step
    times out mid-flight (common when the Railway API is flaky), the mutation
    usually still lands server-side — so instead of re-running it in place
    (which would spawn a duplicate), we re-query and adopt the orphaned
    project/service in the SAME run, then `railway link` it. A later plain
    re-run resumes the same way. Returns (project, service, resumed)."""
    existing = _railway_find_project(name, workspace)
    if existing is None:
        project = _railway_init_project(name, workspace=workspace)
        if project is not None:
            print(f"  project: {project.get('name', name)}")
            service = _railway_ensure_service(name, workspace)
            print(f"  service: {service.get('name', name)}")
            return project, service, False
        # init timed out mid-create — the project usually lands server-side
        # anyway. Re-query and adopt it rather than re-running init (which
        # would create a duplicate). Falls through to the resume path below.
        existing = _railway_wait_for_project(name, workspace)
        if existing is None:
            raise RailwayError(
                f"railway init timed out and no '{name}' project "
                f"appeared server-side; re-run `python setup.py create` once "
                f"the Railway API recovers."
            )
        print(f"  init timed out but project '{name}' landed server-side; adopting it.")

    # Resume: adopt the project a prior run (or a timed-out init above) created.
    env = _railway_project_environment(existing)
    svc_node = _railway_service_in_project(existing, name)
    link_args = ["link", "--project", existing["id"], "--environment", env]
    if workspace:
        link_args += ["--workspace", workspace]
    if svc_node:
        link_args += ["--service", svc_node["name"]]
    _railway_run(*link_args, retries=4)
    print(f"  adopted existing project: {existing.get('name', name)}")
    if svc_node:
        print(f"  adopted existing service: {svc_node.get('name', name)}")
        return existing, svc_node, True
    # Project existed but the service didn't land — add it now (resumably).
    service = _railway_ensure_service(name, workspace)
    print(f"  service: {service.get('name', name)}")
    return existing, service, True


def _railway_set_variables(service: str, vars: dict[str, str]) -> None:
    args = ["variable", "set", "--service", service, "--skip-deploys"]
    for k, v in vars.items():
        args.append(f"{k}={v}")
    _railway_run(*args, retries=4)


def _railway_get_variables(service: str) -> dict[str, str]:
    """Read a service's environment variables back from Railway.

    This is how an adopted swarm recovers its OWN credentials: Railway holds
    the authoritative `ADMIN_KEY` / `SWARM_PASSWORD` that the running server
    boots with, so re-provisioning can reuse them instead of rotating them out
    from under every contributor. It works even when the host lost
    `swarm.admin.json` or is re-provisioning from a different machine.

    A read, so retries are safe. Returns {} on any failure — the caller must
    treat "couldn't read" as "don't assume", never as "no credentials"."""
    result = _railway_run(
        "variable", "list", "--service", service, "--json",
        check=False, retries=4,
    )
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _railway_add_volume(service: str, mount_path: str) -> None:
    """Create a persistent volume mounted at `mount_path`.

    The volume attaches to the linked service in `.railway/config.json`
    (set by the preceding `railway add --service`). `volume add` doesn't
    accept `--service`; we rely on the link being correct.

    `volume add` is the one non-idempotent step — it bails if a volume is
    already mounted on the linked service. Treat that as success."""
    result = _railway_run("volume", "add", "--mount-path", mount_path, check=False, retries=4)
    if result.returncode == 0:
        return
    err = (result.stderr or "").lower()
    if "already" in err and "mount" in err:
        print(f"    volume already mounted at {mount_path}; skipping")
        return
    msg = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    raise RailwayError(f"railway volume add failed: {msg}")


def _ensure_upload_snapshot_targets() -> None:
    """Make sure `railway up` can pack the repo into its upload snapshot.

    `server/static` is a git-tracked symlink to `../dashboard/dist` (so a
    locally-run server serves the built dashboard). On a fresh clone the
    dashboard has never been built, the symlink dangles, and the Railway
    CLI's snapshot packer aborts with "IO error ... No such file or
    directory (os error 2)". An empty dir is enough — the Docker build
    compiles the real dashboard inside the image."""
    dist = ROOT / "dashboard" / "dist"
    if not dist.exists():
        print(f"  creating empty {dist.relative_to(ROOT)}/ (server/static symlink target)")
        dist.mkdir(parents=True, exist_ok=True)


def _railway_up(service: str) -> None:
    """Deploy. --ci streams build logs and blocks until SUCCESS / FAILED."""
    _ensure_upload_snapshot_targets()
    # Inherit stdout/stderr so the user sees build logs as they stream.
    result = sp.run(
        ["railway", "up", "--service", service, "--ci"],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise RailwayError("railway up failed. Check the build logs above.")


def _railway_domain(service: str) -> str:
    """Get (or generate) the public URL for `service`. Idempotent."""
    result = _railway_run("domain", "--service", service, "--json", retries=4)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RailwayError(
            f"couldn't parse `railway domain` output: {result.stdout!r}"
        ) from None
    if isinstance(data, dict):
        if isinstance(data.get("domain"), str):
            return _ensure_https(data["domain"])
        domains = data.get("domains")
        if isinstance(domains, list) and domains:
            first = domains[0]
            if isinstance(first, str):
                return _ensure_https(first)
            if isinstance(first, dict) and isinstance(first.get("domain"), str):
                return _ensure_https(first["domain"])
    raise RailwayError(f"railway domain returned no usable URL: {data!r}")


def _ensure_https(domain: str) -> str:
    if domain.startswith(("http://", "https://")):
        return domain.rstrip("/")
    return f"https://{domain}".rstrip("/")


def _wait_for_server(
    url: str, timeout: int = 240, probe_path: str = "/api/swarm_config",
) -> bool:
    """Poll <url><probe_path> until it answers *stably* or timeout passes.

    `railway up --ci` returns when the build succeeds, but the container's
    health-rollout lags and DNS/TLS for a brand-new public domain can take a
    while — a GPU image with the CUDA toolchain is slow to its first byte. We
    therefore wait generously (default 4 min) and require two consecutive 200s
    a couple seconds apart, so we don't hand off to the config push the instant
    a transient/rolling container first answers. `probe_path` lets the runner
    deploy poll its own health endpoint instead of the server's config."""
    deadline = time.time() + timeout
    probe = f"{url.rstrip('/')}{probe_path}"
    ok = 0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=5) as r:
                if r.status == 200:
                    ok += 1
                    if ok >= 2:
                        return True
                    time.sleep(2)
                    continue
        except Exception:
            ok = 0
        time.sleep(3)
    return False
