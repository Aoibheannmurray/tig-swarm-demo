import type { Panel, WSMessage, LeaderboardEntry, MainnetBaseline } from "../types";
import { getAgentColor } from "../lib/colors";
import { formatScore, shortenModel, escapeHTML } from "../lib/format";
import {
  isComparable, statusLabel, baselineRank, countBeating,
} from "../lib/mainnetBaseline";

type SortKey =
  | "current_score"
  | "best_ever_score"
  | "runs"
  | "improvements"
  | "runs_since_improvement"
  | "num_trajectories"
  | "tacit_knowledge_count"
  | "inspiration_count"
  | "failed_attempts_count";
type SortDir = "asc" | "desc";

// Default cap on rendered rows. The server returns every agent with a
// published experiment; the compact dashboard *tile* caps the rendered rows
// and lets the list scroll (see .leaderboard-list CSS) rather than grow
// unbounded. The dedicated full page overrides this with Infinity to show
// every participating agent (see constructor opts).
const DEFAULT_MAX_ROWS = 50;

const DEFAULT_DIR: Record<SortKey, SortDir> = {
  current_score: "desc",
  best_ever_score: "desc",
  runs: "desc",
  improvements: "desc",
  runs_since_improvement: "asc",
  num_trajectories: "desc",
  tacit_knowledge_count: "desc",
  inspiration_count: "desc",
  failed_attempts_count: "desc",
};

export class LeaderboardPanel implements Panel {
  private list!: HTMLElement;
  private currentEntries: LeaderboardEntry[] = [];
  private baseline: MainnetBaseline | null = null;
  private sortKey: SortKey = "best_ever_score";
  private sortDir: SortDir = "desc";
  private maxRows: number;

  // maxRows defaults to DEFAULT_MAX_ROWS (the tile). Pass { maxRows: Infinity }
  // on the dedicated page to render every participating agent.
  constructor(opts?: { maxRows?: number }) {
    this.maxRows = opts?.maxRows ?? DEFAULT_MAX_ROWS;
  }

