"""Guards on the Rust toolchain/lint policy.

Rust 1.89 promoted `dangerous_implicit_autorefs` to deny-by-default, and the
build paths installed whatever stable rustup served that day — so a mainnet
import that compiled last week failed with 252 errors, mid-run, with no change
on our side. Pooled seeds and LLM-written code were exposed to the same thing.

Two mechanisms answer that, and BOTH have to reach every build path:

  * `.cargo/config.toml` caps lints at warn, so a future promotion is a
    warning rather than a broken swarm;
  * `rust-toolchain.toml` pins the compiler, so a score is reproducible and an
    upgrade is a decision.

The half that rots silently is C3: it stages a hand-listed set of files, so a
build config that is never copied means the cloud compiles under different
rules than the laptop — passing locally, failing remotely.

Self-running: `python scripts/test_toolchain_pin.py`.
"""

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import c3_compute  # noqa: E402

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def _pinned_channel() -> str:
    text = (ROOT / "rust-toolchain.toml").read_text()
    m = re.search(r'channel\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def test_lints_are_capped():
    cfg = ROOT / ".cargo" / "config.toml"
    check(cfg.exists(), ".cargo/config.toml exists at the workspace root")
    text = cfg.read_text()
    check("--cap-lints" in text and '"warn"' in text,
          "lints are capped at warn, covering lints that don't exist yet")


def test_toolchain_is_pinned():
    channel = _pinned_channel()
    check(bool(re.fullmatch(r"\d+\.\d+\.\d+", channel)),
          f"the toolchain is pinned to an exact version (got {channel!r})")
    # A minimal profile would leave `cargo fmt` / `cargo clippy` missing, which
    # agentic_backends explicitly allows agents to run in their worktrees.
    text = (ROOT / "rust-toolchain.toml").read_text()
    check("rustfmt" in text and "clippy" in text,
          "rustfmt + clippy are installed for the agentic sandbox")


def test_every_build_path_agrees_on_the_version():
    """Four entry points install Rust independently. If they disagree, the
    pin causes a toolchain DOWNLOAD inside each job instead of preventing
    drift — slower than before and just as unreproducible."""
    channel = _pinned_channel()
    major_minor = ".".join(channel.split(".")[:2])
    for name in ("Dockerfile.cpu", "Dockerfile.gpu", "Dockerfile.warm"):
        text = (ROOT / name).read_text()
        check(f"--default-toolchain {channel}" in text,
              f"{name} installs the pinned toolchain")
    check(c3_compute._DEFAULT_CPU_IMAGE.startswith(f"rust:{major_minor}"),
          f"C3's default CPU image matches the pin "
          f"(got {c3_compute._DEFAULT_CPU_IMAGE!r})")
    check(":1-" not in c3_compute._DEFAULT_CPU_IMAGE,
          "C3's image tag is exact, not a floating major")


def _fake_root(root: Path) -> None:
    """A minimal repo the workspace builders accept.

    Hermetic on purpose: `src/<ch>/algorithm` is gitignored, so the real ROOT
    has no algorithm on a fresh checkout and a test using it passes on a
    working tree and fails in CI."""
    (root / "Cargo.toml").write_text("[package]\n")
    (root / "Cargo.lock").write_text("# lock\n")
    (root / "requirements.txt").write_text("\n")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("// lib\n")
    (root / "src" / "knapsack").mkdir()
    (root / "src" / "knapsack" / "mod.rs").write_text("// harness\n")
    algo = root / "src" / "knapsack" / "algorithm"
    algo.mkdir()
    (algo / "mod.rs").write_text("// algo\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "benchmark.py").write_text("# bench\n")
    # The files under test.
    (root / ".cargo").mkdir()
    (root / ".cargo" / "config.toml").write_text(
        '[build]\nrustflags = ["--cap-lints", "warn"]\n')
    (root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.89.0"\n')


def test_c3_workspaces_carry_the_build_config():
    """The silent-rot case: staged by hand, so a new root file is invisible to
    C3 until someone adds it to the list."""
    cfg = {"challenge": "knapsack", "tracks": {}}
    orig_root = c3_compute.ROOT
    try:
        for builder, label in ((c3_compute._create_workspace, "full-source"),
                               (c3_compute._create_warm_workspace, "warm")):
            with tempfile.TemporaryDirectory() as fake, \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(fake)
                _fake_root(root)
                c3_compute.ROOT = root
                stage = Path(tmp)
                builder(stage, cfg, "https://swarm.example")
                check((stage / ".cargo" / "config.toml").exists(),
                      f"{label} C3 workspace carries .cargo/config.toml")
                check((stage / "rust-toolchain.toml").exists(),
                      f"{label} C3 workspace carries rust-toolchain.toml")
    finally:
        c3_compute.ROOT = orig_root

    # The warm path also overlays onto a baked image at job time; that script
    # must refresh both files or a stale image keeps its own build rules.
    runner = c3_compute._WARM_RUNNER if hasattr(c3_compute, "_WARM_RUNNER") else ""
    if not runner:
        src = (ROOT / "scripts" / "c3_compute.py").read_text()
        runner = src[src.find('cmp -s Cargo.toml'):][:1200]
    check(".cargo/config.toml" in runner and "rust-toolchain.toml" in runner,
          "the warm-image overlay refreshes both onto a stale baked image")


def test_missing_build_config_is_not_fatal():
    """An older clone may have neither file; a benchmark must still run."""
    orig = c3_compute.ROOT
    try:
        with tempfile.TemporaryDirectory() as fake, \
                tempfile.TemporaryDirectory() as tmp:
            c3_compute.ROOT = Path(fake)  # no .cargo/, no rust-toolchain.toml
            c3_compute._copy_build_config(Path(tmp))
            check(True, "a clone without the build config stages cleanly")
    except Exception as exc:
        check(False, f"missing build config must not raise ({exc})")
    finally:
        c3_compute.ROOT = orig


if __name__ == "__main__":
    print("lint policy")
    test_lints_are_capped()
    print("toolchain pin")
    test_toolchain_is_pinned()
    test_every_build_path_agrees_on_the_version()
    print("reaching every build path")
    test_c3_workspaces_carry_the_build_config()
    test_missing_build_config_is_not_fatal()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s)")
        sys.exit(1)
    print("all toolchain-pin checks passed")
