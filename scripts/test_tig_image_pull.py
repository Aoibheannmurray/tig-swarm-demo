"""Tests for benchmark._ensure_tig_image — the pull-first local image path.

Local-compute contributors should get the SAME published tig-bench-<challenge>
image the C3 path uses (pulled in seconds), not a from-source docker build
inside their first benchmark. The build remains the fallback for hosts the
registry doesn't cover (CI publishes linux/amd64 only) — and the pull must
pass an explicit --platform, because a plain pull of a single-arch manifest
"succeeds" on a mismatched host and explodes later with exec-format errors.

Self-running: `python scripts/test_tig_image_pull.py`.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark as B  # noqa: E402

CFG = {"challenge": "job_scheduling"}


class _Recorder:
    """Stub subprocess.run: scripted per-command results, records calls."""
    def __init__(self, results: dict[str, int]):
        self.results = results  # first-token-ish key -> returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        key = " ".join(cmd[:3]) if cmd[0] == "docker" else cmd[0]
        rc = self.results.get(key, 0)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="stubbed")


def _run(results):
    rec = _Recorder(results)
    orig = B.subprocess.run
    B.subprocess.run = rec
    try:
        B._ensure_tig_image("tig-custom-image-job_scheduling:0.0.6",
                            "job_scheduling", CFG)
    finally:
        B.subprocess.run = orig
    return rec.calls


def test_existing_image_is_left_alone():
    calls = _run({"docker image inspect": 0})
    assert len(calls) == 1, calls
    print("PASS test_existing_image_is_left_alone")


def test_pull_success_tags_and_skips_build():
    calls = _run({"docker image inspect": 1, "docker pull --platform": 0})
    kinds = [" ".join(c[:2]) for c in calls]
    assert kinds == ["docker image", "docker pull", "docker tag"], calls
    pull = calls[1]
    assert "--platform" in pull, pull
    plat = pull[pull.index("--platform") + 1]
    assert plat in ("linux/amd64", "linux/arm64"), pull
    assert pull[-1].startswith("docker.io/") and "tig-bench-job_scheduling" in pull[-1], pull
    # Re-tagged under the local name benchmark.py runs.
    assert calls[2][-1] == "tig-custom-image-job_scheduling:0.0.6", calls[2]
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


if __name__ == "__main__":
    test_existing_image_is_left_alone()
    test_pull_success_tags_and_skips_build()
    test_pull_failure_falls_back_to_build()
    test_custom_namespace_is_honored()
    print("ALL PASS")
