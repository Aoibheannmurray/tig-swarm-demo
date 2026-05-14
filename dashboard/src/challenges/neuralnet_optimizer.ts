import { DisplayPanelBase } from "./base";

interface NeuralnetData {
  epochs_used: number;
  max_epochs: number;
  num_hidden_layers: number;
  total_params: number;
  noise_floor: number | null;
  model_loss: number | null;
}

type AllNeuralnetData = Record<string, NeuralnetData>;

export class NeuralnetPanel extends DisplayPanelBase<AllNeuralnetData> {
  protected idPrefix = "nn";

  private epochsBarEl!: HTMLElement;
  private epochsLabelEl!: HTMLElement;
  private layersEl!: HTMLElement;
  private paramsEl!: HTMLElement;
  private archDiagramEl!: HTMLElement;
  private lossBarEl!: HTMLElement;
  private noiseBarEl!: HTMLElement;
  private lossLabelEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner nn-panel">
        <div class="panel-label">NEURAL NET OPTIMIZER</div>
        <div class="knapsack-agent-name" id="nn-agent-name"></div>
        ${this.navsScaffold()}
        <div class="nn-viz-area" id="nn-viz-area">
          <div class="nn-arch-diagram" id="nn-arch-diagram"></div>
          <div class="nn-epochs-section">
            <div class="nn-epochs-header">CONVERGENCE</div>
            <div class="nn-epochs-bar-wrap">
              <div class="nn-epochs-bar" id="nn-epochs-bar"></div>
            </div>
            <div class="nn-epochs-label" id="nn-epochs-label">---</div>
          </div>
          <div class="nn-loss-section" id="nn-loss-section" style="display:none">
            <div class="nn-epochs-header">LOSS vs NOISE FLOOR</div>
            <div class="nn-loss-bars">
              <div class="nn-loss-row">
                <span class="nn-loss-tag">model</span>
                <div class="nn-loss-bar-wrap"><div class="nn-loss-bar nn-loss-bar-model" id="nn-loss-bar"></div></div>
              </div>
              <div class="nn-loss-row">
                <span class="nn-loss-tag">noise</span>
                <div class="nn-loss-bar-wrap"><div class="nn-loss-bar nn-loss-bar-noise" id="nn-noise-bar"></div></div>
              </div>
            </div>
            <div class="nn-loss-label" id="nn-loss-label">---</div>
          </div>
          <div class="solution-empty-state" id="nn-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="knapsack-value-box">
          <div class="solution-sub-label">HIDDEN LAYERS</div>
          <div class="solution-sub-value" id="nn-layers">---</div>
        </div>
        <div class="knapsack-items-box">
          <div class="solution-sub-label">PARAMETERS</div>
          <div class="solution-sub-value" id="nn-params">---</div>
        </div>
        <div class="solution-score">
          <div class="solution-score-label">SCORE</div>
          <div class="solution-score-value" id="nn-score">---</div>
          <div class="solution-score-delta" id="nn-score-delta"></div>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.scoreEl = document.getElementById("nn-score")!;
    this.scoreDeltaEl = document.getElementById("nn-score-delta")!;
    this.instanceLabelEl = document.getElementById("nn-instance-label")!;
    this.navEl = document.getElementById("nn-nav")!;
    this.agentNameEl = document.getElementById("nn-agent-name")!;
    this.historyNavEl = document.getElementById("nn-history-nav")!;
    this.historyLabelEl = document.getElementById("nn-history-label")!;
    this.historyLiveBtnEl = document.getElementById("nn-hist-live")!;
    this.emptyStateEl = document.getElementById("nn-empty-state")!;

