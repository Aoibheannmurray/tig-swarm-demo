"""Re-provisioning an existing swarm must NOT rotate its credentials.

Why this exists: `create_swarm` used to generate `admin_key` /
`swarm_password` before it knew whether Railway was adopting an existing
project. Adoption preserves the /data volume — scores, trajectories and the
seed pool all survive — but the server re-asserts ADMIN_KEY / SWARM_PASSWORD
from env into its DB on every boot (server/db.py), and each contributor's
password is derived as sha256(username:base_password). So a re-provision left
the data intact while invalidating every invite ever issued, which to a host
looks indistinguishable from "my swarm was destroyed".

Self-running: `python scripts/test_adopt_credentials.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hostadmin import swarm as S  # noqa: E402

LIVE = {"ADMIN_KEY": "live-admin-key", "SWARM_PASSWORD": "live-swarm-pw"}
LOCAL_ADMIN = {
    "swarm_name": "my-swarm",
    "admin_key": "local-admin-key",
    "swarm_password": "local-swarm-pw",
}


def _run(*, resumed, railway_vars=None, admin_file=None, name="my-swarm"):
    orig_vars, orig_admin = S._railway_get_variables, S.read_swarm_admin
    S._railway_get_variables = lambda svc: dict(railway_vars or {})
    S.read_swarm_admin = lambda: dict(admin_file or {})
    lines: list[str] = []
    try:
        return S._resolve_swarm_credentials(
            name, resumed=resumed, emit=lines.append), lines
    finally:
        S._railway_get_variables, S.read_swarm_admin = orig_vars, orig_admin


def test_fresh_swarm_gets_new_secrets():
    (key, pw), _ = _run(resumed=False, railway_vars=LIVE, admin_file=LOCAL_ADMIN)
    assert key not in (LIVE["ADMIN_KEY"], LOCAL_ADMIN["admin_key"]), key
    assert pw not in (LIVE["SWARM_PASSWORD"], LOCAL_ADMIN["swarm_password"]), pw
    assert len(key) > 15 and len(pw) > 15
    assert key != pw
    print("PASS test_fresh_swarm_gets_new_secrets")


def test_adopted_swarm_reuses_railway_credentials():
    """THE REGRESSION: adoption must keep what the server is running with."""
    (key, pw), lines = _run(resumed=True, railway_vars=LIVE, admin_file={})
    assert key == LIVE["ADMIN_KEY"], key
    assert pw == LIVE["SWARM_PASSWORD"], pw
    assert any("reusing" in ln for ln in lines), lines
    print("PASS test_adopted_swarm_reuses_railway_credentials")


def test_railway_wins_over_a_stale_local_file():
    """Railway is what the running server actually booted with; a local
    swarm.admin.json can be stale (an older create, another machine)."""
    (key, pw), _ = _run(resumed=True, railway_vars=LIVE, admin_file=LOCAL_ADMIN)
    assert (key, pw) == (LIVE["ADMIN_KEY"], LIVE["SWARM_PASSWORD"])
    print("PASS test_railway_wins_over_a_stale_local_file")


def test_falls_back_to_local_admin_file():
    """Railway unreadable (API blip, older CLI) — the local file still holds
    the right secrets for this swarm."""
    (key, pw), lines = _run(resumed=True, railway_vars={}, admin_file=LOCAL_ADMIN)
    assert key == LOCAL_ADMIN["admin_key"], key
    assert pw == LOCAL_ADMIN["swarm_password"], pw
    assert any("swarm.admin.json" in ln for ln in lines), lines
    print("PASS test_falls_back_to_local_admin_file")


def test_admin_file_for_a_different_swarm_is_ignored():
    """A swarm.admin.json naming some OTHER swarm must never supply
    credentials — that would deploy the wrong swarm's key."""
    other = dict(LOCAL_ADMIN, swarm_name="someone-elses-swarm")
    (key, pw), lines = _run(resumed=True, railway_vars={}, admin_file=other)
    assert key != other["admin_key"], key
    assert pw != other["swarm_password"], pw
    assert any("WARNING" in ln for ln in lines), lines
    print("PASS test_admin_file_for_a_different_swarm_is_ignored")


def test_unrecoverable_generates_but_warns_loudly():
    """Nothing to recover from: generate rather than hard-fail (a failed create
    leaves the host stuck), but the rotation must be stated, with the fix."""
    (key, pw), lines = _run(resumed=True, railway_vars={}, admin_file={})
    assert key and pw and key != pw
    blob = "\n".join(lines)
    assert "WARNING" in blob, blob
    assert "invite" in blob, "must tell the host how to recover"
    print("PASS test_unrecoverable_generates_but_warns_loudly")


def test_partial_railway_vars_do_not_half_apply():
    """Only ADMIN_KEY readable: we must not pair a live admin key with a freshly
    generated password and call it a reuse."""
    (key, pw), lines = _run(
        resumed=True, railway_vars={"ADMIN_KEY": "live-admin-key"}, admin_file={})
    assert key == "live-admin-key", key          # keep what we could recover
    assert pw != ""                               # generated
    assert any("WARNING" in ln for ln in lines), lines
    print("PASS test_partial_railway_vars_do_not_half_apply")


if __name__ == "__main__":
    test_fresh_swarm_gets_new_secrets()
    test_adopted_swarm_reuses_railway_credentials()
    test_railway_wins_over_a_stale_local_file()
    test_falls_back_to_local_admin_file()
    test_admin_file_for_a_different_swarm_is_ignored()
    test_unrecoverable_generates_but_warns_loudly()
    test_partial_railway_vars_do_not_half_apply()
    print("ALL PASS")
