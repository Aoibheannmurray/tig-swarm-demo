// Per-agent experiment history for the chart's per-agent tabs.
//
// Two data structures are kept in lockstep:
//  - `loaded` map: agentId → fully-loaded history fetched from
//    /api/agent_experiments, plus any live events that arrived after the
//    fetch resolved.
//  - `pending` map: live events that landed BEFORE the history fetch
//    finished. These are merged into `loaded` at the end of load(), so no
//    iteration is dropped even if a publish lands while we're waiting on
//    the REST round-trip.
//
// Extracted from ChartPanel so the cache + fetch + merge logic can be
// reasoned about (and replaced) independently of the SVG rendering layer.

import { getViewedChallenge } from "../lib/viewedChallenge";

export interface AgentExperiment {
  time: number;
  score: number;
  feasible: boolean;
  experimentId?: string;
  // Per-iteration metadata fed back from /api/agent_experiments — used to
  // mark events on the per-agent progress plot:
  //  - trajectoryId   → group of consecutive experiments sharing a trajectory
  //  - trajectoryDeactivated → last experiment on a trajectory that became
  //                            inactive (cross marker)
  //  - receivedHint   → "tacit_knowledge" (star) / "inspiration" (square)
  trajectoryId?: string | null;
  trajectoryDeactivated?: boolean;
  receivedHint?: "tacit_knowledge" | "inspiration" | null;
}

export interface AgentProgress {
  registeredAt: number; // epoch ms
  experiments: AgentExperiment[]; // time = ms since registeredAt
  experimentIds: Set<string>;
  loaded: boolean;
  lastEventTime: number; // epoch ms of most recent appended experiment
}

interface AgentExperimentsResponse {
  agent_id: string;
  agent_name: string | null;
  registered_at: string | null;
  experiments: {
    id?: string;
    score: number;
    feasible: boolean;
    created_at: string;
    trajectory_id?: string | null;
    received_hint?: "tacit_knowledge" | "inspiration" | null;
    trajectory_deactivated?: boolean;
  }[];
}

export class AgentProgressStore {
  private loaded = new Map<string, AgentProgress>();
  private pending = new Map<string, unknown[]>();

  get(agentId: string): AgentProgress | undefined {
    return this.loaded.get(agentId);
  }

  clear(): void {
    this.loaded.clear();
    this.pending.clear();
  }

  // Idempotent: a second load() for the same agent is a no-op while the
  // first is still in flight (the `loaded` entry already says `loaded: true`).
  async load(apiUrl: string, agentId: string): Promise<void> {
    const existing = this.loaded.get(agentId);
    if (existing?.loaded) return;

    try {
      // Pin to the viewed challenge — without this, the server falls back
      // to its active challenge (resolve_challenge in server.py), so an
      // agent viewed on a non-active challenge returns zero experiments
      // and the per-agent tab shows "no attempts yet from <name>".
      const challenge = getViewedChallenge();
      const res = await fetch(
        `${apiUrl}/api/agent_experiments` +
          `?agent_id=${encodeURIComponent(agentId)}` +
          `&challenge=${encodeURIComponent(challenge)}`,
      );
      if (!res.ok) return;
      const data = (await res.json()) as AgentExperimentsResponse;

      // Drop the response if the user switched challenges while the fetch
      // was in flight. clear() runs synchronously on switch — without this
      // guard we'd write a stale entry back into the freshly-cleared map.
      if (getViewedChallenge() !== challenge) return;

      const registeredAt = data.registered_at
        ? new Date(data.registered_at).getTime()
        : Date.now();

      const experiments: AgentExperiment[] = data.experiments.map((e) => ({
        time: Math.max(0, new Date(e.created_at).getTime() - registeredAt),
        score: e.score,
        feasible: e.feasible,
        experimentId: e.id,
        trajectoryId: e.trajectory_id ?? null,
        trajectoryDeactivated: !!e.trajectory_deactivated,
        receivedHint: e.received_hint ?? null,
      }));

      const experimentIds = new Set(
        experiments
          .map((e) => e.experimentId)
          .filter((id): id is string => Boolean(id)),
      );

      const lastEventTime = data.experiments.length
        ? new Date(data.experiments[data.experiments.length - 1].created_at).getTime()
        : 0;

      const progress: AgentProgress = {
        registeredAt,
        experiments,
        experimentIds,
        loaded: true,
        lastEventTime,
      };

      // Merge any live events that landed while the history request was in-flight.
      const queued = this.pending.get(agentId) || [];
      for (const msg of queued) {
        this.appendToProgress(progress, msg);
      }
      this.pending.delete(agentId);

      this.loaded.set(agentId, progress);
    } catch {
      // leave unloaded; next tab visit will retry
    }
  }

  // Apply a live experiment_published message. Returns true iff it was added
  // to a fully-loaded agent's history (caller can use the return to decide
  // whether to redraw). Returns false if the message was queued for later
  // merge or was a duplicate.
  appendLive(msg: { agent_id?: string }): boolean {
    if (!msg.agent_id) return false;
    const progress = this.loaded.get(msg.agent_id);
    if (!progress || !progress.loaded) {
      const queued = this.pending.get(msg.agent_id) || [];
      queued.push(msg);
      this.pending.set(msg.agent_id, queued);
      return false;
    }
    return this.appendToProgress(progress, msg);
  }

  private appendToProgress(progress: AgentProgress, msg: unknown): boolean {
    const m = msg as {
      timestamp?: string;
      experiment_id?: unknown;
      feasible?: boolean;
      score: number;
    };
    const msgTime = m.timestamp ? new Date(m.timestamp).getTime() : Date.now();
    const experimentId = typeof m.experiment_id === "string" ? m.experiment_id : null;

    if (experimentId && progress.experimentIds.has(experimentId)) {
      return false;
    }

    const time = Math.max(0, msgTime - progress.registeredAt);
    const feasible = m.feasible !== false;

    progress.experiments.push({
      time,
      score: m.score,
      feasible,
      experimentId: experimentId || undefined,
    });
    if (experimentId) progress.experimentIds.add(experimentId);
    progress.lastEventTime = Math.max(progress.lastEventTime, msgTime);
    return true;
  }
}
