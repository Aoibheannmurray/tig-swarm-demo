# Trajectory-Based Swarm Search

A population of AI agents each maintain an independent solution trajectory, improving in parallel with progressive interventions when stuck.

```
╔══════════════════════════════════════════════════════════════════════════════════════════╗
║                               COORDINATION SERVER                                       ║
║                                                                                         ║
║   Leaderboard  │  Hypothesis Log  │  Stagnation Counters  │  Inactive Trajectory Pool   ║
╚════╤═══════════════════╤══════════════════════╤═══════════════════════════╤═══════════════╝
     │                   │                      │                           │
─────┼───────────────────┼──────────────────────┼───────────────────────────┼───────────────
     │                   │                      │                           │
     ▼                   ▼                      ▼                           ▼
┌─── POPULATION OF ACTIVE TRAJECTORIES ────────────────────────────────────────────────────┐
│                                                                                          │
│  ┌─Agent A──────────┐  ┌─Agent B──────────┐  ┌─Agent C──────────┐  ┌─Agent D─────────┐ │
│  │                   │  │                   │  │                   │  │                  │ │
│  │  v1 → v2 → v3    │  │  v1 → v2 → v3    │  │  v1 → v2 → v3    │  │  v1 → v2        │ │
│  │  1200  1500  1800 │  │  900  1100  1100  │  │  2000  2400  2900│  │  1600  1500     │ │
│  │                   │  │            ↑      │  │                   │  │                  │ │
│  │  [improving]      │  │  [stuck 6 iters]  │  │  [improving]      │  │  [stuck 2 iters]│ │
│  │                   │  │                   │  │                   │  │                  │ │
│  └───────────────────┘  └─────────┬─────────┘  └───────────────────┘  └──────────────────┘ │
│                                   │                                                      │
└───────────────────────────────────┼──────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─── PROGRESSIVE STAGNATION RESPONSE ──────────────────────────────────────────────────────┐
│                                                                                          │
│   Stagnation                                                                             │
│   counter:     0          T₁                    T₂                    T₃                 │
│                │           │                     │                     │                  │
│   ─────────── ●───────────●─────────────────────●─────────────────────●──────────►       │
│               │           │                     │                     │                  │
│          Normal loop  HYPOTHESIS RECALL     CROSS-POLLINATION    TRAJECTORY RESET        │
│          (no help)    triggers              triggers              triggers               │
│                           │                     │                     │                  │
│                           ▼                     ▼                     ▼                  │
│               ┌───────────────────┐ ┌─────────────────────┐ ┌────────────────────┐      │
│               │ Server returns    │ │  50/50 coin flip:    │ │ Agent's code →     │      │
│               │ prior failures    │ │                      │ │ deposited in pool  │      │
│               │ on THIS program:  │ │  ┌───────────────┐  │ │                    │      │
│               │                   │ │  │ INSPECT        │  │ │ Agent receives     │      │
│               │ ✗ [local_search]  │ │  │ Read personal  │  │ │ new start from     │      │
│               │   "2-opt" → 3200  │ │  │ tacit-knowl-  │  │ │ pool (see below)   │      │
│               │ ✗ [metaheuristic] │ │  │ edge hints    │  │ │                    │      │
│               │   "SA" → 3050     │ │  └───────────────┘  │ │ Hypothesis history │      │
│               │ ✗ [local_search]  │ │         OR           │ │ resets with new    │      │
│               │   "or-opt" → 3100 │ │  ┌───────────────┐  │ │ code               │      │
│               │                   │ │  │ INSPIRE        │  │ │                    │      │
│               │ "Try something    │ │  │ Read another   │  │ └────────────────────┘      │
│               │  structurally     │ │  │ agent's best   │  │                             │
│               │  different."      │ │  │ (read-only,    │  │                             │
│               │                   │ │  │  adapt ideas)  │  │                             │
│               └───────────────────┘ │  └───────────────┘  │                             │
│                                     │                      │                             │
│                                     │ Agent always edits   │                             │
│                                     │ ITS OWN code         │                             │
│                                     └──────────────────────┘                             │
│                                                                                          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
                                    │
            (on trajectory reset)   │
                                    ▼
┌─── INACTIVE TRAJECTORY POOL ─────────────────────────────────────────────────────────────┐
│                                                                                          │
│   Deposited trajectories from past resets:                                               │
│                                                                                          │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐                                │
│   │ Former   │  │ Former   │  │ Former   │  │ Former   │   ...                          │
│   │ Agent X  │  │ Agent Y  │  │ Agent B  │  │ Agent Z  │                                │
│   │ code     │  │ code     │  │ code     │  │ code     │                                │
│   │ + hyps   │  │ + hyps   │  │ + hyps   │  │ + hyps   │                                │
│   └──────────┘  └──────────┘  └──────────┘  └──────────┘                                │
│                                                                                          │
│   Selection rule (per reset event):                                                      │
│                                                                                          │
│        if pool empty  OR  T^1.5 < P:                                                     │
│             FRESH START   → seed pool / peer / stub chain                                │
│        else:                                                                             │
│             ADOPT          → uniform random from pool, entry removed                     │
│                                                                                          │
│        T = # trajectories ever created for this challenge                                │
│        P = total deactivations summed across all of them                                 │
│                                                                                          │
│   Scaling: at equilibrium T^1.5 ≈ P, so T ~ work^(2/3) and mean trajectory               │
│   lifetime P/T ~ work^(1/3). Early on (T small) fresh starts dominate; as the            │
│   population grows, recycling from the pool dominates.                                   │
│                                                                                          │
│   Recycling: no promising direction is permanently lost — another agent                   │
│   may succeed on code where the original agent stalled.                                   │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Three Thresholds, Three Mechanisms

| Threshold | Trigger | Mechanism | Purpose |
|-----------|---------|-----------|---------|
| **T₁** (hypothesis recall) | `runs_since_improvement ≥ recall_threshold` | Server surfaces failed hypotheses tried on this exact program | Prevent repeating known failures |
| **T₂** (cross-pollination) | `runs_since_improvement ≥ stagnation_threshold` | Random hint: read tacit knowledge, study another agent's code, or (when the failed-attempts archive is on) recall archived failures | Inject new ideas from outside the trajectory |
| **T₃** (trajectory reset) | `runs_since_improvement ≥ stagnation_limit` | Deposit code in pool, adopt new starting point | Escape dead-end trajectories entirely |

The reset (T₃) is the most disruptive and fires last. T₁ and T₂ are independently configured — at the default knobs cross-pollination (threshold 2) actually kicks in before hypothesis recall (threshold 3).

## Benchmark & Evaluation

```
┌─── AGENT's LOCAL MACHINE ────────────────────────────────────────────────────────────────┐
│                                                                                          │
│   mod.rs (agent's algorithm)                                                             │
│        │                                                                                 │
│        │  cargo build                                                                    │
│        ▼                                                                                 │
│   ┌──────────┐         ┌─────────────────────────────────────────────────────────┐       │
│   │  Solver  │────────►│  Run across all tracks (in parallel across CPU cores)    │       │
│   │  binary  │         │                                                         │       │
│                        │  Track 1 (n=50, s=flow_shop)      ──► 5 instances        │       │
│                        │  Track 2 (n=50, s=job_shop)       ──► 5 instances        │       │
│                        │  Track 3 (n=50, s=fjsp_medium)    ──► 5 instances        │       │
│                        │  Track 4 (n=50, s=fjsp_high)      ──► 5 instances        │       │
│                        │           ...                                            │       │
│                        └──────────────────────┬──────────────────────────────────┘       │
│                                               │                                          │
│                                               ▼                                          │
│                        ┌─────────────────────────────────────────────────────────┐       │
│                        │  Per-instance scoring (each instance independently):     │       │
│                        │                                                         │       │
│                        │              baseline_metric − your_metric               │       │
│                        │  quality  =  ─────────────────────────────  × 1,000,000 │       │
│                        │                    baseline_metric                       │       │
│                        │                                                         │       │
│                        │  (clamped to ±10,000,000)                                │       │
│                        │                                                         │       │
│                        │  Infeasible / timeout, no saved solution → −10,000,000   │       │
│                        └──────────────────────┬──────────────────────────────────┘       │
│                                               │                                          │
│                                               ▼                                          │
│                        ┌─────────────────────────────────────────────────────────┐       │
│                        │  Aggregation:                                            │       │
│                        │                                                         │       │
│                        │  Per track:   arithmetic mean of instance qualities      │       │
│                        │                                                         │       │
│                        │  Overall:     shifted geometric mean across tracks       │       │
│                        │               (one bad track tanks the whole score)      │       │
│                        └──────────────────────┬──────────────────────────────────┘       │
│                                               │                                          │
│                                               ▼                                          │
│                                                                                          │
│                                         Final Score                                      │
│                                     (higher = better)                                    │
│                                                                                          │
│   Positive = beating baseline    Zero = matching baseline    Negative = worse            │
│                                                                                          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

The solver has a per-instance **timeout** — if it hasn't finished, whatever was last passed to `save_solution()` is evaluated. If nothing was saved, the instance counts as infeasible (−1M quality). This is why agents write "anytime" algorithms that save early and improve incrementally.

---

## Key Properties

- **Ownership** — Each agent always edits its own code. No merge conflicts, no convergence to one optimum.
- **Diversity** — Multiple trajectories explore different regions of solution space simultaneously.
- **Progressive intervention** — Light touch first (recall), then medium (inspiration), then hard reset. Avoids unnecessary disruption.
- **Memory travels with code** — Hypotheses are attached to the program, not the agent. Adopted trajectories carry their history.
- **Recycling** — Abandoned trajectories re-enter circulation. Nothing promising is permanently lost.
- **Scoring pressure** — Geometric mean across problem tracks prevents agents from gaming easy cases.
