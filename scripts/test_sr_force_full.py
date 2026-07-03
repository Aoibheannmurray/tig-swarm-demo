"""Test that _generate_code(force_full=True) bypasses the search/replace path.

This is the core of the S/R no-edit-skip fix: when an S/R agent stalls (the model
keeps returning no blocks), the loop forces a full rewrite so it produces
something and publishes, instead of skipping forever without advancing the
server's stagnation reset.

Self-running: `python scripts/test_sr_force_full.py`.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_loop


class _FakeFiles:
    entry_name = "mod.rs"

    def read_files(self):
        # Multi-file -> _use_search_replace would normally pick S/R.
        return {"mod.rs": "use super::*;\n", "helpers.rs": "pub fn h() {}\n"}

    def parse_response(self, text):
        return text, ""

    def describe_parse(self, parsed, kernel):
        return "parsed"

    def separator_suffix(self):
        return ""


def _run(force_full):
    """Call _generate_code with everything mocked; return (code, sr_called)."""
    calls = {"sr": False}

    # Force the S/R decision ON, so only force_full can bypass it.
    run_loop._use_search_replace = lambda role, fm, cfg: True

    def _fake_sr(*a, **k):
        calls["sr"] = True
        return "SR_CODE", None, 0, 0
    run_loop._generate_code_search_replace = _fake_sr

    run_loop._call_llm_logged = lambda *a, **k: ("FULL_CODE", {"input_tokens": 1, "output_tokens": 1})
    run_loop.validate_code = lambda code, cfg: None
    run_loop.build_code_system_prompt = lambda *a, **k: "sys"
    run_loop.build_code_user_prompt = lambda *a, **k: "user"

    args = types.SimpleNamespace(provider="claude-code", api_base=None)
    code, kernel, ti, to = run_loop._generate_code(
        args, "m", "k", {}, {}, {"challenge": "vehicle_routing"}, "md",
        _FakeFiles(), role="exploiter", force_full=force_full,
    )
    return code, calls["sr"]


def test_default_uses_search_replace():
    code, sr_called = _run(force_full=False)
    assert sr_called and code == "SR_CODE", (code, sr_called)
    print("PASS test_default_uses_search_replace")


def test_force_full_bypasses_search_replace():
    code, sr_called = _run(force_full=True)
    assert not sr_called, "force_full must NOT call the search/replace path"
    assert code == "FULL_CODE", code
    print("PASS test_force_full_bypasses_search_replace")


def test_skip_fallback_threshold_is_sane():
    assert isinstance(run_loop._SR_SKIP_FALLBACK, int) and run_loop._SR_SKIP_FALLBACK >= 1
    print("PASS test_skip_fallback_threshold_is_sane")


if __name__ == "__main__":
    test_default_uses_search_replace()
    test_force_full_bypasses_search_replace()
    test_skip_fallback_threshold_is_sane()
    print("\nS/R force-full fallback tests passed.")
