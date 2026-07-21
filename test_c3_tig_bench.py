"""Focused tests for the metered C3 build_so rollout path."""

import tempfile
from pathlib import Path

import c3_tig_bench as bench


def _fake_monorepo(root: Path) -> None:
    (root / "Cargo.toml").write_text("[workspace]\n")
    (root / "Cargo.lock").write_text("")
    (root / "scripts").mkdir()
    (root / "scripts" / "download_algorithm").write_text("#!/bin/sh\n")
    for crate in bench.WORKSPACE_CRATES:
        path = root / crate
        path.mkdir()
        (path / "placeholder").write_text(crate)


def test_stage_workspace_build_so_is_opt_in() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "source"
        source.mkdir()
        _fake_monorepo(source)
        optimized = root / "optimized"
        optimized.write_text("#!/bin/bash\necho optimized\n")

        official_stage = root / "official"
        official_stage.mkdir()
        bench.stage_workspace(official_stage, source)
        assert not (official_stage / "build_so.llsplit").exists()

        optimized_stage = root / "optimized-stage"
        optimized_stage.mkdir()
        bench.stage_workspace(optimized_stage, source, optimized)
        assert (optimized_stage / "build_so.llsplit").read_text() == optimized.read_text()


def test_runner_installs_canary_and_records_build_time() -> None:
    script = bench.runner_script(
        ["candidate"], [], "track", 1, "test", 100_000, 1,
        "hypergraph", "c005",
    )
    assert "install -m 0755 build_so.llsplit tig-binary/scripts/build_so" in script
    assert 'c3-artifacts/build-candidate.log' in script
    assert 'c3-artifacts/build-times.txt' in script


def test_benchmark_result_captures_fuel_identity_fields() -> None:
    assert '"fuel_consumed": runtime_data.get("fuel_consumed")' in bench.BENCH_PY
    assert '"runtime_signature": runtime_data.get("runtime_signature")' in bench.BENCH_PY


def test_optimized_build_so_is_the_metered_default() -> None:
    assert bench.DEFAULT_BUILD_SO == "optimized"


if __name__ == "__main__":
    test_stage_workspace_build_so_is_opt_in()
    test_runner_installs_canary_and_records_build_time()
    test_benchmark_result_captures_fuel_identity_fields()
    test_optimized_build_so_is_the_metered_default()
    print("All c3_tig_bench tests passed.")
