import type { Panel, WSMessage } from "../types";
import { formatTime } from "../lib/animate";
import { getAgentColor } from "../lib/colors";
import { formatScore } from "../lib/format";

const MAX_ITEMS = 200;

const EVENT_CONFIG: Record<string, { dot: string; icon: string }> = {
  agent_joined: { dot: "var(--cyan)", icon: "+" },
  hypothesis_proposed: { dot: "var(--purple)", icon: "?" },
  experiment_success: { dot: "var(--green)", icon: "✓" },
  experiment_fail: { dot: "var(--red)", icon: "✗" },
  new_global_best: { dot: "var(--amber)", icon: "★" },
  chat: { dot: "var(--text-dim)", icon: "…" },
};

// Per-item bookkeeping for in-place rename updates. The element holds the
// rendered text; agentId is the key we re-render by when an AgentRenamed
// event arrives; render() rebuilds .innerHTML using the current name from
// the lookup callback.
interface FeedItem {
  el: HTMLElement;
  agentId: string;
  // Used to keep items[] sorted newest-first across racing backfill sources.
  timestamp: string;
  render: (name: string) => void;
}

// Optional callback supplied by main.ts: resolves an agent_id to its current
// display name. When absent, items fall back to the agent_name snapshot on
// the event. Set via FeedPanel.setNameLookup() during construction.
export type NameLookup = (agent_id: string) => string | undefined;

export class FeedPanel implements Panel {
  private list!: HTMLElement;
  private items: FeedItem[] = [];
  private lookup: NameLookup | null = null;