  init(container: HTMLElement) {
    container.innerHTML = `
      <div class="panel-inner">
        <div class="panel-label">LEADERBOARD</div>
        <div class="leaderboard-header">
          <span class="lb-rank">#</span>
          <span class="lb-name">Agent</span>
          <span class="lb-model">Model</span>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="runs">Runs<span class="lb-arrow"></span></button>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="improvements">Imp<span class="lb-arrow"></span></button>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="runs_since_improvement">Stag<span class="lb-arrow"></span></button>
          <button type="button" class="lb-score lb-sortable" data-sort="current_score">Score<span class="lb-arrow"></span></button>
          <button type="button" class="lb-score lb-sortable" data-sort="best_ever_score">Best<span class="lb-arrow"></span></button>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="num_trajectories">Traj<span class="lb-arrow"></span></button>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="tacit_knowledge_count" title="Tacit knowledge reads">TK<span class="lb-arrow"></span></button>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="inspiration_count" title="Inspiration reads">Insp<span class="lb-arrow"></span></button>
          <button type="button" class="lb-col-sm lb-sortable" data-sort="failed_attempts_count" title="Failed-attempt recalls">FA<span class="lb-arrow"></span></button>
        </div>
        <div class="leaderboard-list" id="leaderboard-list"></div>
      </div>
    `;
    this.list = document.getElementById("leaderboard-list")!;

    container.querySelectorAll<HTMLButtonElement>(".lb-sortable").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.sort as SortKey;
        if (this.sortKey === key) {
          this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
        } else {
          this.sortKey = key;
          this.sortDir = DEFAULT_DIR[key];
        }
        this.render();
      });
    });

    this.updateHeaderIndicators();
  }

  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.currentEntries = [];
      this.list.innerHTML = "";
      return;
    }

    if (msg.type === "mainnet_baseline") {
      this.baseline = msg.baseline;
      this.render();
      return;
    }

    if (msg.type !== "leaderboard_update") return;
    this.currentEntries = msg.entries.slice();
    this.render();
  }

  // Which column the ghost row can be ranked against. Sorting by "runs" or
  // "stagnation" puts agents in an order the mainnet algorithm has no position
  // in — it has no runs — so the row is hidden rather than parked at a rank
  // that would misread as a claim.
  private baselineScoreKey(): "current_score" | "best_ever_score" | null {
    return this.sortKey === "current_score" || this.sortKey === "best_ever_score"
      ? this.sortKey
      : null;
  }

  private updateHeaderIndicators() {
    document.querySelectorAll<HTMLButtonElement>(".lb-sortable").forEach((btn) => {
      const isActive = btn.dataset.sort === this.sortKey;
      btn.classList.toggle("lb-sortable--active", isActive);
      const arrow = btn.querySelector<HTMLElement>(".lb-arrow")!;
      arrow.textContent = isActive ? (this.sortDir === "asc" ? " ↑" : " ↓") : "";
    });
  }

  private sortEntries(entries: LeaderboardEntry[]): LeaderboardEntry[] {
    const sorted = entries.slice();
    const dir = this.sortDir === "asc" ? 1 : -1;
    sorted.sort((a, b) => {
      const av = a[this.sortKey];
      const bv = b[this.sortKey];
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      return ((av as number) - (bv as number)) * dir;
    });
    return sorted;
  }

  // The mainnet algorithm as a pinned competitor. Deliberately a row in the
  // same table rather than a separate readout: the question people actually
  // have is "have we passed it", and a rank answers that without arithmetic.
  private buildGhostRow(scoreKey: "current_score" | "best_ever_score"): HTMLElement {
    const b = this.baseline!;
    const row = document.createElement("div");
    row.className = "leaderboard-row lb-mainnet";
    // No agent id: the FLIP pass keys on it, and a synthetic one would make
    // the row animate as if it were an agent changing rank.
    row.dataset.agentId = "";
    const beaten = countBeating(
      this.currentEntries, b.score!, b.direction, (e) => e[scoreKey],
    );
    const total = this.currentEntries.length;
    const scoreText = formatScore(b.score);
    // Column-for-column the same spans as a real agent row (rank, name,
    // model, 3 stats, 2 scores, 4 stats): the rows are flex with a
    // flexible name, so a missing fixed-width span widens the name and
    // shoves the score cells out from under their headers.
    row.innerHTML = `
      <span class="lb-rank lb-mainnet-mark">◆</span>
      <span class="lb-name">
        <span class="lb-mainnet-title">TIG MAINNET</span>
        <span class="lb-mainnet-sub">${escapeHTML(b.algorithm ?? "")}</span>
      </span>
      <span class="lb-model"></span>
      <span class="lb-col-sm"></span>
      <span class="lb-col-sm"></span>
      <span class="lb-col-sm"></span>
      <span class="lb-score">${scoreKey === "current_score" ? scoreText : ""}</span>
      <span class="lb-score">${scoreKey === "best_ever_score" ? scoreText : ""}</span>
      <span class="lb-col-sm"></span>
      <span class="lb-col-sm"></span>
      <span class="lb-col-sm"></span>
      <span class="lb-col-sm"></span>
    `;
    row.title = beaten
      ? `${beaten} of ${total} agents have beaten mainnet (${b.algorithm})`
      : `Nothing has beaten mainnet (${b.algorithm}) yet`;
    return row;
  }

  private render() {
    this.updateHeaderIndicators();

    const firstRects = new Map<string, DOMRect>();
    Array.from(this.list.children).forEach((child) => {
      const el = child as HTMLElement;
      firstRects.set(el.dataset.agentId || "", el.getBoundingClientRect());
    });

    const prevValues = new Map<string, number | null>();
    this.list.childNodes.forEach((node) => {
      const el = node as HTMLElement;
      const id = el.dataset.agentId || "";
      const v = el.dataset.sortValue;
      prevValues.set(id, v === "" || v === undefined ? null : Number(v));
    });

    const sorted = this.sortEntries(this.currentEntries).slice(0, this.maxRows);

    // Where the mainnet row belongs, computed against EVERY entry rather than
    // the truncated view: an agent scrolled off the tile has still beaten it,
    // and the count under the row must say so.
    const scoreKey = this.baselineScoreKey();
    const ghostAt = this.baseline && isComparable(this.baseline) && scoreKey
      ? baselineRank(
          this.sortEntries(this.currentEntries), this.baseline.score!,
          this.baseline.direction, (e) => e[scoreKey],
        )
      : null;

    this.list.innerHTML = "";
    sorted.forEach((entry, i) => {
      const rank = i + 1;
      if (ghostAt === rank) this.list.appendChild(this.buildGhostRow(scoreKey!));
      const row = document.createElement("div");
      row.className = `leaderboard-row${entry.active ? "" : " lb-inactive"}`;
      row.dataset.agentId = entry.agent_id;
      const sortVal = entry[this.sortKey];
      row.dataset.sortValue = sortVal === null ? "" : String(sortVal);

      const color = getAgentColor(entry.agent_id);

      const prev = prevValues.get(entry.agent_id);
      const goodDir = DEFAULT_DIR[this.sortKey];
      const improved =
        prev !== undefined && prev !== null && sortVal !== null &&
        ((goodDir === "asc" && (sortVal as number) < prev) ||
         (goodDir === "desc" && (sortVal as number) > prev));

      const curText = formatScore(entry.current_score);
      const bestText = formatScore(entry.best_ever_score);
      const scoreImproved = improved && (this.sortKey === "current_score" || this.sortKey === "best_ever_score");

      // Show the shortened model name in the cell; keep the full provider-
      // prefixed id in the tooltip so the exact model is still recoverable.
      const llmFull = entry.llm_type ? escapeHTML(entry.llm_type) : "";
      const llmText = entry.llm_type ? escapeHTML(shortenModel(entry.llm_type)) : "";
      row.innerHTML = `
        <span class="lb-rank">${rank}</span>
        <span class="lb-name">
          <span class="lb-dot" style="background:${color}"></span>
          ${escapeHTML(entry.agent_name)}
        </span>
        <span class="lb-model" title="${llmFull}">${llmText}</span>
        <span class="lb-col-sm">${entry.runs}</span>
        <span class="lb-col-sm">${entry.improvements}</span>
        <span class="lb-col-sm${entry.runs_since_improvement >= 2 ? " lb-stag--alert" : ""}">${entry.runs_since_improvement}</span>
        <span class="lb-score ${scoreImproved && this.sortKey === "current_score" ? "lb-score--improved" : ""}">${curText}</span>
        <span class="lb-score ${scoreImproved && this.sortKey === "best_ever_score" ? "lb-score--improved" : ""}">${bestText}</span>
        <span class="lb-col-sm">${entry.num_trajectories}</span>
        <span class="lb-col-sm">${entry.tacit_knowledge_count}</span>
        <span class="lb-col-sm">${entry.inspiration_count}</span>
        <span class="lb-col-sm">${entry.failed_attempts_count ?? 0}</span>
      `;

      this.list.appendChild(row);
    });

    // Everyone in view has beaten it — the row belongs at the bottom, and
    // dropping it would silently remove the good news.
    if (ghostAt !== null && ghostAt > sorted.length) {
      this.list.appendChild(this.buildGhostRow(scoreKey!));
    }

    // FLIP animation for reordered rows
    if (firstRects.size > 0) {
      Array.from(this.list.children).forEach((child) => {
        const el = child as HTMLElement;
        const agentId = el.dataset.agentId || "";
        const first = firstRects.get(agentId);
        if (!first) {
          el.style.opacity = "0";
          el.style.transform = "translateX(20px)";
          requestAnimationFrame(() => {
            el.style.transition = "opacity 0.4s ease, transform 0.4s ease";
            el.style.opacity = "1";
            el.style.transform = "translateX(0)";
            setTimeout(() => { el.style.transition = ""; }, 400);
          });
          return;
        }

        const last = el.getBoundingClientRect();
        const deltaY = first.top - last.top;
        if (Math.abs(deltaY) < 1) return;

        el.style.transform = `translateY(${deltaY}px)`;
        el.style.transition = "none";

        requestAnimationFrame(() => {
          el.style.transition = "transform 0.5s cubic-bezier(0.4, 0, 0.2, 1)";
          el.style.transform = "";
          setTimeout(() => { el.style.transition = ""; }, 500);
        });
      });
    }
  }
}
