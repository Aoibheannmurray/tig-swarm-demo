import type { WSMessage } from "../../types";
import { getAgentColor } from "../../lib/colors";
import { formatTime } from "../../lib/animate";

interface FeedItem {
  id: string;
  agentName: string;
  agentId: string;
  content: string;
  msgType: "agent" | "milestone";
  timestamp: string;
}

// Base cap for the research feed. "Load older" raises the effective cap
// (this.maxItems) by the number of paged-in rows so history isn't trimmed.
const MAX_FEED_ITEMS = 40;
const OLDER_PAGE = 60;

// Tracks element + timestamp so we can keep the feed sorted newest-first
// across racing backfill sources (chat history, hypothesis replay, etc.).
interface RenderedItem {
  el: HTMLElement;
  timestamp: string;
}

export class IdeasTree {
  private feedEl!: HTMLElement;
  private feedItems: RenderedItem[] = [];
  private statsEl!: HTMLElement;
  private hypothesisCount = 0;
  private succeededCount = 0;
  private failedCount = 0;
  private messageCount = 0;
  private maxItems = MAX_FEED_ITEMS;
  // "Load older" pages back through two REST sources: chat messages and
  // hypotheses. Track the oldest timestamp loaded from each as its cursor.
  private oldestMessageTs: string | null = null;
  private oldestHypothesisTs: string | null = null;
  private msgHistoryDone = false;
  private hypHistoryDone = false;
  private loadOlderBtn: HTMLButtonElement | null = null;
  private loadingOlder = false;
  private apiUrl = "";
  private getChallenge: (() => string) | null = null;

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="ideas-page">
        <div class="ideas-header">
          <div class="ideas-title">
            <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />
            <span class="ideas-title-text">Insight Library</span>
          </div>
          <div class="ideas-nav">
            <a href="/" class="ideas-nav-link">Dashboard</a>
            <span class="ideas-nav-active">Ideas</span>
            <a href="/diversity.html" class="ideas-nav-link">Diversity</a>
            <a href="/benchmark.html" class="ideas-nav-link">Benchmark</a>
            <a href="/trajectories.html" class="ideas-nav-link">Trajectories</a>
          </div>
        </div>

        <div class="ideas-body">
          <div class="ideas-feed-col">
            <div class="ideas-col-label">RESEARCH FEED</div>
            <div class="ideas-feed" id="ideas-feed"></div>
            <button type="button" class="feed-load-older" id="ideas-load-older" hidden>
              Load older
            </button>
          </div>
          <div class="ideas-right-col" id="strategy-lb-mount"></div>
        </div>

