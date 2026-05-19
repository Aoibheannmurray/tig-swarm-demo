"""LLM prompt construction and response parsing for the swarm loop.

All build_*_prompt functions live here, plus hypothesis parsing.
"""

from __future__ import annotations

from challenge_files import is_stub_code, read_tacit_knowledge


# ── Strategy tags ──────────────────────────────────────────────────


DEFAULT_STRATEGY_TAGS = [
    "construction", "local_search", "metaheuristic",
    "constraint_relaxation", "decomposition", "hybrid",
    "data_structure", "other",
]


def get_strategy_tags(config: dict) -> list[str]:
    challenge = config.get("challenge", "")
    available = config.get("available_challenges") or {}
    sub = available.get(challenge) or {}
    tags = sub.get("strategy_tags") or []
    return tags if tags else DEFAULT_STRATEGY_TAGS


# ── Hypothesis prompts ─────────────────────────────────────────────


def build_hypothesis_system_prompt(
    challenge_md: str, config: dict, *, is_bootstrap: bool = False,
) -> str:
    challenge = config.get("challenge", "unknown")
    tags = ", ".join(get_strategy_tags(config))
    if is_bootstrap:
        job = (
            "propose an initial algorithm strategy. The current code is a "
            "stub — you need a complete working approach, not a tweak."
        )
    else:
        job = "propose ONE specific change to try."
    return f"""\
You are planning an improvement to a Rust algorithm for the "{challenge}" challenge.

{challenge_md}

Your job: {job} Do NOT write code — just describe the idea.

Respond in EXACTLY this format (4 lines, nothing else):

TITLE: <short title of what to change, under 80 chars>
DESCRIPTION: <2-3 sentence description of the change and reasoning>
STRATEGY_TAG: <one of: {tags}>
NOTES: <brief interpretation of your approach>"""


def _format_inspiration(state: dict, is_gpu: bool, headline: str) -> list[str]:
    insp = state.get("inspiration_code", "")
    if not insp:
        return []
    out = [f"\n{headline}\n```rust\n{insp}\n```"]
    if is_gpu:
        insp_kernel = state.get("inspiration_kernel_code", "")
        if insp_kernel:
            out.append(f"\nInspiration CUDA kernels:\n```cuda\n{insp_kernel}\n```")
    return out


def build_hypothesis_user_prompt(state: dict, config: dict) -> str:
    parts: list[str] = []
    is_gpu = bool(config.get("is_gpu"))

    code = state.get("best_algorithm_code") or ""
    bootstrap = is_stub_code(code)
    if bootstrap:
        parts.append(
            "Current algorithm (mod.rs) is a stub — propose an initial "
            "implementation strategy from scratch. The stub below shows the "
            "exact `solve_challenge` signature any implementation must keep.\n"
            f"```rust\n{code}\n```"
        )
    else:
        parts.append(f"Current algorithm (mod.rs):\n```rust\n{code}\n```")
        if is_gpu:
            kernel_code = state.get("best_kernel_code") or ""
            if kernel_code:
                parts.append(f"\nCurrent CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```")

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = [f"\n{len(prior)} strategies already tried on this code — try something STRUCTURALLY DIFFERENT:"]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            lines.append(f"  - [{tag}] {title}")
        parts.append("\n".join(lines))

    hint = state.get("stagnation_hint")
    if hint == "inspiration":
        parts.extend(_format_inspiration(
            state, is_gpu,
            "Study this approach for ideas (adapt ideas, do NOT copy wholesale):",
        ))
    elif hint == "tacit_knowledge":
        tk = read_tacit_knowledge().strip()
        if tk:
            parts.append(f"\nPersonal strategy hints:\n{tk}")
        else:
            parts.extend(_format_inspiration(
                state, is_gpu, "Study this approach for ideas:",
            ))

    reset = state.get("trajectory_reset")
    if reset:
        rtype = reset.get("type", "")
        if rtype == "adopted_inactive":
            parts.append(
                "\nYou are starting from another agent's previous algorithm. "
                "Study the code above and propose an improvement to build on it."
            )
        else:
            parts.append(
                "\nYou are starting from the template algorithm. "
                "Propose an initial strategy to improve it."
            )

    parts.append("\nPropose one specific improvement to try.")
    return "\n".join(parts)


