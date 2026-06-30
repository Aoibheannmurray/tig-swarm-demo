"""Unit tests for tier->role classification and a config default (F3, F7).

Self-running: `python server/test_tiers_config.py`.

F3: `tiers.classify_tier` (standard markers beat frontier; provider fallback)
    and `tiers.role_for_tier` (frontier->explorer, standard->exploiter).
F7: the `inactive_minutes` swarm default is 60 (raised from 20).
"""

import os
import sys
import tempfile


def test_classify_tier():
    sys.path.insert(0, os.path.dirname(__file__))
    import tiers
    # Frontier markers.
    assert tiers.classify_tier("anthropic", "claude-opus-4-7") == "frontier"
    assert tiers.classify_tier("anthropic", "claude-sonnet-4-6") == "frontier"  # sonnet-4
    assert tiers.classify_tier("openai", "gpt-5") == "frontier"
    assert tiers.classify_tier("google", "gemini-2.5-pro") == "frontier"
    # Standard markers (checked first — downgrade wins).
    assert tiers.classify_tier("anthropic", "claude-haiku-4-5") == "standard"
    assert tiers.classify_tier("openai", "gpt-4o-mini") == "standard"
    assert tiers.classify_tier("google", "gemini-2.5-flash") == "standard"
    assert tiers.classify_tier("anthropic", "claude-3.5-sonnet") == "standard"  # 3.5-sonnet
    # Unknown model -> provider fallback -> standard.
    assert tiers.classify_tier("openai", "gpt-4o") == "standard"
    assert tiers.classify_tier(None, None) == "standard"
    # Agentic CLI providers are frontier even with no model string.
    assert tiers.classify_tier("claude-code-agentic", None) == "frontier"
    assert tiers.classify_tier("codex-agentic", "") == "frontier"
    print("PASS test_classify_tier")


def test_role_for_tier():
    sys.path.insert(0, os.path.dirname(__file__))
    import tiers
    assert tiers.role_for_tier("frontier") == "explorer"
    assert tiers.role_for_tier("standard") == "exploiter"
    print("PASS test_role_for_tier")


def test_inactive_minutes_default_is_60():
    # Fresh server import (DATA_DIR before import, per repo test convention).
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    for mod in ("db", "server"):
        sys.modules.pop(mod, None)
    sys.path.insert(0, os.path.dirname(__file__))
    import server
    # Empty config -> effective swarm default.
    assert server.swarm_setting({}, "inactive_minutes") == 60, \
        server.swarm_setting({}, "inactive_minutes")
    print("PASS test_inactive_minutes_default_is_60")


if __name__ == "__main__":
    test_classify_tier()
    test_role_for_tier()
    test_inactive_minutes_default_is_60()
    print("\nAll tiers/roles/config tests passed.")