        <div class="ideas-stats" id="ideas-stats"></div>
      </div>
    `;

    this.feedEl = document.getElementById("ideas-feed")!;
    this.statsEl = document.getElementById("ideas-stats")!;
    this.loadOlderBtn = document.getElementById(
      "ideas-load-older",
    ) as HTMLButtonElement;
    this.loadOlderBtn.addEventListener("click", () => void this.loadOlder());
  }

  /** Wire up "load older" paging once the API base + challenge resolver
   *  are known (called by pages/ideas/main.ts). */
  enableLoadOlder(apiUrl: string, getChallenge: () => string) {
    this.apiUrl = apiUrl;
    this.getChallenge = getChallenge;
    this.updateLoadOlderBtn();
  }

  private updateLoadOlderBtn() {
    if (!this.loadOlderBtn) return;
    const hasCursor =
      this.oldestMessageTs !== null || this.oldestHypothesisTs !== null;
    const exhausted = this.msgHistoryDone && this.hypHistoryDone;
    const show = !!this.getChallenge && hasCursor && !exhausted;
    this.loadOlderBtn.hidden = !show;
    this.loadOlderBtn.disabled = this.loadingOlder;
    this.loadOlderBtn.textContent = this.loadingOlder ? "Loading…" : "Load older";
  }

  private async loadOlder() {
    if (this.loadingOlder || !this.getChallenge) return;
    this.loadingOlder = true;
    this.updateLoadOlderBtn();

    const challenge = this.getChallenge();
    const ch = encodeURIComponent(challenge);
    const prevFromBottom = this.feedEl.scrollHeight - this.feedEl.scrollTop;

    try {
      // Page both sources in parallel; each advances its own cursor.
      const reqs: Promise<void>[] = [];

      if (!this.msgHistoryDone && this.oldestMessageTs !== null) {
        const url =
          `${this.apiUrl}/api/messages?limit=${OLDER_PAGE}&challenge=${ch}` +
          `&before=${encodeURIComponent(this.oldestMessageTs)}`;
        reqs.push(
          fetch(url)
            .then((r) => (r.ok ? r.json() : []))
            .then((rows: any[]) => {
              if (this.getChallenge!() !== challenge) return;
              this.maxItems += rows.length;
              rows.sort((a, b) => a.created_at.localeCompare(b.created_at));
              for (const m of rows) {
                this.handleMessage({
                  type: "chat_message",
                  challenge,
                  message_id: m.id,
                  agent_name: m.agent_name,
                  agent_id: m.agent_id,
                  content: m.content,
                  msg_type: m.msg_type,
                  timestamp: m.created_at,
                } as WSMessage);
              }
              if (rows.length < OLDER_PAGE) this.msgHistoryDone = true;
            }),
        );
      }

      if (!this.hypHistoryDone && this.oldestHypothesisTs !== null) {
        const url =
          `${this.apiUrl}/api/hypotheses?limit=${OLDER_PAGE}&challenge=${ch}` +
          `&before=${encodeURIComponent(this.oldestHypothesisTs)}`;
        reqs.push(
          fetch(url)
            .then((r) => (r.ok ? r.json() : []))
            .then((rows: any[]) => {
              if (this.getChallenge!() !== challenge) return;
              this.maxItems += rows.length;
              rows.sort((a, b) => a.created_at.localeCompare(b.created_at));
              for (const h of rows) {
                this.handleMessage({
                  type: "hypothesis_proposed",
                  hypothesis_id: h.id,
                  agent_name: h.agent_name,
                  agent_id: h.agent_id || "",
                  title: h.title,
                  description: h.description || "",
                  strategy_tag: h.strategy_tag,
                  parent_hypothesis_id: h.parent_hypothesis_id || null,
                  timestamp: h.created_at,
                } as WSMessage);
              }
              if (rows.length < OLDER_PAGE) this.hypHistoryDone = true;
            }),
        );
      }

      await Promise.all(reqs);
      this.feedEl.scrollTop = this.feedEl.scrollHeight - prevFromBottom;
    } catch (e) {
      console.warn("[Ideas] load older failed:", e);
    } finally {
      this.loadingOlder = false;
      this.updateLoadOlderBtn();
    }
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.feedItems.forEach((item) => item.el.remove());
      this.feedItems = [];
      this.feedEl.innerHTML = "";
      this.hypothesisCount = 0;
      this.succeededCount = 0;
      this.failedCount = 0;
      this.messageCount = 0;
      this.maxItems = MAX_FEED_ITEMS;
      this.oldestMessageTs = null;
      this.oldestHypothesisTs = null;
      this.msgHistoryDone = false;
      this.hypHistoryDone = false;
      this.updateStats();
      this.updateLoadOlderBtn();
      return;
    }

    switch (msg.type) {
      case "chat_message":
        this.addFeedItem({
          id: msg.message_id,
          agentName: msg.agent_name,
          agentId: msg.agent_id || "",
          content: msg.content,
          msgType: msg.msg_type,
          timestamp: msg.timestamp,
        });
        this.trackOldest("message", msg.timestamp);
        this.messageCount++;
        break;

      case "hypothesis_proposed":
        this.hypothesisCount++;
        this.addFeedItem({
          id: msg.hypothesis_id,
          agentName: msg.agent_name,
          agentId: msg.agent_id,
          content: `Proposed: "${msg.title}"`,
          msgType: "agent",
          timestamp: msg.timestamp,
        });
        this.trackOldest("hypothesis", msg.timestamp);
        break;

      case "hypothesis_status_changed":
        if (msg.new_status === "succeeded") this.succeededCount++;
        if (msg.new_status === "failed") this.failedCount++;
        break;

      case "experiment_published": {
        // Three outcomes: new global best → milestone with own + global %s;
        // beats own best but not global → lightweight "improvement" post;
        // no improvement → skip (research feed stays narrative-focused).
        const fmtPct = (p: number | null | undefined): string => {
          if (p == null) return "";
          const sign = p >= 0 ? "-" : "+";
          return `${sign}${Math.abs(p).toFixed(2)}%`;
        };

        if (msg.is_new_best) {
          const ownPart = msg.delta_vs_own_best_pct != null
            ? ` (${fmtPct(msg.delta_vs_own_best_pct)} own)`
            : "";
          const globalPart = msg.delta_vs_best_pct != null
            ? ` and NEW GLOBAL BEST (${fmtPct(msg.delta_vs_best_pct)} vs global)`
            : " and NEW GLOBAL BEST";
          this.addFeedItem({
            id: msg.experiment_id,
            agentName: msg.agent_name,
            agentId: msg.agent_id,
            content: `Improvement — Score ${msg.score.toFixed(1)}${ownPart}${globalPart}`,
            msgType: "milestone",
            timestamp: msg.timestamp,
          });
        } else if (msg.beats_own_best === true) {
          const ownPart = msg.delta_vs_own_best_pct != null
            ? ` (${fmtPct(msg.delta_vs_own_best_pct)})`
            : "";
          this.addFeedItem({
            id: msg.experiment_id,
            agentName: msg.agent_name,
            agentId: msg.agent_id,
            content: `Improvement — Score ${msg.score.toFixed(1)}${ownPart}`,
            msgType: "agent",
            timestamp: msg.timestamp,
          });
        }
        break;
      }
    }

    this.updateStats();
  }

  private addFeedItem(item: FeedItem) {
    const el = document.createElement("div");
    el.className = `feed-post feed-post--${item.msgType}`;

    const agentColor = getAgentColor(item.agentId || item.agentName);
    const time = formatTime(item.timestamp);

    if (item.msgType === "milestone") {
      el.innerHTML = `
        <div class="feed-post-header">
          <span class="feed-post-badge milestone-badge">&#9733; MILESTONE</span>
          <span class="feed-post-time">${time}</span>
        </div>
        <div class="feed-post-content milestone-content">${item.content}</div>
        <div class="feed-post-author">
          <span class="feed-post-dot" style="background:${agentColor}"></span>
          ${item.agentName}
        </div>
      `;
    } else {
      el.innerHTML = `
        <div class="feed-post-agent">
          <span class="feed-post-dot" style="background:${agentColor}"></span>
          <span class="feed-post-name">${item.agentName}</span>
          <span class="feed-post-time">${time}</span>
        </div>
        <div class="feed-post-content">${item.content}</div>
      `;
    }

    // Keep feedItems sorted newest-first by ISO timestamp so racing backfill
    // sources don't end up out of order. Live messages typically hit idx 0.
    let insertIdx = 0;
    if (item.timestamp) {
      while (
        insertIdx < this.feedItems.length &&
        this.feedItems[insertIdx].timestamp > item.timestamp
      ) {
        insertIdx++;
      }
    }

    el.style.opacity = "0";
    el.style.transform = "translateY(-16px)";
    if (insertIdx === 0) {
      this.feedEl.prepend(el);
    } else if (insertIdx >= this.feedItems.length) {
      this.feedEl.appendChild(el);
    } else {
      this.feedEl.insertBefore(el, this.feedItems[insertIdx].el);
    }
    requestAnimationFrame(() => {
      el.style.transition = "opacity 0.35s ease, transform 0.35s ease";
      el.style.opacity = "1";
      el.style.transform = "translateY(0)";
    });

    this.feedItems.splice(insertIdx, 0, { el, timestamp: item.timestamp });

    while (this.feedItems.length > this.maxItems) {
      const old = this.feedItems.pop()!;
      old.el.remove();
    }
  }

  /** Advance the oldest-loaded cursor for a "load older" source. */
  private trackOldest(source: "message" | "hypothesis", ts: string) {
    if (!ts) return;
    if (source === "message") {
      if (this.oldestMessageTs === null || ts < this.oldestMessageTs) {
        const first = this.oldestMessageTs === null;
        this.oldestMessageTs = ts;
        if (first) this.updateLoadOlderBtn();
      }
    } else {
      if (this.oldestHypothesisTs === null || ts < this.oldestHypothesisTs) {
        const first = this.oldestHypothesisTs === null;
        this.oldestHypothesisTs = ts;
        if (first) this.updateLoadOlderBtn();
      }
    }
  }

  private updateStats() {
    const active = this.hypothesisCount - this.succeededCount - this.failedCount;
    this.statsEl.innerHTML = `
      <span class="ideas-stat">HYPOTHESES <b>${this.hypothesisCount}</b></span>
      <span class="ideas-stat">SUCCEEDED <b style="color:var(--green)">${this.succeededCount}</b></span>
      <span class="ideas-stat">FAILED <b style="color:var(--red)">${this.failedCount}</b></span>
      <span class="ideas-stat">ACTIVE <b style="color:var(--cyan)">${Math.max(0, active)}</b></span>
      <span class="ideas-stat">MESSAGES <b>${this.messageCount}</b></span>
    `;
  }
}
