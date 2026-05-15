// Shared scaffolding for the per-challenge visualization panels.
//
// Every per-challenge panel in this folder extends DisplayPanelBase. The
// base class owns the bits that used to duplicate across every panel:
// history entries, replay fetch, instance rotation, empty-state toggle,
// agent-name rendering, score-delta formatting, LIVE-button wiring.
//
// A subclass must implement:
//   - idPrefix:        unique prefix for DOM ids (e.g. "sat", "knapsack")
//   - scaffoldHtml():  the panel's HTML skeleton
//   - attachRefs():    wire DOM references after the scaffold is mounted
//   - showInstance():  render one instance into the panel's chart area
//
// Subclasses can additionally override:
//   - onAfterApplyHistory(): called after history index is applied (e.g.
//     the VRP panel uses this to recompute its tight viewBox)
//   - onReset():             called from the reset handler so subclasses
//     can clear extra chart groups / sub-stat boxes
//   - formatScoreDelta():    panels with "lower is better" semantics
//     override to flip the sign

import type { Panel, WSMessage } from "../types";
import { liveSwitchToActive, shouldShowLiveButton } from "../lib/panelLive";
import { getAgentColor } from "../lib/colors";
import { formatScore } from "../lib/format";
import type { Challenge } from "./registry";

export interface DisplayHistoryEntry<TInstances> {
  experiment_id: string;
  agent_name: string;
  agent_id?: string;
  score: number;
  solution_data: TInstances;
  created_at: string;
}

