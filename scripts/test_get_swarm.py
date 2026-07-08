"""Tests for the no-clone contributor bootstrap deploy/get-swarm.py (P4).

Loads the hyphenated script by path and exercises its arg mapping + checkout
selection with git/exec stubbed, so nothing clones or launches.

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


def test_join_verb_maps_to_flag():
    gs = _load()
    fake_checkout = Path(tempfile.mkdtemp())
    captured = {}

    gs.ensure_checkout = lambda: fake_checkout
    gs.subprocess.call = lambda cmd, cwd=None: captured.update(cmd=cmd, cwd=cwd) or 0

    rc = gs.main(["join", "https://s/join#u=a&p=b"])
    assert rc == 0
    # `join <link>` → run.py --join <link>, run in the checkout.
    assert captured["cmd"][1].endswith("run.py"), captured["cmd"]
    assert captured["cmd"][2:] == ["--join", "https://s/join#u=a&p=b"], captured["cmd"]
    assert captured["cwd"] == str(fake_checkout)
    print("PASS test_join_verb_maps_to_flag")


def test_passthrough_args_and_empty():
    gs = _load()
    captured = {}
    gs.ensure_checkout = lambda: Path("/x")
    gs.subprocess.call = lambda cmd, cwd=None: captured.update(cmd=cmd) or 0

    gs.main(["--ui", "--port", "9000"])
    assert captured["cmd"][2:] == ["--ui", "--port", "9000"], captured["cmd"]
    # No args → usage error (return code 2), no checkout/exec attempted.
    assert gs.main([]) == 2
    print("PASS test_passthrough_args_and_empty")


if __name__ == "__main__":
    test_data_dir_is_namespaced()
    test_join_verb_maps_to_flag()
    test_passthrough_args_and_empty()
    print("ALL PASS")
