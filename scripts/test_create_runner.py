"""Tests for `setup.py create-runner` orchestration.

Stubs the Railway wrappers, health-wait, and set-runner so it verifies
run_create_runner's sequence + the variables it sets (custom Dockerfile path,
a valid Fernet secret, swarm URL + admin key) without touching Railway.

Self-running: `python scripts/test_create_runner.py`.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hostadmin import swarm as S  # noqa: E402
from hostadmin import contributors as C  # noqa: E402


def _stub(admin, *, healthy=True):
    cap = {"provision": None, "vars": None, "volume": None, "up": [],
           "domain_used": None, "set_runner": None}
    S._railway_check_installed = lambda: None
    S._railway_check_auth = lambda: {"email": "host@example.com"}
    S._pick_workspace = lambda user: None
    S.read_swarm_admin = lambda: dict(admin)
    S.read_swarm_cache = lambda: {}
    S._railway_provision = lambda name, ws: (
        cap.__setitem__("provision", (name, ws)) or ({}, {"name": name}, False))
    S._railway_set_variables = lambda svc, vars: cap.__setitem__("vars", (svc, dict(vars)))
    S._railway_add_volume = lambda svc, path: cap.__setitem__("volume", (svc, path))
    S._railway_up = lambda svc: cap["up"].append(svc)
    S._railway_domain = lambda svc: (cap.__setitem__("domain_used", svc)
                                     or "https://my-runner.up.railway.app")
    S._wait_for_server = lambda url, probe_path="/": healthy
    C.run_set_runner = lambda url: cap.__setitem__("set_runner", url) or 0
    return cap


def _args():
    return types.SimpleNamespace(runner_name=None, workspace=None)


def test_happy_path_sets_vars_and_enables_tab():
    from cryptography.fernet import Fernet
    admin = {"server_url": "https://swarm.up.railway.app", "admin_key": "adm-123",
             "swarm_name": "cool-swarm"}
    cap = _stub(admin)
    rc = S.run_create_runner(_args())
    assert rc == 0

    # Provisioned under <swarm>-runner.
    assert cap["provision"][0] == "cool-swarm-runner", cap["provision"]
    svc, vars = cap["vars"]
    # Custom Dockerfile so Railway builds the runner, not the server.
    assert vars["RAILWAY_DOCKERFILE_PATH"] == "runner/Dockerfile", vars
    # Swarm coordinates wired in.
    assert vars["COORDINATION_SERVER_URL"] == "https://swarm.up.railway.app"
    assert vars["RUNNER_ADMIN_KEY"] == "adm-123"
    # The generated secret is a real Fernet key the runner's vault can load.
    Fernet(vars["RUNNER_SECRET_KEY"].encode())
    # Persistent volume, deploy, and the swarm was pointed at the runner URL.
    assert cap["volume"] == ("cool-swarm-runner", "/data"), cap["volume"]
    assert cap["up"] == ["cool-swarm-runner"], cap["up"]
    assert cap["set_runner"] == "https://my-runner.up.railway.app", cap["set_runner"]
    print("PASS test_happy_path_sets_vars_and_enables_tab")


def test_requires_existing_swarm():
    cap = _stub({})  # no server_url / admin_key
    rc = S.run_create_runner(_args())
    assert rc == 1
    assert cap["provision"] is None, "must not provision without a swarm"
    print("PASS test_requires_existing_swarm")


def test_health_timeout_does_not_enable_tab():
    admin = {"server_url": "https://s", "admin_key": "k", "swarm_name": "s"}
    cap = _stub(admin, healthy=False)
    rc = S.run_create_runner(_args())
    assert rc == 1
    # Deployed, but the swarm was NOT pointed at a possibly-broken runner.
    assert cap["up"] == ["s-runner"], cap["up"]
    assert cap["set_runner"] is None, "must not set-runner when health check fails"
    print("PASS test_health_timeout_does_not_enable_tab")


def test_custom_runner_name():
    admin = {"server_url": "https://s", "admin_key": "k", "swarm_name": "s"}
    cap = _stub(admin)
    args = types.SimpleNamespace(runner_name="my-runner", workspace="team-ws")
    S.run_create_runner(args)
    assert cap["provision"] == ("my-runner", "team-ws"), cap["provision"]
    print("PASS test_custom_runner_name")


if __name__ == "__main__":
    test_happy_path_sets_vars_and_enables_tab()
    test_requires_existing_swarm()
    test_health_timeout_does_not_enable_tab()
    test_custom_runner_name()
    print("ALL PASS")
