"""Trajectory-reset state machine (stagnation → deposit → adopt/fresh start).

Lifecycle
---------
An agent's work on a challenge is organised into *trajectories*: lineages of
code that improve (or stall) publish by publish. When an agent has gone
`stagnation_limit` publishes without beating its trajectory best — or, with
`negative_trajectory_limit` > 0, when its best is still not better than 0
after that many edits (the "negative cull") — the agent view of
GET /api/state (server._agent_state) invokes this machine, which:

  1. Deactivates the agent's current trajectory (`trajectories.status` →
     'inactive', num_deactivations += 1).
  2. Picks the next launch point:
       - a *fresh start* — code chosen by the injected `seed_fn`
         (server.seed_for_agent: seed pool → best active peer → stub) —
         when the inactive pool is empty or the N^1.5 < D diversity rule
         says the population needs more distinct trajectories, or
       - *adoption* of a random per-challenge inactive-pool entry
         (consume-once: the row is deleted), reactivating and re-joining
         the original trajectory when the entry still carries its id.
  3. Deposits the stagnated trajectory's best into the inactive pool
     (feasible bests only), so other agents can adopt it later.
  4. Seeds `trajectory_bests` with the adopted floor (or clears it on a
     fresh start / unscored seed) and zeroes the agent's stagnation
     counters + swaps its current trajectory/program ids.

Concurrency
-----------
The whole machine runs in one BEGIN IMMEDIATE transaction (same pattern as
server.create_iteration): it is a multi-step read-modify-write (pool pick →
consume → deposit → upsert) across many awaits, so without the write lock
two concurrent /api/state calls for the same agent could both pass the
stagnation check and double-reset — each consuming a pool seed and
depositing the old best twice. The stagnation condition is re-checked
*after* taking the lock; the loser sees runs_since_improvement already back
at 0 and returns None (no reset, no side effects).

This module never opens its own DB connection — the caller's `conn` is
injected, and the caller broadcasts the TrajectoryReset WS event after this
returns (i.e. after the commit, matching the original ordering). That is
also why the module-level `import db` is safe under the self-running tests'
re-import dance (they pop only "db"/"server" from sys.modules): every db
helper used here is a pure function of `conn` and never touches db.DB_PATH.
"""

import random
from dataclasses import dataclass

import db
from models import new_id


@dataclass
class ResetOutcome:
    """What server._agent_state needs to build its response after a reset."""

    # The `trajectory_reset` field of the /api/state response
    # ({"type": "fresh_start"|"adopted_inactive", ...}).
    reset_info: dict
    # How the fresh start's code was chosen ('seed' | 'peer' | 'stub');
    # None on adoption.
    seed_start: str | None
    # The agent's new starting code.
    algorithm_code: str
    kernel_code: str | None
    algorithm_files: dict | None
    # Canonical trajectory_bests row after the reset (None when cleared —
    # fresh start or unscored seed).
    traj_best: dict | None
    current_trajectory_best: float | None
    traj_best_experiment_id: str | None
    # Stamp used for deactivation/deposit — the caller reuses it for the
    # TrajectoryReset broadcast.
    timestamp: str


