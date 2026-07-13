"""Tests for benchmark._ensure_tig_image — the pull-first local image path.

Local-compute contributors should get the SAME published tig-bench-<challenge>
image the C3 path uses (pulled in seconds), not a from-source docker build
inside their first benchmark. The build remains the fallback for hosts the
registry doesn't cover (CI publishes linux/amd64 only) — and the pull must
pass an explicit --platform, because a plain pull of a single-arch manifest
"succeeds" on a mismatched host and explodes later with exec-format errors.

Also covers _cleanup_stale_tig_images: baked images are 10–20GB each and
nothing else deletes them, so switching challenges must drop the other
challenges' images (and stale versions) before pulling — otherwise a few
switches fill the disk.

Self-running: `python scripts/test_tig_image_pull.py`.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark as B  # noqa: E402

CFG = {"challenge": "job_scheduling"}


class _Recorder:
    """Stub subprocess.run: scripted per-command results, records calls.

    `results` maps a first-token-ish key to a returncode, or to a
    (returncode, stdout) tuple for commands whose output matters.
    """
    def __init__(self, results: dict):
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        key = " ".join(cmd[:3]) if cmd[0] == "docker" else cmd[0]
        res = self.results.get(key, 0)
        rc, out = res if isinstance(res, tuple) else (res, "")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="stubbed")


def _run(results, cfg=CFG):
    rec = _Recorder(results)
    orig = B.subprocess.run
    B.subprocess.run = rec
    try:
        B._ensure_tig_image(f"tig-custom-image-{cfg['challenge']}:0.0.6",
                            cfg["challenge"], cfg)
    finally:
        B.subprocess.run = orig
    return rec.calls


def test_existing_image_is_left_alone():
    calls = _run({"docker image inspect": 0})
    kinds = [" ".join(c[:2]) for c in calls]
    # The stale-image sweep always runs first; with nothing stale it's a no-op.
    assert kinds == ["docker images", "docker image"], calls
    print("PASS test_existing_image_is_left_alone")


def test_pull_success_tags_and_skips_build():
    calls = _run({"docker image inspect": 1, "docker pull --platform": 0})
    kinds = [" ".join(c[:2]) for c in calls]
    assert kinds == ["docker images", "docker image", "docker pull", "docker tag"], calls
    pull = calls[2]
    assert "--platform" in pull, pull
    plat = pull[pull.index("--platform") + 1]
    assert plat in ("linux/amd64", "linux/arm64"), pull
    assert pull[-1].startswith("docker.io/") and "tig-bench-job_scheduling" in pull[-1], pull
    # Re-tagged under the local name benchmark.py runs.
    assert calls[3][-1] == "tig-custom-image-job_scheduling:0.0.6", calls[3]
    print("PASS test_pull_success_tags_and_skips_build")


def test_pull_failure_falls_back_to_build():
    calls = _run({"docker image inspect": 1, "docker pull --platform": 1})
    assert any(c[0] == "bash" and c[1].endswith("build_bench_image.sh")
               for c in calls), calls
    assert not any(c[:2] == ["docker", "tag"] for c in calls), calls
    print("PASS test_pull_failure_falls_back_to_build")


def test_custom_namespace_is_honored():
    img = B._published_tig_image({"challenge": "knapsack", "tig_dockerhub": "myorg"})
    assert img.startswith("docker.io/myorg/tig-bench-knapsack:"), img
    print("PASS test_custom_namespace_is_honored")


def test_stale_challenge_images_are_removed():
    listing = "\n".join([
        # current challenge at the pinned version — keep both names
        "tig-custom-image-job_scheduling:0.0.6",
        "danieltiagoadams/tig-bench-job_scheduling:0.0.6",
        # other challenges — stale
        "tig-custom-image-satisfiability:0.0.6",
        "danieltiagoadams/tig-bench-satisfiability:0.0.6",
        # current challenge at an OLD version — stale
        "tig-custom-image-job_scheduling:0.0.5",
        # not ours / dangling — never touched
        "ubuntu:24.04",
        "danieltiagoadams/tig-bench-knapsack:<none>",
    ])
    calls = _run({"docker images --format": (0, listing),
                  "docker image inspect": 0})
    removed = sorted(c[2] for c in calls if c[:2] == ["docker", "rmi"])
    assert removed == [
        "danieltiagoadams/tig-bench-satisfiability:0.0.6",
        "tig-custom-image-job_scheduling:0.0.5",
        "tig-custom-image-satisfiability:0.0.6",
    ], removed
    print("PASS test_stale_challenge_images_are_removed")


def test_cleanup_keeps_custom_namespace_image():
    cfg = {"challenge": "knapsack", "tig_dockerhub": "myorg"}
    listing = "\n".join([
        "myorg/tig-bench-knapsack:0.0.6",          # current, custom ns — keep
        "tig-custom-image-knapsack:0.0.6",         # current local name — keep
        "danieltiagoadams/tig-bench-knapsack:0.0.6",  # other ns copy — stale
    ])
    calls = _run({"docker images --format": (0, listing),
                  "docker image inspect": 0}, cfg=cfg)
    removed = [c[2] for c in calls if c[:2] == ["docker", "rmi"]]
    assert removed == ["danieltiagoadams/tig-bench-knapsack:0.0.6"], removed
    print("PASS test_cleanup_keeps_custom_namespace_image")


def test_cleanup_survives_listing_failure():
    # `docker images` failing must not block the benchmark path.
    calls = _run({"docker images --format": (1, ""),
                  "docker image inspect": 0})
    assert not any(c[:2] == ["docker", "rmi"] for c in calls), calls
    kinds = [" ".join(c[:2]) for c in calls]
    assert kinds == ["docker images", "docker image"], calls
    print("PASS test_cleanup_survives_listing_failure")


if __name__ == "__main__":
    test_existing_image_is_left_alone()
    test_pull_success_tags_and_skips_build()
    test_pull_failure_falls_back_to_build()
    test_custom_namespace_is_honored()
    test_stale_challenge_images_are_removed()
    test_cleanup_keeps_custom_namespace_image()
    test_cleanup_survives_listing_failure()
    print("ALL PASS")
