"""Tests for the no-clone contributor bootstrap deploy/get-swarm.py.

Loads the hyphenated script by path and exercises its arg mapping, --branch
handling, and checkout selection with git/exec stubbed, so nothing clones or
launches.

Self-running: `python scripts/test_get_swarm.py`.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "get_swarm", ROOT / "deploy" / "get-swarm.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_data_dir_is_namespaced():
    gs = _load()
    assert gs._data_dir().name == "tig-swarm", gs._data_dir()
    print("PASS test_data_dir_is_namespaced")


def test_join_verb_maps_to_flag_and_ui_passes_through():
    gs = _load()
    fake_checkout = Path(tempfile.mkdtemp())
    captured = {}

    gs.ensure_checkout = lambda branch="": captured.update(branch=branch) or fake_checkout
    gs.subprocess.call = lambda cmd, cwd=None: captured.update(cmd=cmd, cwd=cwd) or 0

    rc = gs.main(["join", "https://s/join#u=a&p=b", "--ui"])
    assert rc == 0
    # `join <link> --ui` → run.py --join <link> --ui, run in the checkout.
    assert captured["cmd"][1].endswith("run.py"), captured["cmd"]
    assert captured["cmd"][2:] == ["--join", "https://s/join#u=a&p=b", "--ui"], captured["cmd"]
    assert captured["cwd"] == str(fake_checkout)
    assert captured["branch"] == "", captured
    print("PASS test_join_verb_maps_to_flag_and_ui_passes_through")


def test_branch_flag_is_stripped_and_forwarded():
    gs = _load()
    captured = {}
    gs.ensure_checkout = lambda branch="": captured.update(branch=branch) or Path("/x")
    gs.subprocess.call = lambda cmd, cwd=None: captured.update(cmd=cmd) or 0

    rc = gs.main(["join", "https://s/join#u=a&p=b", "--ui", "--branch", "server-onboarding"])
    assert rc == 0
    assert captured["branch"] == "server-onboarding", captured
    # --branch must NOT leak through to run.py.
    assert "--branch" not in captured["cmd"], captured["cmd"]
    assert captured["cmd"][2:] == ["--join", "https://s/join#u=a&p=b", "--ui"], captured["cmd"]

    # Dangling --branch is a usage error.
    assert gs.main(["join", "x", "--branch"]) == 2
    print("PASS test_branch_flag_is_stripped_and_forwarded")


def test_env_var_branch_still_works():
    import os
    gs = _load()
    captured = {}
    gs.ensure_checkout = lambda branch="": captured.update(branch=branch) or Path("/x")
    gs.subprocess.call = lambda cmd, cwd=None: 0
    os.environ["TIG_SWARM_BRANCH"] = "from-env"
    try:
        gs.main(["--ui"])
        assert captured["branch"] == "from-env", captured
    finally:
        del os.environ["TIG_SWARM_BRANCH"]
    print("PASS test_env_var_branch_still_works")


def test_passthrough_args_and_empty():
    gs = _load()
    captured = {}
    gs.ensure_checkout = lambda branch="": Path("/x")
    gs.subprocess.call = lambda cmd, cwd=None: captured.update(cmd=cmd) or 0

    gs.main(["--ui", "--port", "9000"])
    assert captured["cmd"][2:] == ["--ui", "--port", "9000"], captured["cmd"]
    # No args → usage error (return code 2), no checkout/exec attempted.
    assert gs.main([]) == 2
    print("PASS test_passthrough_args_and_empty")


def test_ascii_only_for_windows_pipes():
    # PowerShell re-encodes piped text; a non-ASCII byte in the script can
    # corrupt in transit on legacy code pages. Keep it pure ASCII.
    raw = (ROOT / "deploy" / "get-swarm.py").read_bytes()
    raw.decode("ascii")  # raises on failure
    print("PASS test_ascii_only_for_windows_pipes")


if __name__ == "__main__":
    test_data_dir_is_namespaced()
    test_join_verb_maps_to_flag_and_ui_passes_through()
    test_branch_flag_is_stripped_and_forwarded()
    test_env_var_branch_still_works()
    test_passthrough_args_and_empty()
    test_ascii_only_for_windows_pipes()
    print("ALL PASS")