async def maybe_reset_trajectory(
    conn,
    *,
    agent_id: str,
    challenge: str,
    direction: str,
    cutoff_ts: str,
    stagnation_limit: int,
    negative_trajectory_limit: int = 0,
    agent_tier: str,
    agent_role: str,
    seed_fn,
    timestamp: str,
) -> ResetOutcome | None:
    """Run the reset machine for (agent, challenge) if it is still stagnating.

    Returns None without side effects when the re-check under the write lock
    finds the reset already done (a concurrent call won the race) — callers
    should then re-read state and serve the post-reset view.

    `seed_fn` must have server.seed_for_agent's signature:
    (conn, agent_id, challenge, tier, role, *, direction, cutoff_ts) →
    (algorithm_code, kernel_code, algorithm_files, start).
    """
    # Take the write lock FIRST, then re-check the stagnation condition
    # under it (see module docstring: this is what makes two concurrent
    # calls for the same agent unable to double-reset or double-consume a
    # pool seed).
    await conn.execute("BEGIN IMMEDIATE")
    acs = await db.get_agent_challenge_state(conn, agent_id, challenge)
    runs_since = acs["runs_since_improvement"] if acs else 0
    traj_best = await db.get_trajectory_best(conn, agent_id, challenge)
    stagnated = (stagnation_limit > 0 and runs_since >= stagnation_limit
                 and traj_best is not None)

    # Negative cull: the trajectory's best never crossed zero after
    # `negative_trajectory_limit` edits (see server._agent_state for the
    # rationale). Re-checked here under the lock like stagnation: a
    # concurrent reset swaps current_trajectory_id to a fresh line with
    # few edits, so the loser fails this check and returns None.
    negative_cull = False
    if not stagnated and traj_best is not None and negative_trajectory_limit > 0:
        cull_traj_id = acs["current_trajectory_id"] if acs else None
        if cull_traj_id and not db.is_better(direction, traj_best["score"], 0.0):
            traj_row = await db.get_trajectory(conn, cull_traj_id)
            if traj_row and (traj_row["num_edits"] or 0) >= negative_trajectory_limit:
                negative_cull = True

    if not (stagnated or negative_cull):
        # Lost the race: a concurrent call already reset (zeroing
        # runs_since_improvement) or cleared the trajectory best.
        await conn.rollback()
        return None
    reset_reason = "stagnation" if stagnated else "negative_cull"

    # Deactivate the current trajectory.
    cur_traj_id = acs["current_trajectory_id"] if acs else None
    old_program_id = acs["current_program_id"] if acs else None
    if cur_traj_id:
        await db.deactivate_trajectory(conn, cur_traj_id, timestamp)

    # Pick from the per-challenge inactive pool BEFORE depositing,
    # so the agent can't re-adopt its own just-deposited code.
    # CORRECTNESS INVARIANT: pick must be filtered by challenge —
    # otherwise a stagnating VRP agent could be handed SAT code.
    inactive_pool = await db.get_inactive_with_deactivations(conn, challenge)
    new_traj_id = None
    new_program_id = None

    # Fresh start if N^1.5 < D (number of trajectories^1.5 <
    # total deactivations). At equilibrium N^1.5 ≈ D, so the
    # trajectory count grows as total_work^(2/3) and mean
    # trajectory length (D/N) grows as total_work^(1/3) —
    # favors more diverse trajectories with shorter lives than
    # the prior N² rule.
    n_traj, total_deact = await db.trajectory_counts(conn, challenge)
    go_fresh = not inactive_pool or n_traj ** 1.5 < total_deact

    # Carries the adopted trajectory's peak score (None on a fresh
    # start) so we can seed it as the agent's personal best below.
    adopted_score = None
    seed_start = None
    if go_fresh:
        new_code, new_kernel_code, new_files, _start = await seed_fn(
            conn, agent_id, challenge, agent_tier, agent_role,
            direction=direction, cutoff_ts=cutoff_ts,
        )
        new_program_id = new_id()
        reset_info = {"type": "fresh_start", "start": _start,
                      "reason": reset_reason}
        seed_start = _start
    else:
        picked = random.choice(inactive_pool)
        new_code = picked["algorithm_code"]
        new_kernel_code = picked.get("kernel_code")
        new_files = db.row_files(picked)
        new_program_id = picked.get("program_id") or new_id()
        adopted_score = picked.get("score")
        await db.remove_inactive(conn, picked["id"])
        reset_info = {
            "type": "adopted_inactive",
            "reason": reset_reason,
            "prior_score": adopted_score,
            # Seeds deposited without a benchmark (admin/mainnet
            # seeds) have no score, so there's no floor to inherit.
            # Tell the agent to benchmark the adopted code unchanged
            # FIRST, so the trajectory floor is the seed's real
            # score instead of its first (possibly worse) mutation.
            "needs_benchmark": adopted_score is None,
        }
        if picked.get("trajectory_id"):
            new_traj_id = picked["trajectory_id"]
            await db.reactivate_trajectory(conn, new_traj_id)
            await db.increment_trajectory_agents(conn, new_traj_id)

    # Now deposit the stagnated code into the per-challenge pool —
    # but ONLY if the trajectory's best is feasible. The inactive
    # pool is the adoption/seed pool other agents draw from on a
    # stagnation reset (random.choice above), so an infeasible entry
    # would hand broken code to a fresh agent and spread the
    # infeasible-floor trap. With the feasibility-gated
    # `beats_trajectory_best` in create_iteration, trajectory_bests
    # should already be feasible, but legacy rows (and the
    # adopted-floor upsert) mean we can't assume it — gate explicitly
    # here so infeasible algorithms never enter the pool.
    if traj_best.get("feasible"):
        await db.deposit_inactive(
            conn, agent_id, challenge,
            traj_best["algorithm_code"], traj_best["score"], timestamp,
            trajectory_id=cur_traj_id, program_id=old_program_id,
            kernel_code=traj_best.get("kernel_code"),
            experiment_id=traj_best.get("experiment_id"),
            algorithm_files=traj_best.get("algorithm_files"),
        )

    # Seed the agent's personal best with the adopted trajectory's
    # peak so it builds UP from that best instead of from zero.
    # Without this the floor is gone: the agent's first (possibly
    # worse) result becomes its new best and the trajectory drifts
    # below the peak it was handed. It also kept the adopted code
    # alive only until the next publish — if that publish failed,
    # the next state fell back to the stub (see the `else` branch
    # in server._agent_state). Fresh starts, and pool entries with no
    # score, keep the original clear-to-empty behaviour.
    if adopted_score is not None:
        # Inherit the real experiment that earned this floor, carried
        # through the inactive pool. It belongs to whoever last
        # improved this (possibly shared) trajectory — not
        # necessarily this agent. NULL when provenance is unknown
        # (legacy inactive row predating experiment_id, or an
        # admin-seeded entry); never a fabricated id, so it always
        # resolves to a real experiments row or is absent.
        adopted_experiment_id = picked.get("experiment_id")
        await db.upsert_trajectory_best(
            conn, agent_id=agent_id, challenge=challenge,
            experiment_id=adopted_experiment_id,
            algorithm_code=new_code, score=adopted_score,
            feasible=True, challenge_metrics=None,
            solution_data=None, updated_at=timestamp,
            trajectory_id=new_traj_id, kernel_code=new_kernel_code,
            algorithm_files=db.files_json(new_files),
        )
        # Re-read the row we just upserted so traj_best has the full
        # canonical shape (solution_data / hyperparameters /
        # challenge_metrics included). A hand-built partial dict here
        # KeyErrors downstream — e.g. `traj_best["solution_data"]`
        # when building the state response.
        traj_best = await db.get_trajectory_best(conn, agent_id, challenge)
        current_trajectory_best = adopted_score
        traj_best_experiment_id = adopted_experiment_id
    else:
        await db.clear_trajectory_best(conn, agent_id, challenge)
        traj_best = None
        current_trajectory_best = None
        traj_best_experiment_id = None
    await db.update_agent_challenge_state(
        conn, agent_id, challenge,
        set_fields={
            "runs_since_improvement": 0,
            "current_trajectory_id": new_traj_id,
            "current_program_id": new_program_id,
        },
    )
    await conn.commit()

    return ResetOutcome(
        reset_info=reset_info,
        seed_start=seed_start,
        algorithm_code=new_code,
        kernel_code=new_kernel_code,
        algorithm_files=new_files,
        traj_best=traj_best,
        current_trajectory_best=current_trajectory_best,
        traj_best_experiment_id=traj_best_experiment_id,
        timestamp=timestamp,
    )