    this.epochsBarEl = document.getElementById("nn-epochs-bar")!;
    this.epochsLabelEl = document.getElementById("nn-epochs-label")!;
    this.layersEl = document.getElementById("nn-layers")!;
    this.paramsEl = document.getElementById("nn-params")!;
    this.archDiagramEl = document.getElementById("nn-arch-diagram")!;
    this.lossBarEl = document.getElementById("nn-loss-bar")!;
    this.noiseBarEl = document.getElementById("nn-noise-bar")!;
    this.lossLabelEl = document.getElementById("nn-loss-label")!;
  }

  protected onReset(): void {
    this.epochsBarEl.style.width = "0%";
    this.epochsLabelEl.textContent = "---";
    this.layersEl.textContent = "---";
    this.paramsEl.textContent = "---";
    this.archDiagramEl.innerHTML = "";
    this.lossBarEl.style.width = "0%";
    this.noiseBarEl.style.width = "0%";
    this.lossLabelEl.textContent = "---";
    document.getElementById("nn-loss-section")!.style.display = "none";
  }

  protected showInstance(data: NeuralnetData) {
    if (!data) {
      this.onReset();
      return;
    }

    const pct = data.max_epochs > 0
      ? (data.epochs_used / data.max_epochs) * 100
      : 0;
    this.epochsBarEl.style.width = `${pct}%`;
    this.epochsLabelEl.textContent =
      `${data.epochs_used.toLocaleString()} / ${data.max_epochs.toLocaleString()} epochs (${pct.toFixed(1)}%)`;

    this.layersEl.textContent = String(data.num_hidden_layers);
    this.paramsEl.textContent = data.total_params.toLocaleString();

    this.renderArchDiagram(data);
    this.renderLossComparison(data);
  }

  private renderLossComparison(data: NeuralnetData) {
    const section = document.getElementById("nn-loss-section")!;
    if (data.noise_floor == null || data.model_loss == null) {
      section.style.display = "none";
      return;
    }
    section.style.display = "";
    const nf = data.noise_floor;
    const ml = data.model_loss;
    const maxVal = Math.max(nf, ml, 1e-12);

    this.lossBarEl.style.width = `${(ml / maxVal) * 100}%`;
    this.noiseBarEl.style.width = `${(nf / maxVal) * 100}%`;

    const ratio = nf > 0 ? (ml / nf) * 100 : 0;
    const below = nf > 0 && ml < nf;
    const pctText = below
      ? `${(100 - ratio).toFixed(1)}% below noise floor`
      : ratio > 100
        ? `${(ratio - 100).toFixed(1)}% above noise floor`
        : "at noise floor";
    this.lossLabelEl.textContent =
      `Loss ${ml.toFixed(6)} / Noise ${nf.toFixed(6)} — ${pctText}`;
  }

  private renderArchDiagram(data: NeuralnetData) {
    const nHidden = data.num_hidden_layers;
    const layers = [1, ...Array(nHidden).fill(256), 2];
    const nLayers = layers.length;

    const W = 400;
    const H = 200;
    const pad = 40;
    const layerSpacing = (W - 2 * pad) / (nLayers - 1);

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">`;

    const maxNodes = 6;
    const nodeR = 5;

    const layerPositions: Array<Array<[number, number]>> = [];

    for (let li = 0; li < nLayers; li++) {
      const x = pad + li * layerSpacing;
      const count = layers[li];
      const shown = Math.min(count, maxNodes);
      const showEllipsis = count > maxNodes;
      const positions: Array<[number, number]> = [];

      const totalSlots = showEllipsis ? shown + 1 : shown;
      const spacing = Math.min(20, (H - 2 * pad) / (totalSlots + 1));
      const startY = H / 2 - (spacing * (totalSlots - 1)) / 2;

      for (let ni = 0; ni < shown; ni++) {
        let idx = ni;
        if (showEllipsis && ni >= Math.floor(shown / 2)) {
          idx = ni + 1;
        }
        const y = startY + idx * spacing;
        positions.push([x, y]);
      }

      if (showEllipsis) {
        const ey = startY + Math.floor(shown / 2) * spacing;
        svg += `<text x="${x}" y="${ey + 2}" text-anchor="middle" fill="var(--ink-dim)" font-size="10" font-family="var(--ui)">···</text>`;
      }

      layerPositions.push(positions);
    }

    for (let li = 0; li < nLayers - 1; li++) {
      const from = layerPositions[li];
      const to = layerPositions[li + 1];
      for (const [x1, y1] of from) {
        for (const [x2, y2] of to) {
          svg += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="var(--border-default)" stroke-width="0.5"/>`;
        }
      }
    }

    for (let li = 0; li < nLayers; li++) {
      const isFrozen = li >= nLayers - 2;
      const fill = isFrozen ? "var(--ink-dim)" : "var(--color-accent)";
      const stroke = isFrozen ? "var(--border-strong)" : "var(--color-accent-hov)";
      for (const [x, y] of layerPositions[li]) {
        svg += `<circle cx="${x}" cy="${y}" r="${nodeR}" fill="${fill}" stroke="${stroke}" stroke-width="0.8"/>`;
      }

      const lx = layerPositions[li][0][0];
      const label = li === 0 ? "in" : li === nLayers - 1 ? "out" : `h${li}`;
      svg += `<text x="${lx}" y="${H - 8}" text-anchor="middle" fill="var(--ink-dim)" font-size="8" font-family="var(--ui)">${label}</text>`;
      svg += `<text x="${lx}" y="${16}" text-anchor="middle" fill="var(--ink-faint)" font-size="7" font-family="var(--ui)">${layers[li]}</text>`;
    }

    svg += `</svg>`;
    this.archDiagramEl.innerHTML = svg;
  }
}