# ── Code generation prompts ────────────────────────────────────────


def build_code_system_prompt(challenge_md: str, config: dict) -> str:
    challenge = config.get("challenge", "unknown")
    is_gpu = bool(config.get("is_gpu"))
    timeout = config.get("timeout", 30)
    time_guidance = (
        f"\nPer-instance time budget: {timeout} seconds. Your solver process is killed "
        f"after this hard deadline. Use a time-based loop (std::time::Instant + deadline) "
        f"that runs until the budget is nearly exhausted, leaving a small margin (e.g. 2-5s) "
        f"for cleanup. Call save_solution() early with your first feasible solution, then "
        f"keep improving and re-saving — the last saved solution is evaluated. If no "
        f"solution was saved when the deadline hits, the instance counts as infeasible."
    )
    if is_gpu:
        return f"""\
You are optimizing a Rust+CUDA algorithm for the "{challenge}" GPU challenge.

{challenge_md}
{time_guidance}

IMPORTANT RULES:
- `use super::*;` must remain as the first import in the Rust file.
- `fn solve_challenge(` MUST appear in your Rust output — do NOT omit it.
- Return BOTH files: the complete Rust source AND the complete CUDA kernel source.
- Separate them with a line containing exactly: // --- kernels.cu ---
- The Rust file comes FIRST, then the separator, then the CUDA file.
- No explanation, no markdown fences — just the two raw source files with the separator.
- Kernel function names in mod.rs (module.get_function("...")) must match the extern "C" __global__ function names in kernels.cu."""
    return f"""\
You are optimizing a Rust algorithm for the "{challenge}" challenge.

{challenge_md}
{time_guidance}

OUTPUT FORMAT (strict):
Your response will be written verbatim to mod.rs and compiled. The very first
character of your response MUST be `u` from `use super::*;`. No preamble, no
prose, no markdown fences (```), no commentary before or after the code.
`use super::*;` must remain as the first import."""


def build_code_user_prompt(state: dict, hypothesis: dict, config: dict) -> str:
    parts: list[str] = []
    is_gpu = bool(config.get("is_gpu"))

    code = state.get("best_algorithm_code") or ""
    bootstrap = is_stub_code(code)
    if bootstrap:
        parts.append(
            "Current algorithm (mod.rs) is a stub — replace it with a complete "
            "implementation. Keep the exact `solve_challenge` signature shown "
            "below (parameter names and types), and call the `save_solution` "
            "closure parameter whenever you find an improved solution.\n"
            f"```rust\n{code}\n```"
        )
    else:
        parts.append(f"Current algorithm (mod.rs):\n```rust\n{code}\n```")

    if is_gpu:
        kernel_code = state.get("best_kernel_code") or ""
        if kernel_code:
            parts.append(f"\nCurrent CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```")
        else:
            parts.append(
                "\nNo CUDA kernel file yet — write any custom GPU kernels you need."
            )
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nThe Rust file (with fn solve_challenge) comes first, then the separator, then the CUDA file."
        )

    title = hypothesis.get("title", "")
    description = hypothesis.get("description", "")
    verb = "Implement this strategy" if bootstrap else "Apply this change"
    parts.append(f"\n{verb}:\n{title}\n{description}")

    return "\n".join(parts)


# ── Error recovery prompts ─────────────────────────────────────────


def build_runtime_fix_prompt(code: str, bench: dict, kernel_code: str = "", timeout: int = 30) -> str:
    errors = bench.get("errors") or []
    error_lines = "\n".join(f"  - {e}" for e in errors)
    score = bench.get("score", 0)
    feasible = bench.get("feasible", False)
    track_scores = bench.get("track_scores", {})
    track_summary = "\n".join(
        f"  - {track}: {s:.0f}" for track, s in track_scores.items()
    )
    parts = [f"Current algorithm (mod.rs):\n```rust\n{code}\n```\n"]
    if kernel_code:
        parts.append(f"Current CUDA kernels (kernels.cu):\n```cuda\n{kernel_code}\n```\n")
    parts.append(
        f"This code compiled successfully but failed at runtime.\n\n"
        f"Score: {score}  Feasible: {feasible}\n"
        f"Per-track scores:\n{track_summary}\n"
        f"Errors:\n{error_lines}\n\n"
        f"Per-instance time budget: {timeout} seconds. The solver is killed after this deadline.\n\n"
        "How to interpret the errors:\n"
        "- 'no solution saved' = the code crashed, panicked, or returned Err() "
        "before ever calling save_solution(), OR the solver ran out of time "
        f"without saving. Fix: use a time-based loop (std::time::Instant + deadline "
        f"at {timeout}s minus a few seconds margin) and call save_solution() EARLY "
        "with your first feasible solution, then keep improving and re-saving.\n"
        "- Any other error = the code saved a solution but the evaluator "
        "rejected it (constraint violation). Fix: check that your solution "
        "satisfies all feasibility constraints described in the challenge.\n\n"
        "Fix the runtime errors and return the complete source."
    )
    if kernel_code:
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nEnsure kernel function names match between mod.rs and kernels.cu."
        )
    return "\n".join(parts)


