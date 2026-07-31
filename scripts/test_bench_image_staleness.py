"""The local Docker image must rebuild when its build inputs change.

`_ensure_docker_image` only checked EXISTENCE, so the image was built once on
first run and reused forever — editing Dockerfile.cpu, requirements.txt or the
toolchain pin changed nothing until someone manually removed it.

That went from stale to expensive when rust-toolchain.toml arrived: an image
baked against a different rustc still works, but rustup honours the pin by
DOWNLOADING the pinned toolchain inside the container, on every benchmark.
Seen in a live fleet log as `syncing channel updates for 1.89.0` +
`downloading 6 components` before each build.

Self-running: `python scripts/test_bench_image_staleness.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark  # noqa: E402

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def test_digest_covers_every_input_that_changes_the_image():
    base = benchmark._image_inputs_digest("Dockerfile.cpu")
    check(bool(base), "a digest is produced")
    check(base == benchmark._image_inputs_digest("Dockerfile.cpu"),
          "it is stable when nothing changes")
    check(base != benchmark._image_inputs_digest("Dockerfile.gpu"),
          "cpu and gpu images are distinguished")

    for rel in ("rust-toolchain.toml", ".cargo/config.toml",
                "requirements.txt", "Dockerfile.cpu"):
        path = ROOT / rel
        if not path.exists():
            continue
        orig = path.read_bytes()
        try:
            path.write_bytes(orig + b"\n# touched\n")
            moved = benchmark._image_inputs_digest("Dockerfile.cpu")
        finally:
            path.write_bytes(orig)
        check(moved != base, f"a change to {rel} triggers a rebuild")
    check(benchmark._image_inputs_digest("Dockerfile.cpu") == base,
          "the digest returns to its original value once files are restored")


def test_missing_inputs_do_not_crash():
    """An older clone may have no toolchain pin; benchmarking must still run."""
    try:
        d = benchmark._image_inputs_digest("Dockerfile.does-not-exist")
        check(bool(d), "an absent input hashes as absent rather than raising")
    except Exception as exc:
        check(False, f"missing inputs must not raise ({exc})")


def test_the_image_is_stamped_and_compared():
    """Existence alone must no longer be the check."""
    import inspect
    src = inspect.getsource(benchmark._ensure_docker_image)
    # The source references the constant by NAME, not by its value.
    check("--label" in src and "_IMAGE_INPUT_LABEL" in src,
          "the build stamps the digest onto the image")
    check(".Config.Labels" in src,
          "the next run compares against the stamped label")
    check("stale" in src.lower(), "a rebuild says why it is rebuilding")


if __name__ == "__main__":
    print("image input digest")
    test_digest_covers_every_input_that_changes_the_image()
    test_missing_inputs_do_not_crash()
    print("staleness check")
    test_the_image_is_stamped_and_compared()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("all image-staleness checks passed")
