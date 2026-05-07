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
│   Deposited trajectories from past resets, plus one "fresh start" slot:                  │
│                                                                                          │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       ┌─────────────┐         │
│   │ Former   │  │ Former   │  │ Former   │  │ Former   │       │   FRESH     │         │
│   │ Agent X  │  │ Agent Y  │  │ Agent B  │  │ Agent Z  │  ...  │   START     │         │
│   │ code     │  │ code     │  │ code     │  │ code     │       │   (seed)    │         │
│   │ + hyps   │  │ + hyps   │  │ + hyps   │  │ + hyps   │       │             │         │
│   └──────────┘  └──────────┘  └──────────┘  └──────────┘       └─────────────┘         │
│        ▲              ▲             ▲              ▲                   ▲                  │
│        └──────────────┴─────────────┴──────────────┴───────────────────┘                  │
│                         uniform random selection                                          │
│                      (chosen item removed from pool)                                      │
│                                                                                          │
│   Recycling: no promising direction is permanently lost — another agent                   │
│   may succeed on code where the original agent stalled.                                   │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

## Three Thresholds, Three Mechanisms

| Threshold | Trigger | Mechanism | Purpose |
|-----------|---------|-----------|---------|
| **T₁** (hypothesis recall) | `runs_since_improvement ≥ recall_threshold` | Server surfaces failed hypotheses tried on this exact program | Prevent repeating known failures |
| **T₂** (cross-pollination) | `runs_since_improvement ≥ stagnation_threshold` | 50/50: read tacit knowledge OR study another agent's code | Inject new ideas from outside the trajectory |
| **T₃** (trajectory reset) | `runs_since_improvement ≥ stagnation_limit` | Deposit code in pool, adopt new starting point | Escape dead-end trajectories entirely |

Each threshold is progressively more disruptive: first "remember what failed," then "look outside for ideas," then "abandon this line and start fresh."

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
│   └──────────┘         │  Track 1 (n=50, FLOW_SHOP)        ──► 20 instances      │       │
│                        │  Track 2 (n=100, JOB_SHOP)        ──► 20 instances      │       │
│                        │  Track 3 (n=200, FJSP_MEDIUM)     ──► 20 instances      │       │
│                        │  Track 4 (n=500, FJSP_HIGH)       ──► 20 instances      │       │
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
│                        │  Infeasible / timeout with no saved solution → −1,000,000│       │
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