def build_compile_fix_prompt(
    code: str, kernel: str, compiler_errors: str, is_gpu: bool,
) -> str:
    parts = [f"Current algorithm (mod.rs):\n```rust\n{code}\n```\n"]
    if kernel:
        parts.append(f"Current CUDA kernels (kernels.cu):\n```cuda\n{kernel}\n```\n")
    parts.append(
        f"This code failed to compile. Here are the errors:\n```\n{compiler_errors}\n```\n\n"
        "Fix the compile errors and return the complete source."
    )
    if is_gpu:
        parts.append(
            "\nReturn BOTH files separated by: // --- kernels.cu ---"
            "\nEnsure kernel function names match between mod.rs and kernels.cu."
        )
    return "\n".join(parts)


def build_redescribe_system_prompt(config: dict) -> str:
    """System prompt for the re-describe pass.

    The normal hypothesis prompt asks the LLM to propose ONE new change;
    here we instead ask it to characterize what the (post-fix) code does,
    so the system prompt has to match the user prompt's intent.
    """
    challenge = config.get("challenge", "unknown")
    tags = ", ".join(get_strategy_tags(config))
    return f"""\
You are reviewing a code change that was made for the "{challenge}" challenge.

Your job: produce an accurate post-hoc description of what the FINAL code does,
in the same 4-line format used for hypotheses. Do NOT propose new changes.

Respond in EXACTLY this format (4 lines, nothing else):

TITLE: <short title of what the final code does, under 80 chars>
DESCRIPTION: <2-3 sentence description of the approach in the final code>
STRATEGY_TAG: <one of: {tags}>
NOTES: <brief notes on how/whether the recovery changed the approach>"""


def build_redescribe_hypothesis_prompt(
    original_code: str, final_code: str, original_hypothesis: dict,
    *, original_kernel: str = "", final_kernel: str = "",
) -> str:
    orig_title = original_hypothesis.get("title", "")
    orig_desc = original_hypothesis.get("description", "")
    orig_tag = original_hypothesis.get("strategy_tag", "other")
    parts = [
        f"The original hypothesis was:\n"
        f"  TITLE: {orig_title}\n"
        f"  DESCRIPTION: {orig_desc}\n"
        f"  STRATEGY_TAG: {orig_tag}\n",
        f"Original code (before):\n```rust\n{original_code}\n```\n",
    ]
    if original_kernel:
        parts.append(f"Original CUDA kernels (before):\n```cuda\n{original_kernel}\n```\n")
    parts.append(f"Final code (after fixing runtime errors):\n```rust\n{final_code}\n```\n")
    if final_kernel:
        parts.append(f"Final CUDA kernels (after):\n```cuda\n{final_kernel}\n```\n")
    parts.append(
        "The code was modified to fix runtime errors. Compare the original "
        "hypothesis against the final code. If the error recovery changed the "
        "core approach (e.g. replaced the construction heuristic, added a "
        "fundamentally different fallback strategy, restructured the solver), "
        "update the TITLE, DESCRIPTION, and STRATEGY_TAG to accurately reflect "
        "what the final code actually does. If the fixes were minor (e.g. "
        "bounds checks, error handling wrappers) and the core approach is "
        "unchanged, keep the original hypothesis as-is.\n\n"
        "Respond with the corrected hypothesis."
    )
    return "\n".join(parts)


# ── Hypothesis parsing ─────────────────────────────────────────────


_META_DEFAULTS = {
    "title": "LLM mutation",
    "description": "Automated code improvement",
    "strategy_tag": "other",
    "notes": "",
}


