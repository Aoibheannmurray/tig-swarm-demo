import { DisplayPanelBase } from "./base";

interface VectorSearchData {
  num_queries: number;
  vector_dims: number;
  database_size: number;
  avg_distance: number | null;
}

type AllVectorSearchData = Record<string, VectorSearchData>;

export class VectorSearchPanel extends DisplayPanelBase<AllVectorSearchData> {
  protected idPrefix = "vs";

  private queriesEl!: HTMLElement;
  private distanceEl!: HTMLElement;
  private dbEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner vs-panel">
        <div class="panel-label">VECTOR SEARCH</div>
        <div class="knapsack-agent-name" id="vs-agent-name"></div>
        <div class="solution-history-nav" id="vs-history-nav" style="display:none">
          <button class="solution-nav-btn" id="vs-hist-prev" title="Previous global best">&lsaquo;</button>
          <span class="solution-history-label" id="vs-history-label"></span>
          <button class="solution-nav-btn" id="vs-hist-next" title="Next global best">&rsaquo;</button>
          <button class="solution-history-live" id="vs-hist-live" title="Jump to latest" style="display:none">LIVE &rarr;</button>
        </div>
        <div class="solution-nav" id="vs-nav" style="display:none">
          <button class="solution-nav-btn" id="vs-prev">&lsaquo;</button>
          <span class="solution-instance-label" id="vs-instance-label"></span>
          <button class="solution-nav-btn" id="vs-next">&rsaquo;</button>
        </div>
        <div class="vs-empty-wrap">
          <div class="solution-empty-state" id="vs-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">QUERIES</div>
          <div class="solution-sub-value" id="vs-queries">---</div>
        </div>
        <div class="knapsack-items-box">
          <div class="solution-sub-label">AVG DISTANCE</div>
          <div class="solution-sub-value" id="vs-distance">---</div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">DATABASE</div>
          <div class="solution-sub-value" id="vs-db">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="vs-score">---</div>
          <div class="solution-score-delta" id="vs-score-delta"></div>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("vs-score")!;
    this.scoreDeltaEl = document.getElementById("vs-score-delta")!;
    this.instanceLabelEl = document.getElementById("vs-instance-label")!;
    this.navEl = document.getElementById("vs-nav")!;
    this.agentNameEl = document.getElementById("vs-agent-name")!;
    this.historyNavEl = document.getElementById("vs-history-nav")!;
    this.historyLabelEl = document.getElementById("vs-history-label")!;
    this.historyLiveBtnEl = document.getElementById("vs-hist-live")!;
    this.emptyStateEl = document.getElementById("vs-empty-state")!;

    this.queriesEl = document.getElementById("vs-queries")!;
    this.distanceEl = document.getElementById("vs-distance")!;
    this.dbEl = document.getElementById("vs-db")!;
  }

  protected onReset(): void {
    this.queriesEl.textContent = "---";
    this.distanceEl.textContent = "---";
    this.dbEl.textContent = "---";
  }

  protected showInstance(data: VectorSearchData) {
    if (!data) {
      this.onReset();
      return;
    }
    this.queriesEl.textContent = data.num_queries.toLocaleString();
    this.dbEl.textContent =
      `${data.database_size.toLocaleString()} × ${data.vector_dims}D`;
    this.distanceEl.textContent =
      data.avg_distance != null ? data.avg_distance.toFixed(4) : "---";
  }
}
