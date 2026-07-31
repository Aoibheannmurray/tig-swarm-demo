"""Single source of truth for the per-agent config knobs.

Every knob a fleet entry can carry is declared here **once**, with flags for how
far it travels. The four key lists that `run_fleet.py` and `run_loop.py` used to
maintain by hand are derived from this registry:

    AGENT_CONFIG_KEYS       every knob (all of them are written into the
                            worktree's agent.config.json at spawn)
    FLEET_WIDE_DEFAULT_KEYS fleet_default=True — settable once at the top level
                            of fleet.config.json, inherited via setdefault so a
                            per-agent value still wins
    HOT_RELOAD_KEYS         hot_reload=True — the monitor re-syncs it into a
                            RUNNING worktree when fleet.config.json changes
    LIVE_CONFIG_KEYS        live=True — run_loop re-reads it off `config` every
                            iteration (the subset of hot_reload that flows
                            through run_loop's per-iteration merge, as opposed
                            to `role`/`seeded_start`, which it re-reads
                            explicitly at the top of the loop)

**Why this exists.** These were four hand-maintained tuples across two modules,
and every new knob had to be added to the right subset of them. Getting that
wrong is silent in both directions: omit a knob from HOT_RELOAD_KEYS and a
documented hot-reload knob quietly needs a restart; add a startup-only knob to
it and the monitor rewrites the file and logs a reassuring "changed" line while
the running process keeps using its startup value — worse than an honest
"restart required", because it looks like it worked. The former shipped (fixed
in df13614) and was patched with a test asserting the four lists agree, which
polices the hazard rather than removing it. Declaring each knob once removes it.

**Adding a knob:** add one `Knob(...)` below and nothing else. The invariants at
the bottom of this module reject a declaration that cannot work — a knob cannot
be `live` without being `hot_reload` (run_loop would re-read a value the monitor
never delivers), nor `live` without being `fleet_default` (the documented way to
set these is once at the top level, so that path must hot-reload too).

Kept deliberately dependency-free (stdlib only, no side effects) so both the
fleet launcher and a standalone `run_loop.py` inside a worktree can import it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Knob:
    """One per-agent config field.

    `doc` is the human note that used to sit as a comment beside the key in
    run_fleet.py's tuples; it is kept with the declaration so the explanation
    cannot drift away from the flag it explains.
    """

    name: str
    fleet_default: bool = False
    hot_reload: bool = False
    live: bool = False
    doc: str = ""


# Order is preserved from the original _AGENT_CONFIG_KEYS purely so the
# generated lists stay diff-comparable against the pre-refactor tuples.
KNOBS: tuple[Knob, ...] = (
    # ── Startup-only: run_loop resolves these once, before the loop ──
    # Propagating any of them would log a change the running process never
    # makes. They are written at spawn and then fixed for the process's life.
    Knob("provider"),
    Knob("model"),
    Knob("api_base"),
    Knob("compute"),
    Knob("c3_hardware"),
    Knob("c3_time"),
    Knob("c3_cloud_provider"),
    Knob("c3_no_build"),
    Knob(
        "c3_max_parallel_jobs",
        fleet_default=True,
        doc="Fleet-wide C3 concurrent-job cap, and the balanced shard count "
            "per benchmark. Best set once at the top level — all agents share "
            "ONE C3 key and thus one FCFS slot pool. Startup-only: the pool is "
            "sized when the fleet launches (and overwritten from the live C3 "
            "subscription), so re-syncing it mid-run would not resize it.",
    ),
    # ── C3 warm-image fast path: read per benchmark, so fully live ──
    Knob("c3_warm_images", fleet_default=True, hot_reload=True, live=True,
         doc="Boolean opt-in to the warm-image path (c3_compute._warm_c3_image)."),
    Knob("c3_warm_image", fleet_default=True, hot_reload=True, live=True,
         doc="Explicit warm image ref, pinning an exact tag."),
    Knob("tig_dockerhub", fleet_default=True, hot_reload=True, live=True,
         doc="Docker Hub namespace override for the warm images."),
    Knob(
        "c3_api_key",
        doc="Per-agent C3 API key (raw value). Omit to inherit the top-level "
            "fleet c3_api_key, then C3_API_KEY, then the `c3 login` session. "
            "Lets each agent bill C3 to a different key without separate fleets.",
    ),
    Knob(
        "agent_id",
        doc="Hand-set identity, to point a new clone at an existing dashboard "
            "agent without re-registering. Normal flow: run_loop writes this "
            "after the first /api/agents/register so restarts resume it.",
    ),
    Knob("agent_name", doc="See agent_id."),
    Knob("log_prompts"),
    Knob("detailed_prompts",
         doc="Opt-in stricter Rust prompt for smaller/cheaper models (prompts.py)."),
    # ── Write gates: read off `config` each iteration ──
    Knob(
        "tacit_write", fleet_default=True, hot_reload=True, live=True,
        doc="Kill switch for tacit-knowledge writing (default True). False "
            "stops this agent appending `- LLM:` lessons to its "
            "tacit_knowledge_personal.md; driver-side and in-band paths both "
            "gated. A host turning agent writes off wants that fleet-wide.",
    ),
    Knob(
        "failed_attempts_write", fleet_default=True, hot_reload=True, live=True,
        doc="Opt-out for the server's failed-attempts archive (default True). "
            "Only matters when the host has enabled the swarm-wide "
            "failed_attempts_archive toggle. With the archive on, distilled "
            "lessons go to the DB ONLY — the local tacit file is not appended.",
    ),
    # ── Contributor-owned behaviour: hot-reloaded, but run_loop re-reads
    #    these explicitly at the top of the loop rather than through the
    #    per-iteration config merge, so they are NOT `live`. ──
    Knob(
        "role", hot_reload=True,
        doc="explorer (novel/ambitious) or exploiter (small localized edits). "
            "Materialized at spawn AND re-synced live, so editing it in "
            "fleet.config.json takes effect on the agent's next iteration.",
    ),
    Knob(
        "seeded_start", hot_reload=True,
        doc="true/false override of where a fresh trajectory starts; omit for "
            "auto. true: working code (seed pool -> best peer -> stub). false: "
            "always the bare stub. Absent, the server decides by model "
            "tier/role. Applies at the next fresh trajectory, not mid-one.",
    ),
    Knob(
        "edit_mode",
        doc='API-mode edit strategy for SINGLE-FILE algorithms: "full" '
            '(default, whole-file replacement) or "search_replace". Multi-file '
            "algorithms and exploiters always use search/replace regardless. "
            "Startup-only: resolved once when the loop picks its edit path.",
    ),
    # ── Hyperparameter search (docs/hyperparameter-search.md) ──
    Knob("hpo_min_improvements", fleet_default=True, hot_reload=True, live=True),
    Knob("hpo_first_tune_improvements", fleet_default=True, hot_reload=True, live=True),
    Knob("hpo_num_suggested_configs", fleet_default=True, hot_reload=True, live=True),
    Knob("hpo_search_budget", fleet_default=True, hot_reload=True, live=True),
    Knob("hpo_seed", fleet_default=True, hot_reload=True, live=True),
    # ── Cleaner (docs/cleaner-agent.md) ──
    Knob("cleaner_trigger_chars", fleet_default=True, hot_reload=True, live=True),
    Knob("cleaner_target_pct", fleet_default=True, hot_reload=True, live=True),
    Knob("cleaner_score_delta_pct", fleet_default=True, hot_reload=True, live=True),
    Knob("cleaner_cooldown_iters", fleet_default=True, hot_reload=True, live=True),
    # ── Freeze guard ──
    Knob(
        "no_benchmark_freeze_limit", fleet_default=True, hot_reload=True, live=True,
        doc="Consecutive token-spending iterations without a successful "
            "benchmark before the agent exits (run_loop._NO_BENCHMARK_FREEZE_"
            "LIMIT). Default 10; 0 disables.",
    ),
)


def _names(**flags: bool) -> tuple[str, ...]:
    return tuple(
        k.name for k in KNOBS
        if all(getattr(k, flag) is value for flag, value in flags.items())
    )


#: Every knob — all of them are forwarded into agent.config.json at spawn.
AGENT_CONFIG_KEYS: tuple[str, ...] = tuple(k.name for k in KNOBS)

#: Settable once at the top level of fleet.config.json; inherited per agent.
FLEET_WIDE_DEFAULT_KEYS: tuple[str, ...] = _names(fleet_default=True)

#: Re-synced by the fleet monitor into a running worktree's agent.config.json.
HOT_RELOAD_KEYS: tuple[str, ...] = _names(hot_reload=True)

#: Re-read by run_loop off `config` on every iteration.
LIVE_CONFIG_KEYS: tuple[str, ...] = _names(live=True)

#: Read once before the loop starts. Syncing these would be a lie — kept as a
#: derived list so tests can assert the monitor never touches them.
STARTUP_ONLY_KEYS: tuple[str, ...] = _names(hot_reload=False)


def _validate() -> None:
    """Reject a declaration that cannot work, at import time.

    Cheap enough to run on every import, and it fires in the one place that
    matters — the moment someone adds a knob with the wrong flags.
    """
    seen: set[str] = set()
    for k in KNOBS:
        if k.name in seen:
            raise ValueError(f"duplicate knob {k.name!r} in KNOBS")
        seen.add(k.name)
        if k.live and not k.hot_reload:
            raise ValueError(
                f"{k.name!r}: live=True needs hot_reload=True — run_loop would "
                "re-read a value the monitor never delivers, so editing it on a "
                "running fleet would silently do nothing."
            )
        if k.live and not k.fleet_default:
            raise ValueError(
                f"{k.name!r}: live=True needs fleet_default=True — these knobs "
                "are documented as 'set them once at the top level', so the "
                "documented way to configure it must hot-reload too."
            )


_validate()