def parse_hypothesis(text: str) -> dict:
    meta = dict(_META_DEFAULTS)
    for line in text.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if key in meta and value:
                meta[key] = value
    return meta


# ── Agentic mode user prompt ───────────────────────────────────────


def build_agentic_user_prompt(state: dict, config: dict) -> str:
    """Per-iteration prompt for tooled (agentic) Claude Code.

    Stable rules — file scope, hypothesis.json schema, cargo allowlist,
    solver constraints — live in CLAUDE.md. This prompt is just the
    variable state the agent needs to decide what to try this iteration.
    """
    parts: list[str] = []
    is_gpu = bool(config.get("is_gpu"))
    challenge = config.get("challenge", "unknown")

    my_score = state.get("my_best_score")
    global_best = state.get("best_score")
    stagnation = state.get("my_runs_since_improvement", 0)
    runs = state.get("my_runs", 0)
    improvements = state.get("my_improvements", 0)

    parts.append(
        f"## Iteration context\n"
        f"- Challenge: {challenge}{' (GPU)' if is_gpu else ''}\n"
        f"- Your best score: {my_score}\n"
        f"- Global best score: {global_best}\n"
        f"- Runs / improvements / stagnation: {runs} / {improvements} / {stagnation}"
    )

    reset = state.get("trajectory_reset")
    if reset:
        rtype = reset.get("type", "")
        if rtype == "adopted_inactive":
            parts.append(
                "\n## Trajectory reset\n"
                "You stagnated and the server handed you another agent's "
                "previously-active algorithm as your new starting point. "
                "Study it (already on disk) and propose an improvement that "
                "builds on its approach."
            )
        else:
            parts.append(
                "\n## Trajectory reset (fresh start)\n"
                "You stagnated and have been reset to the template algorithm. "
                "Propose an initial strategy from scratch."
            )

    bootstrap = is_stub_code(state.get("best_algorithm_code") or "")
    if bootstrap:
        parts.append(
            "\n## Bootstrap iteration\n"
            "The algorithm on disk is a stub (`unimplemented!()`). Write a "
            "complete `solve_challenge` implementation from scratch — don't "
            "tweak, replace. Read the stub first to lock in the exact "
            "`solve_challenge` signature (parameter names and types) before "
            "writing anything."
        )

    prior = state.get("prior_hypotheses") or []
    if prior:
        lines = [
            "\n## Already tried against this exact code (avoid repeating)",
        ]
        for h in prior:
            tag = h.get("strategy_tag", "?")
            title = h.get("title", "?")
            lines.append(f"  - [{tag}] {title}")
        parts.append("\n".join(lines))

    hint = state.get("stagnation_hint")
    if hint == "inspiration":
        insp = state.get("inspiration_code", "")
        insp_agent = state.get("inspiration_agent_name", "another agent")
        if insp:
            block = [
                f"\n## Stagnation hint — inspiration from {insp_agent}",
                "Study this peer's current best for *structural* ideas to "
                "adapt. Do NOT copy it wholesale — your job is to evolve "
                "your own lineage, not replace it.",
                "",
                "```rust",
                insp,
                "```",
            ]
            if is_gpu:
                insp_kernel = state.get("inspiration_kernel_code", "")
                if insp_kernel:
                    block += [
                        "",
                        f"Peer CUDA kernels:",
                        "```cuda",
                        insp_kernel,
                        "```",
                    ]
            parts.append("\n".join(block))
    elif hint == "tacit_knowledge":
        parts.append(
            "\n## Stagnation hint — tacit knowledge\n"
            "Check `tacit_knowledge_personal.md` in this worktree (if "
            "present) for strategy hints the contributor wrote down. If "
            "absent, fall back to the inspiration_code block above if any."
        )

    parts.append(
        "\n## Your task\n"
        "1. Decide on ONE specific improvement.\n"
        "2. Edit the algorithm file in place to implement it.\n"
        "3. Run `cargo check --features solver," + challenge + "` to confirm it compiles.\n"
        "4. Write `.swarm/hypothesis.json` describing what you did.\n"
        "5. Stop.\n\n"
        "Do not run `scripts/benchmark.py` — the driver will run the "
        "official benchmark after you stop."
    )
    return "\n".join(parts)
