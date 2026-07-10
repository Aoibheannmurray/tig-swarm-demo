"""Tests for deriving the fleet C3 pool size from the LIVE subscription cap.

Runs standalone (`python3 test_c3_plan_cap.py` from the scripts dir) — no
network / no C3. `_query_c3_plan_cap` fetches from the C3 control plane, so only
its pure selection logic (`_select_concurrency_limit`) is unit-tested here,
against the real `/v2/billing/subscription` and `/v2/billing/tiers` payloads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_fleet


# Real /v2/billing/tiers payload shape (trimmed).
_TIERS = {"tiers": [
    {"tier": "free", "display_name": "Free", "concurrency_limit": 3, "unit": "chips"},
    {"tier": "pro", "display_name": "Pro", "concurrency_limit": 10, "unit": "chips"},
    {"tier": "team", "display_name": "Team", "concurrency_limit": 50, "unit": "chips"},
]}


def _sub(tier, **extra):
    # /v2/billing/subscription omits concurrency_limit unless the account overrides.
    return {"tier": tier, "display_name": tier.title(), "is_trial": False, **extra}


def test_current_tier_limit_from_catalog():
    assert run_fleet._select_concurrency_limit(_sub("free"), _TIERS) == 3
    assert run_fleet._select_concurrency_limit(_sub("pro"), _TIERS) == 10
    assert run_fleet._select_concurrency_limit(_sub("team"), _TIERS) == 50
    print("PASS test_current_tier_limit_from_catalog")


def test_account_override_wins():
    # A per-account concurrency_limit on the subscription beats the tier default.
    assert run_fleet._select_concurrency_limit(_sub("team", concurrency_limit=7), _TIERS) == 7
    # A non-positive/absent override falls through to the tier.
    assert run_fleet._select_concurrency_limit(_sub("pro", concurrency_limit=0), _TIERS) == 10
    print("PASS test_account_override_wins")


def test_case_insensitive_tier_match():
    assert run_fleet._select_concurrency_limit(_sub("TEAM"), _TIERS) == 50
    assert run_fleet._select_concurrency_limit(_sub(" Pro "), _TIERS) == 10
    print("PASS test_case_insensitive_tier_match")


def test_missing_or_unknown_returns_none():
    assert run_fleet._select_concurrency_limit(_sub("enterprise"), _TIERS) is None  # not in catalog
    assert run_fleet._select_concurrency_limit({"tier": ""}, _TIERS) is None
    assert run_fleet._select_concurrency_limit({}, _TIERS) is None
    assert run_fleet._select_concurrency_limit(_sub("team"), {}) is None            # empty catalog
    assert run_fleet._select_concurrency_limit(_sub("team"), {"tiers": []}) is None
    print("PASS test_missing_or_unknown_returns_none")


def test_bad_catalog_values_return_none():
    bad = {"tiers": [{"tier": "team", "concurrency_limit": "lots"}]}
    assert run_fleet._select_concurrency_limit(_sub("team"), bad) is None
    bad_neg = {"tiers": [{"tier": "team", "concurrency_limit": -1}]}
    assert run_fleet._select_concurrency_limit(_sub("team"), bad_neg) is None
    print("PASS test_bad_catalog_values_return_none")


def _main():
    test_current_tier_limit_from_catalog()
    test_account_override_wins()
    test_case_insensitive_tier_match()
    test_missing_or_unknown_returns_none()
    test_bad_catalog_values_return_none()
    print("\nAll C3 plan-cap tests passed.")


if __name__ == "__main__":
    _main()
