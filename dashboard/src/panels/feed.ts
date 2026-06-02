import type { Panel, WSMessage } from "../types";
import { formatTime } from "../lib/animate";
import { getAgentColor, NEUTRAL_AGENT_COLOR } from "../lib/colors";
import { formatScore } from "../lib/format";

// Base in-memory/DOM cap for the live feed. "Load older" raises the effective
// cap (this.maxItems) by the page size so paged-in history isn't immediately
// trimmed away. Each row is ~2-4 KB, so even a few thousand stays cheap.
const MAX_ITEMS = 200;
const OLDER_PAGE = 100;

// Event glyph only — colors are *not* set per event type. The dot always
// paints with the agent's own palette color (assigned globally when the
// agent registers; see lib/colors.ts and main.ts). This keeps every panel
// — feed, leaderboard, chart, diversity — visually consistent for a given
// agent regardless of which kind of event the feed line is reporting.
const EVENT_ICON: Record<string, string> = {
  agent_joined: "+",
  hypothesis_proposed: "?",
  experiment_success: "✓",
  experiment_fail: "✗",
  new_global_best: "★",
  chat: "…",
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
  private maxItems = MAX_ITEMS;
  // Oldest `messages`-table timestamp currently loaded — the cursor for
  // "load older". Updated from chat_message / agent_joined events (the events
  // that correspond to message rows). Synthesised experiment/hypothesis feed
  // lines aren't pageable, so they don't move this cursor.
  private oldestMessageTs: string | null = null;
  private loadOlderBtn: HTMLButtonElement | null = null;
  private loadingOlder = false;
  private noMoreHistory = false;
  private apiUrl = "";
  private getChallenge: (() => string) | null = null;

  setNameLookup(lookup: NameLookup) {
    this.lookup = lookup;
  }

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner panel-inner--feed">
        <div class="panel-label">LIVE FEED</div>
        <div class="feed-list" id="feed-list"></div>
        <button type="button" class="feed-load-older" id="feed-load-older" hidden>
          Load older
        </button>
      </div>
    `;
    this.list = document.getElementById("feed-list")!;
    this.loadOlderBtn = document.getElementById(
      "feed-load-older",
    ) as HTMLButtonElement;
    this.loadOlderBtn.addEventListener("click", () => void this.loadOlder());
  }

  /** Wire up "load older" paging. Called by the dashboard once it knows the
   *  API base and how to resolve the currently-viewed challenge. */
  enableLoadOlder(apiUrl: string, getChallenge: () => string) {
    this.apiUrl = apiUrl;
    this.getChallenge = getChallenge;
    this.updateLoadOlderBtn();
  }

  private updateLoadOlderBtn() {
    if (!this.loadOlderBtn) return;
    const show =
      !!this.getChallenge &&
      !this.noMoreHistory &&
      this.oldestMessageTs !== null;
    this.loadOlderBtn.hidden = !show;
    this.loadOlderBtn.disabled = this.loadingOlder;
    this.loadOlderBtn.textContent = this.loadingOlder
      ? "Loading…"
      : "Load older";
  }

  private async loadOlder() {
    if (this.loadingOlder || this.noMoreHistory || !this.getChallenge) return;
    if (this.oldestMessageTs === null) return;
    this.loadingOlder = true;
    this.updateLoadOlderBtn();

    const challenge = this.getChallenge();
    // Preserve the scroll position: older rows append at the bottom, so we
    // anchor on the distance from the bottom and restore it afterwards.
    const prevFromBottom = this.list.scrollHeight - this.list.scrollTop;
    try {
      const url =
        `${this.apiUrl}/api/messages?limit=${OLDER_PAGE}` +
        `&challenge=${encodeURIComponent(challenge)}` +
        `&before=${encodeURIComponent(this.oldestMessageTs)}`;
      const res = await fetch(url);
      const rows: Array<{
        id: string;
        agent_id: string | null;
        agent_name: string;
        content: string;
        msg_type: string;
        created_at: string;
      }> = res.ok ? await res.json() : [];

      // Stale guard: the user may have switched challenges mid-fetch.
      if (this.getChallenge() !== challenge) return;

      // Raise the cap so the paged-in rows survive the trim, then dispatch
      // oldest-first (handleMessage keeps items[] sorted by timestamp).
      this.maxItems += rows.length;
      rows.sort((a, b) => a.created_at.localeCompare(b.created_at));
      for (const row of rows) {
        if (row.msg_type === "agent_joined") {
          this.handleMessage({
            type: "agent_joined",
            agent_id: row.agent_id || "",
            agent_name: row.agent_name,
            timestamp: row.created_at,
          } as WSMessage);
        } else {
          this.handleMessage({
            type: "chat_message",
            message_id: row.id,
            agent_id: row.agent_id,
            agent_name: row.agent_name,
            content: row.content,
            msg_type: row.msg_type === "milestone" ? "milestone" : "agent",
            timestamp: row.created_at,
          } as WSMessage);
        }
      }
      // A short page means we've reached the start of history.
      if (rows.length < OLDER_PAGE) this.noMoreHistory = true;

      this.list.scrollTop = this.list.scrollHeight - prevFromBottom;
    } catch (e) {
      console.warn("[Feed] load older failed:", e);
    } finally {
      this.loadingOlder = false;
      this.updateLoadOlderBtn();
    }
  }

  private nameFor(agentId: string, fallback: string): string {
    return (agentId && this.lookup?.(agentId)) || fallback;
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.items.forEach(({ el }) => el.remove());
      this.items = [];
      this.list.innerHTML = "";
      this.maxItems = MAX_ITEMS;
      this.oldestMessageTs = null;
      this.noMoreHistory = false;
      this.updateLoadOlderBtn();
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

        const ownDelta = msg.delta_vs_trajectory_best_pct;
        const globalDelta = msg.delta_vs_best_pct;
        const beatsOwn = msg.beats_trajectory_best === true;
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

    const icon = EVENT_ICON[eventType] ?? EVENT_ICON.chat;
    const agentColor = agentId ? getAgentColor(agentId) : NEUTRAL_AGENT_COLOR;
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
        <span class="feed-icon">${icon}</span>
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

    // Advance the "load older" cursor for events that map to message rows
    // (chat + agent_joined). Track the OLDEST such timestamp seen.
    if (
      rawTimestamp &&
      (msg.type === "chat_message" || msg.type === "agent_joined")
    ) {
      if (this.oldestMessageTs === null || rawTimestamp < this.oldestMessageTs) {
        const first = this.oldestMessageTs === null;
        this.oldestMessageTs = rawTimestamp;
        if (first) this.updateLoadOlderBtn();
      }
    }

    // Remove excess
    while (this.items.length > this.maxItems) {
      const old = this.items.pop()!;
      old.el.remove();
    }
  }
}
