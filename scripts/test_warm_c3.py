"""Tests for the warm-image C3 path (Dockerfile.warm + c3_compute warm staging).

Runs standalone (`python test_warm_c3.py` from the scripts dir) and is
pytest-compatible.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3_compute  # noqa: E402


def _clear_env():
    for k in ("TIG_C3_WARM_IMAGE", "TIG_C3_WARM_IMAGES", "TIG_DOCKERHUB"):
        os.environ.pop(k, None)


def test_warm_image_resolution():
    _clear_env()
    # Off by default.
    assert c3_compute._warm_c3_image({"challenge": "knapsack"}) is None
    # Explicit ref wins outright.
    assert c3_compute._warm_c3_image(
        {"c3_warm_image": "docker.io/me/custom:v1"}
    ) == "docker.io/me/custom:v1"
    # Opt-in bool derives the flavor image from the namespace.
    cfg = {"c3_warm_images": True, "tig_dockerhub": "somens"}
    assert c3_compute._warm_c3_image(cfg) == "docker.io/somens/tig-swarm-warm-cpu:latest"
    assert c3_compute._warm_c3_image({**cfg, "is_gpu": True}) == (
        "docker.io/somens/tig-swarm-warm-gpu:latest"
    )
    # Opt-in without a namespace defaults to the TIG Foundation's public one.
    assert c3_compute._warm_c3_image({"c3_warm_images": True}) == (
        "docker.io/tigfoundation/tig-swarm-warm-cpu:latest"
    )
    # Env forms.
    os.environ["TIG_C3_WARM_IMAGES"] = "1"
    os.environ["TIG_DOCKERHUB"] = "envns"
    assert c3_compute._warm_c3_image({}) == "docker.io/envns/tig-swarm-warm-cpu:latest"
    _clear_env()
    print("PASS test_warm_image_resolution")


def test_warm_workspace_stages_only_the_essentials():
    # Hermetic fake repo root: src/<ch>/algorithm is gitignored, so the REAL
    # ROOT has no algorithm on a fresh checkout (CI's python job doesn't seed).
    orig_root = c3_compute.ROOT
    with tempfile.TemporaryDirectory() as fake_root, \
            tempfile.TemporaryDirectory() as tmp:
        root = Path(fake_root)
        (root / "Cargo.toml").write_text("[package]\n")
        (root / "Cargo.lock").write_text("# lock\n")
        algo = root / "src" / "knapsack" / "algorithm"
        algo.mkdir(parents=True)
        (algo / "mod.rs").write_text("// algo\n")
        (algo / "helpers.rs").write_text("// sidecar\n")
        (root / "src" / "knapsack" / "mod.rs").write_text("// harness\n")
        (root / "src" / "lib.rs").write_text("// lib\n")
        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "benchmark.py").write_text("# bench\n")
        c3_compute.ROOT = root
        try:
            stage = Path(tmp)
            c3_compute._create_warm_workspace(
                stage,
                {"challenge": "knapsack", "tracks": {"seed": "test"}},
                "https://example.invalid",
            )
        finally:
            c3_compute.ROOT = orig_root
        assert (stage / "algorithm" / "mod.rs").exists()
        assert (stage / "algorithm" / "helpers.rs").exists()  # multi-file sidecars ship
        assert (stage / "scripts" / "benchmark.py").exists()
        assert (stage / "Cargo.toml").exists()
        assert (stage / "Cargo.lock").exists()
        assert (stage / ".swarm-cache.json").exists()
        # The crate source (harnesses) rides along so a stale cached warm
        # image on a C3 node still builds against the current harness — but
        # algorithm dirs are excluded (uploaded once via stage/algorithm).
        assert (stage / "src" / "lib.rs").exists()
        assert (stage / "src" / "knapsack" / "mod.rs").exists()
        assert not (stage / "src" / "knapsack" / "algorithm").exists()
    print("PASS test_warm_workspace_stages_only_the_essentials")


def test_warm_runner_injects_algorithm_and_uses_baked_target():
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        script = c3_compute._write_warm_c3_project(
            stage,
            {
                "challenge": "knapsack",
                "c3_hardware": "auto",
                "tig_user_id": "aoibheann (agent 69e67db9ffb3)",
            },
            "https://example.invalid",
            "00:10:00",
            "docker.io/somens/tig-swarm-warm-cpu:latest",
            seed="hpo-seed",
            hyperparameters='{"p": 1}',
        )
        runner = (stage / script).read_text()
        # Injects ONLY the algorithm dir into the baked crate.
        assert 'rm -rf "$APP/src/knapsack/algorithm"' in runner
        assert 'cp -r algorithm "$APP/src/knapsack/algorithm"' in runner
        # Guards against a non-warm image being configured by mistake.
        assert '[ ! -d "$APP/target/release" ]' in runner
        # cmp-guarded manifest overlay (no-drift case stays a pure cache hit).
        assert 'cmp -s Cargo.toml "$APP/Cargo.toml"' in runner
        # cmp-guarded crate-source overlay (stale cached image self-corrects),
        # ordered BEFORE the algorithm injection so the injected dir wins.
        assert runner.index('cmp -s "$f" "$APP/src/$f"') < runner.index(
            'rm -rf "$APP/src/knapsack/algorithm"')
        # No toolchain bootstrap in the warm runner.
        assert "apt-get" not in runner
        assert "rustup" not in runner
        # HPO + identity forwarding matches the full-source runner.
        assert 'export TIG_BENCH_SEED="hpo-seed"' in runner
        assert "export TIG_HYPERPARAMETERS=" in runner
        assert 'export TIG_USER_ID="aoibheann (agent 69e67db9ffb3)"' in runner

        c3_yaml = (stage / ".c3").read_text()
        assert 'image: "docker.io/somens/tig-swarm-warm-cpu:latest"' in c3_yaml
        assert 'hardware: "cpu-d3-4vcpu-16gb"' in c3_yaml
        assert 'requires_accelerator: "none"' in c3_yaml
    print("PASS test_warm_runner_injects_algorithm_and_uses_baked_target")


def _main():
    test_warm_image_resolution()
    test_warm_workspace_stages_only_the_essentials()
    test_warm_runner_injects_algorithm_and_uses_baked_target()
    print("\nAll warm-C3 tests passed.")


if __name__ == "__main__":
    _main()