  setNameLookup(lookup: NameLookup) {
    this.lookup = lookup;
  }

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner">
        <div class="panel-label">LIVE FEED</div>
        <div class="feed-list" id="feed-list"></div>
      </div>
    `;
    this.list = document.getElementById("feed-list")!;
  }

  private nameFor(agentId: string, fallback: string): string {
    return (agentId && this.lookup?.(agentId)) || fallback;
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.items.forEach(({ el }) => el.remove());
      this.items = [];
      this.list.innerHTML = "";
      return;
    }

    if (msg.type === "agent_renamed") {
      // Walk existing items, re-render any owned by this agent_id so the
      // displayed name updates without requiring a reload.
      for (const item of this.items) {
        if (item.agentId === msg.agent_id) item.render(msg.new_name);
      }
      return;
    }

    let render: (name: string) => string;
    let eventType = "";
    let agentId = "agent_id" in msg ? ((msg as any).agent_id as string) || "" : "";
    let fallbackName = "agent_name" in msg ? (msg as any).agent_name as string : "";

    switch (msg.type) {
      case "agent_joined":
        render = (name) => `<b>${name}</b> joined the swarm`;
        eventType = "agent_joined";
        break;
      case "hypothesis_proposed":
        render = (name) => `<b>${name}</b> proposed: "${msg.title}"`;
        eventType = "hypothesis_proposed";
        break;
      case "chat_message": {
        const content = msg.content;
        const isMilestone = msg.msg_type === "milestone";
        render = (name) => `<b>${name}</b>: ${content}`;
        eventType = isMilestone ? "new_global_best" : "chat";
        break;
      }
      case "experiment_published": {
        // Three outcomes:
        //   1. beats own best AND new global best → show both %s
        //   2. beats own best only → show own-best %
        //   3. no improvement → just the score
        // Server deltas are improvement-positive (positive = better score).
        // Show "+" green for improvement, "-" red for regression.
        const fmtDelta = (d: number | null | undefined): string => {
          if (d == null) return "";
          // Snap machine-precision blow-ups (server divides by a near-zero
          // previous best) to ∞% so we don't render misleading 1e16-style
          // values. Threshold is generous — 10000x change is the upper end
          // of any legitimate delta we'd ever care to display.
          if (!Number.isFinite(d) || Math.abs(d) > 1e6) {
            const s = d > 0 ? "+" : "-";
            const c = d > 0 ? "var(--green)" : "var(--red)";
            return `<span style="color:${c}">${s}∞%</span>`;
          }
          const sign = d > 0 ? "+" : "";
          const color = d > 0 ? "var(--green)" : d < 0 ? "var(--red)" : "var(--text-dim)";
          return `<span style="color:${color}">${sign}${d.toFixed(3)}%</span>`;
        };

        const ownDelta = msg.delta_vs_own_best_pct;
        const globalDelta = msg.delta_vs_best_pct;
        const beatsOwn = msg.beats_own_best === true;
        const score = formatScore(msg.score);

        if (msg.is_new_best) {
          const ownStr = ownDelta != null ? ` (${fmtDelta(ownDelta)} own)` : "";
          const globalStr = globalDelta != null ? ` ${fmtDelta(globalDelta)} vs global` : "";
          render = (name) => `<b>${name}</b> improved &mdash; ${score}${ownStr} · NEW GLOBAL BEST${globalStr}`;
          eventType = "new_global_best";
        } else if (beatsOwn) {
          const ownStr = ownDelta != null ? ` (${fmtDelta(ownDelta)})` : "";
          render = (name) => `<b>${name}</b> improvement &mdash; ${score}${ownStr}`;
          eventType = "experiment_success";
        } else {
          const ownStr = ownDelta != null ? ` (${fmtDelta(ownDelta)} vs own)` : "";
          render = (name) => `<b>${name}</b> no improvement &mdash; ${score}${ownStr}`;
          eventType = "experiment_fail";
        }
        break;
      }
      case "admin_broadcast":
        render = () => `<b>ADMIN</b>: ${msg.message}`;
        eventType = "new_global_best";
        agentId = "";
        break;
      default:
        return;
    }

    const config = EVENT_CONFIG[eventType] || EVENT_CONFIG.agent_joined;
    const agentColor = agentId ? getAgentColor(agentId) : config.dot;
    const rawTimestamp = "timestamp" in msg ? (msg.timestamp as string) : "";
    const timestamp = rawTimestamp ? formatTime(rawTimestamp) : "";

    const item = document.createElement("div");
    item.className = `feed-item ${eventType === "new_global_best" ? "feed-item--best" : ""}`;

    // Rebuild the item's text region using the (current) name. Stored as
    // a closure so agent_renamed handling can re-run it later. Pin the
    // text span via a stable class so we only touch the bit that needs
    // re-rendering.
    const writeText = (name: string) => {
      const safeName = name || fallbackName || "agent";
      item.innerHTML = `
        <span class="feed-time">${timestamp}</span>
        <span class="feed-dot" style="background:${agentColor}"></span>
        <span class="feed-icon">${config.icon}</span>
        <span class="feed-text">${render(safeName)}</span>
      `;
    };
    writeText(this.nameFor(agentId, fallbackName));

    // Find insertion index — items[] is kept sorted newest-first by ISO
    // timestamp. Without a timestamp (shouldn't happen for real events) we
    // fall back to "newest", i.e. the top.
    let insertIdx = 0;
    if (rawTimestamp) {
      while (insertIdx < this.items.length && this.items[insertIdx].timestamp > rawTimestamp) {
        insertIdx++;
      }
    }

    // Animate in
    item.style.transform = "translateY(-28px)";
    item.style.opacity = "0";
    if (insertIdx === 0) {
      this.list.prepend(item);
    } else if (insertIdx >= this.items.length) {
      this.list.appendChild(item);
    } else {
      this.list.insertBefore(item, this.items[insertIdx].el);
    }

    requestAnimationFrame(() => {
      item.style.transition = "transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.3s ease";
      item.style.transform = "translateY(0)";
      item.style.opacity = "1";
    });

    this.items.splice(insertIdx, 0, { el: item, agentId, timestamp: rawTimestamp, render: writeText });

    // Remove excess
    while (this.items.length > MAX_ITEMS) {
      const old = this.items.pop()!;
      old.el.remove();
    }
  }
}
