"""Model tier classification for the swarm.

A model's *tier* (`frontier` vs `standard`) is auto-derived from its name at
registration. Tier drives **seeding only**: standard (smaller/weaker) models are
handed a working seed algorithm on a fresh trajectory instead of the bare
`unimplemented!()` stub, because they frequently can't bootstrap a complete
algorithm from nothing. Frontier models keep the stub and bootstrap as before.

Tier is intentionally independent of an agent's *role* (explorer/exploiter),
which is contributor-owned and changes live — see scripts/run_loop.py.

Classification is deliberately conservative: anything we can't confidently call
frontier defaults to `standard`. The safe failure mode is "a frontier model gets
seeded" (slightly reduced exploration), never "a weak model gets the stub and
fails to produce anything."
"""

# Checked FIRST, so a "downgrade" marker always wins over a frontier marker.
# This is what makes `claude-3.5-sonnet`, `o3-mini`, `gemini-2.5-flash`, etc.
# resolve to `standard` even though they contain a frontier substring.
#
# The size markers are hyphen-anchored ("-mini", not "mini") so they match real
# suffixes like `gpt-4o-mini` / `o3-mini` / `gpt-5-nano` without colliding with
# substrings of larger names — notably "mini" inside "ge**mini**-2.5-pro".
STANDARD_MARKERS = (
    "-mini",
    "flash",
    "haiku",
    "-small",
    "-lite",
    "-nano",
    "-8b",
    "3.5-sonnet",
)

# Checked SECOND. Sonnet-class and up. `sonnet-4` matches `claude-sonnet-4-6`
# but not `claude-3.5-sonnet` (caught by STANDARD_MARKERS first). `gpt-5`
# matches but `gpt-4o` does not. `gemini-2.5-pro` is frontier; `*-flash` is not.
FRONTIER_MARKERS = (
    "opus",
    "sonnet-4",
    "gpt-5",
    "gemini-2.5-pro",
    "o1",
    "o3",
)

# Providers whose default driver is a strong model even when no explicit model
# string is supplied (the agentic CLIs run Claude/Codex under the hood).
FRONTIER_PROVIDERS = (
    "claude-code-agentic",
    "codex-agentic",
)


def classify_tier(provider: str | None, model: str | None) -> str:
    """Return 'frontier' or 'standard' from a provider + model.

    STANDARD_MARKERS are checked before FRONTIER_MARKERS so downgrade markers
    win. Unknown models fall back to the provider, then to 'standard'.
    """
    m = (model or "").strip().lower()
    if m:
        if any(s in m for s in STANDARD_MARKERS):
            return "standard"
        if any(s in m for s in FRONTIER_MARKERS):
            return "frontier"
    p = (provider or "").strip().lower()
    if p in FRONTIER_PROVIDERS:
        return "frontier"
    return "standard"


def classify_tier_from_label(llm_type: str | None) -> str:
    """Classify from the single `llm_type` label the client sends today.

    `derive_llm_label` produces either the bare model id (e.g.
    "claude-opus-4-7") or "<model> (<provider>)", so matching the same
    substrings against the whole label works without a structured
    provider/model split. Empty/None → 'standard'.
    """
    return classify_tier(None, llm_type)


def role_for_tier(tier: str) -> str:
    """Default role a tier maps to. Role is contributor-owned and overrides
    this, but it seeds the historical default: standard → exploiter behavior,
    frontier → explorer. (Today every agent defaults to 'explorer' regardless;
    this helper is kept for callers that want the tier-derived default.)"""
    return "explorer" if tier == "frontier" else "exploiter"