export abstract class DisplayPanelBase<TInstances extends Record<string, any>>
  implements Panel
{
  protected readonly challenge: Challenge;
  protected apiUrl = "";

  // DOM refs — subclasses populate these in attachRefs().
  protected scoreEl!: HTMLElement;
  protected scoreDeltaEl!: HTMLElement;
  protected instanceLabelEl!: HTMLElement;
  protected navEl!: HTMLElement;
  protected agentNameEl!: HTMLElement;
  protected historyNavEl!: HTMLElement;
  protected historyLabelEl!: HTMLElement;
  protected historyLiveBtnEl!: HTMLElement;
  protected emptyStateEl!: HTMLElement;

  protected allInstances: TInstances = {} as TInstances;
  protected currentIndex = 0;
  protected rawScore: number | null = null;
  protected historyEntries: DisplayHistoryEntry<TInstances>[] = [];
  protected historyIndex = -1;
  protected historyLoaded = false;
  protected rotationTimer: ReturnType<typeof setInterval> | null = null;
  // ResizeObservers (and similar) registered by subclasses via
  // observeResize(); disconnected automatically in dispose() so a challenge
  // switch doesn't leak observers attached to the previous panel's DOM.
  private resizeObservers: ResizeObserver[] = [];

  // Root .panel-inner element + the overlay we inject for the new-best
  // flash animation. Both are set up in init() so every subclass gets
  // the flash for free without touching its scaffoldHtml.
  private panelInnerEl: HTMLElement | null = null;
  private flashOverlayEl: HTMLElement | null = null;
  private flashTimer: ReturnType<typeof setTimeout> | null = null;

  protected abstract idPrefix: string;
  protected abstract scaffoldHtml(): string;
  protected abstract attachRefs(root: HTMLElement): void;
  protected abstract showInstance(data: TInstances[keyof TInstances]): void;

  // Hooks — override as needed.
  protected onAfterApplyHistory(): void {}
  protected onReset(): void {}

  // Universal scaffold for the BEST/LIVE history nav + instance nav. Each
  // subclass drops `${this.navsScaffold()}` into its scaffoldHtml() so both
  // nav rows render inside `.solution-navs`, which is absolutely positioned
  // at the top-center of `.panel-inner` by the shared style.css rule.
  protected navsScaffold(): string {
    const p = this.idPrefix;
    return `
      <div class="solution-navs">
        <div class="solution-history-nav" id="${p}-history-nav" style="display:none">
          <button class="solution-nav-btn" id="${p}-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="${p}-history-label"></span>
          <button class="solution-nav-btn" id="${p}-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="${p}-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="${p}-nav" style="display:none">
          <button class="solution-nav-btn" id="${p}-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="${p}-instance-label"></span>
          <button class="solution-nav-btn" id="${p}-next">&rsaquo;</button>
        </div>
      </div>
    `;
  }
  // Called from dispose() — subclasses release their own per-instance
  // listeners/observers (e.g. ResizeObserver) here.
  protected onDispose(): void {}

  constructor(challenge: string) {
    this.challenge = challenge as Challenge;
  }

  protected get instanceKeys(): string[] {
    return Object.keys(this.allInstances).sort();
  }

  protected isAtLatest(): boolean {
    return (
      this.historyEntries.length === 0 ||
      this.historyIndex >= this.historyEntries.length - 1
    );
  }

  init(container: HTMLElement) {
    container.innerHTML = this.scaffoldHtml();
    this.attachRefs(container);

    // Inject a transparent overlay over the panel-inner for the
    // new-best flash. Appended last so it stacks above sibling content
    // without needing per-panel z-index plumbing.
    this.panelInnerEl = container.querySelector(".panel-inner") as HTMLElement | null;
    if (this.panelInnerEl) {
      const overlay = document.createElement("div");
      overlay.className = "panel-flash-overlay";
      overlay.setAttribute("aria-hidden", "true");
      this.panelInnerEl.appendChild(overlay);
      this.flashOverlayEl = overlay;
    }

    // Generic button wiring. Subclasses must use the agreed id convention:
    //   {idPrefix}-prev, -next                     instance navigation
    //   {idPrefix}-hist-prev, -hist-next, -hist-live   history navigation
    container
      .querySelector(`#${this.idPrefix}-prev`)
      ?.addEventListener("click", () => this.navigate(-1));
    container
      .querySelector(`#${this.idPrefix}-next`)
      ?.addEventListener("click", () => this.navigate(1));
    container
      .querySelector(`#${this.idPrefix}-hist-prev`)
      ?.addEventListener("click", () => this.navigateHistory(-1));
    container
      .querySelector(`#${this.idPrefix}-hist-next`)
      ?.addEventListener("click", () => this.navigateHistory(1));
    this.historyLiveBtnEl?.addEventListener("click", () => {
      if (liveSwitchToActive(this.challenge)) return;
      if (!this.historyEntries.length) return;
      this.historyIndex = this.historyEntries.length - 1;
      this.applyHistoryEntry();
    });

    this.apiUrl = resolveApiUrl();

    this.rotationTimer = setInterval(() => {
      // Skip rotation while the tab is hidden so we're not redrawing SVG
      // into a non-visible page. Browsers already throttle setInterval to
      // ~1Hz in hidden tabs, but the guard avoids wasted work either way.
      if (document.hidden) return;
      if (this.instanceKeys.length > 1) this.navigate(1);
    }, 24000);

    void this.fetchHistory();
  }

  dispose(): void {
    if (this.rotationTimer !== null) {
      clearInterval(this.rotationTimer);
      this.rotationTimer = null;
    }
    if (this.flashTimer !== null) {
      clearTimeout(this.flashTimer);
      this.flashTimer = null;
    }
    for (const ro of this.resizeObservers) ro.disconnect();
    this.resizeObservers = [];
    this.onDispose();
  }

  // Briefly pulse the panel edges to signal that a new global best just
  // arrived. Colour comes from the contributing agent (falls back to the
  // theme's success green) so a glance at any panel tells you who pushed.
  protected flashNewBest(agentColor?: string): void {
    const overlay = this.flashOverlayEl;
    if (!overlay) return;
    if (this.flashTimer !== null) {
      clearTimeout(this.flashTimer);
      this.flashTimer = null;
    }
    overlay.classList.remove("flash-new-best");
    overlay.style.setProperty("--flash-color", agentColor ?? "var(--green)");
    // Force a reflow so re-adding the class restarts the animation when
    // bests arrive back-to-back.
    void overlay.offsetWidth;
    overlay.classList.add("flash-new-best");
    this.flashTimer = setTimeout(() => {
      overlay.classList.remove("flash-new-best");
      this.flashTimer = null;
    }, 700);
  }

  // Subclass helper — registers a ResizeObserver that's auto-disconnected
  // when the panel is disposed.
  protected observeResize(target: Element, callback: () => void): void {
    const ro = new ResizeObserver(callback);
    ro.observe(target);
    this.resizeObservers.push(ro);
  }

  protected async fetchHistory() {
    try {
      const res = await fetch(
        `${this.apiUrl}/api/replay?challenge=${encodeURIComponent(this.challenge)}`,
      );
      if (!res.ok) return;
      const rows: any[] = await res.json();
      const fetched: DisplayHistoryEntry<TInstances>[] = rows
        .filter((r) => r && r.solution_data)
        .map((r) => ({
          experiment_id: r.experiment_id,
          agent_name: r.agent_name,
          agent_id: r.agent_id,
          score: r.score,
          solution_data: r.solution_data as TInstances,
          created_at: r.created_at,
        }));
      const existingIds = new Set(
        this.historyEntries.map((e) => e.experiment_id),
      );
      // Remember which entry the user is looking at, so we can restore the
      // index after the merge+sort possibly shifts it (e.g. older entries
      // get prepended and push the current entry further down the list).
      const wasAtLatest = this.isAtLatest();
      const currentEntryId =
        this.historyIndex >= 0
          ? this.historyEntries[this.historyIndex]?.experiment_id
          : undefined;
      const merged = [
        ...fetched.filter((e) => !existingIds.has(e.experiment_id)),
        ...this.historyEntries,
      ];
      merged.sort((a, b) =>
        (a.created_at || "").localeCompare(b.created_at || ""),
      );
      this.historyEntries = merged;
      if (wasAtLatest && this.historyEntries.length) {
        this.historyIndex = this.historyEntries.length - 1;
        this.applyHistoryEntry();
      } else if (currentEntryId !== undefined) {
        const restored = this.historyEntries.findIndex(
          (e) => e.experiment_id === currentEntryId,
        );
        if (restored >= 0) this.historyIndex = restored;
      }
      this.historyLoaded = true;
      this.updateHistoryLabel();
      this.updateEmptyState();
    } catch {
      this.historyLoaded = true;
      this.updateEmptyState();
    }
  }

  protected navigateHistory(delta: number) {
    if (!this.historyEntries.length) return;
    const next = Math.max(
      0,
      Math.min(this.historyEntries.length - 1, this.historyIndex + delta),
    );
    if (next === this.historyIndex) return;
    this.historyIndex = next;
    this.applyHistoryEntry();
  }

  protected applyHistoryEntry() {
    const entry = this.historyEntries[this.historyIndex];
    if (!entry) return;

    this.rawScore = entry.score;
    this.allInstances = entry.solution_data;
    this.onAfterApplyHistory();

    this.agentNameEl.textContent = entry.agent_name;
    this.agentNameEl.style.color = entry.agent_id
      ? getAgentColor(entry.agent_id)
      : "";

    const keys = this.instanceKeys;
    if (this.currentIndex >= keys.length) this.currentIndex = 0;
    this.updateInstanceLabel();
    if (keys.length > 0) {
      this.showInstance(this.allInstances[keys[this.currentIndex]]);
    }

    this.scoreEl.textContent = formatScore(entry.score);

    if (this.historyIndex > 0) {
      const prev = this.historyEntries[this.historyIndex - 1];
      this.formatScoreDelta(entry.score, prev.score);
    } else {
      this.scoreDeltaEl.textContent = "first global best";
      this.scoreDeltaEl.style.color = "var(--text-dim)";
    }

    this.updateHistoryLabel();
    this.updateEmptyState();
  }

  // Default formatter: shows raw % delta with the sign of the score change.
  // VRP overrides this to flip sign (lower distance = improvement).
  protected formatScoreDelta(currentScore: number, prevScore: number) {
    // A prevScore of exactly 0 — or near-zero machine-precision noise from
    // mean-over-instances aggregation when every instance failed — would
    // otherwise produce 1e16-style percentages that are meaningless. Snap
    // those to ∞% so the dashboard says "we went from nothing to something"
    // instead of a misleading huge number.
    const delta = currentScore - prevScore;
    const pct = prevScore !== 0 ? (delta / Math.abs(prevScore)) * 100 : Infinity;
    if (!Number.isFinite(pct) || Math.abs(pct) > 1e6) {
      const sign = delta > 0 ? "+" : delta < 0 ? "-" : "";
      this.scoreDeltaEl.textContent = `${sign}∞% vs prev best`;
      this.scoreDeltaEl.style.color = "var(--green)";
      return;
    }
    const sign = pct >= 0 ? "+" : "";
    this.scoreDeltaEl.textContent = `${sign}${pct.toFixed(3)}% vs prev best`;
    this.scoreDeltaEl.style.color = "var(--green)";
  }

  protected updateHistoryLabel() {
    const total = this.historyEntries.length;
    const atLatest = this.isAtLatest();
    const showLive = shouldShowLiveButton(this.challenge, atLatest);
    if (total <= 1 && !showLive) {
      this.historyNavEl.style.display = "none";
      return;
    }
    this.historyNavEl.style.display = "flex";
    this.historyLiveBtnEl.style.display = showLive ? "inline-block" : "none";
    const suffix = atLatest ? " · LATEST" : "";
    this.historyLabelEl.textContent =
      total > 0 ? `BEST ${this.historyIndex + 1}/${total}${suffix}` : "";
  }

  protected updateEmptyState() {
    if (!this.emptyStateEl) return;
    const showEmpty = this.historyLoaded && this.historyEntries.length === 0;
    this.emptyStateEl.style.display = showEmpty ? "flex" : "none";
  }

  protected navigate(delta: number) {
    const keys = this.instanceKeys;
    if (keys.length === 0) return;
    this.currentIndex = (this.currentIndex + delta + keys.length) % keys.length;
    this.updateInstanceLabel();
    this.showInstance(this.allInstances[keys[this.currentIndex]]);
  }

  protected updateInstanceLabel() {
    const keys = this.instanceKeys;
    if (keys.length <= 1) {
      this.navEl.style.display = "none";
      return;
    }
    this.navEl.style.display = "flex";
    const key = keys[this.currentIndex].replace(/\.txt$/, "");
    this.instanceLabelEl.textContent = `${key}  (${this.currentIndex + 1}/${keys.length})`;
  }

  // Default message routing. Subclasses can override and call super.handleMessage
  // for additional cases, or fully override.
  handleMessage(msg: WSMessage) {
    if (msg.type === "reset") {
      this.handleReset();
      return;
    }
    if (msg.type === "stats_update") {
      this.handleStatsUpdate(msg);
      return;
    }
    if (msg.type === "new_global_best") {
      this.handleNewGlobalBest(msg);
    }
  }

  protected handleReset() {
    this.allInstances = {} as TInstances;
    this.currentIndex = 0;
    this.rawScore = null;
    this.historyEntries = [];
    this.historyIndex = -1;
    this.scoreEl.textContent = "---";
    this.scoreDeltaEl.textContent = "";
    this.navEl.style.display = "none";
    this.historyNavEl.style.display = "none";
    this.instanceLabelEl.textContent = "";
    this.agentNameEl.textContent = "";
    this.agentNameEl.style.color = "";
    this.updateHistoryLabel();
    this.updateEmptyState();
    this.onReset();
  }

  protected handleStatsUpdate(msg: any) {
    if (msg.best_score != null && this.historyEntries.length === 0) {
      this.rawScore = msg.best_score;
      this.scoreEl.textContent = formatScore(msg.best_score);
    }
  }

  protected handleNewGlobalBest(msg: any) {
    if (!msg.solution_data) return;
    this.historyLoaded = true;
    const entry: DisplayHistoryEntry<TInstances> = {
      experiment_id: msg.experiment_id,
      agent_name: msg.agent_name,
      agent_id: msg.agent_id,
      score: msg.score,
      solution_data: msg.solution_data as TInstances,
      created_at: msg.timestamp,
    };
    const existingIdx = this.historyEntries.findIndex(
      (e) => e.experiment_id === entry.experiment_id,
    );
    if (existingIdx >= 0) {
      this.historyEntries[existingIdx] = entry;
      this.historyIndex = existingIdx;
      this.applyHistoryEntry();
    } else {
      const wasAtLatest = this.isAtLatest();
      this.historyEntries.push(entry);
      if (wasAtLatest) {
        this.historyIndex = this.historyEntries.length - 1;
        this.applyHistoryEntry();
      } else {
        this.updateHistoryLabel();
        this.updateEmptyState();
      }
      // Pulse the panel for genuinely new entries (replays of duplicates
      // don't count — those just patch existing history slots).
      this.flashNewBest(entry.agent_id ? getAgentColor(entry.agent_id) : undefined);
    }
  }
}

function resolveApiUrl(): string {
  const params = new URLSearchParams(window.location.search);
  const explicit = params.get("api");
  if (explicit) return explicit;
  const ws = params.get("ws") || "";
  if (ws) {
    return ws
      .replace("ws://", "http://")
      .replace("wss://", "https://")
      .replace("/ws/dashboard", "");
  }
  return `${window.location.protocol}//${window.location.host}`;
}
