import { DisplayPanelBase } from "./base";

interface NeuralnetData {
  epochs_used: number;
  max_epochs: number;
  num_hidden_layers: number;
  total_params: number;
}

type AllNeuralnetData = Record<string, NeuralnetData>;

export class NeuralnetPanel extends DisplayPanelBase<AllNeuralnetData> {
  protected idPrefix = "nn";

  private epochsLabelEl!: HTMLElement;
  private layersEl!: HTMLElement;
  private paramsEl!: HTMLElement;
  private archDiagramEl!: HTMLElement;
  private vizStackEl!: HTMLElement;
  private bottomBarEl!: HTMLElement;
  // Last instance rendered — kept so the architecture diagram can re-fit to the
  // container on resize (its viewBox is sized from the container's pixels).
  private currentData: NeuralnetData | null = null;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner nn-panel">
        <div class="panel-label">NEURAL NET OPTIMIZER</div>
        <div class="solution-agent-name" id="nn-agent-name"></div>
        ${this.navsScaffold()}
        <div class="nn-svg-wrap" id="nn-svg-wrap">
          <div class="nn-viz-stack" id="nn-viz-stack">
            <div class="nn-arch-diagram" id="nn-arch-diagram"></div>
          </div>
          <div class="solution-empty-state" id="nn-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        <div class="stat-bar nn-stat-bar" id="nn-stat-bar">
          <div class="stat-cell">
            <div class="stat-label">HIDDEN LAYERS</div>
            <div class="stat-value" id="nn-layers">---</div>
          </div>
          <div class="stat-cell">
            <div class="stat-label">PARAMETERS</div>
            <div class="stat-value" id="nn-params">---</div>
          </div>
          <div class="stat-cell">
            <div class="stat-label">EPOCHS</div>
            <div class="stat-value" id="nn-epochs-label">---</div>
          </div>
          <div class="stat-cell stat-cell--score">
            <div class="stat-label">SCORE</div>
            <div class="stat-value" id="nn-score" data-track-score>---</div>
            <div class="stat-delta" id="nn-score-delta"></div>
          </div>
        </div>
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.epochsLabelEl = document.getElementById("nn-epochs-label")!;
    this.layersEl = document.getElementById("nn-layers")!;
    this.paramsEl = document.getElementById("nn-params")!;
    this.archDiagramEl = document.getElementById("nn-arch-diagram")!;
    this.vizStackEl = document.getElementById("nn-viz-stack")!;
    this.bottomBarEl = document.getElementById("nn-stat-bar")!;

    // Re-fit the architecture diagram when its column resizes — its viewBox is
    // sized from the container's pixels, so it must redraw to fill a new size.
    this.observeResize(this.archDiagramEl, () => {
      if (this.currentData) this.renderArchDiagram(this.currentData);
    });
  }

  protected onReset(): void {
    this.currentData = null;
    this.epochsLabelEl.textContent = "---";
    this.layersEl.textContent = "---";
    this.paramsEl.textContent = "---";
    this.archDiagramEl.innerHTML = "";
  }

  // Empty state hides everything except the centred "challenge not started yet"
  // copy — viz stack and bottom-bar stats collapse together so the panel reads
  // as blank, not as a partially-filled chrome.
  protected updateEmptyState() {
    super.updateEmptyState();
    const showEmpty = this.historyLoaded && this.historyEntries.length === 0;
    const display = showEmpty ? "none" : "";
    if (this.vizStackEl) this.vizStackEl.style.display = display;
    if (this.bottomBarEl) this.bottomBarEl.style.display = display;
  }

  protected showInstance(data: NeuralnetData) {
    if (!data) {
      this.onReset();
      return;
    }
    this.currentData = data;

    this.epochsLabelEl.textContent =
      `${data.epochs_used.toLocaleString()} / ${data.max_epochs.toLocaleString()}`;

    this.layersEl.textContent = String(data.num_hidden_layers);
    this.paramsEl.textContent = data.total_params.toLocaleString();

    this.renderArchDiagram(data);
  }

  private renderArchDiagram(data: NeuralnetData) {
    const nHidden = data.num_hidden_layers;
    const layers = [1, ...Array(nHidden).fill(256), 2];
    const nLayers = layers.length;

    // Size the viewBox to the container's actual aspect so the drawing fills it
    // edge-to-edge — a fixed-aspect viewBox letterboxes (the side whitespace).
    // Matching the aspect means no gaps and (unlike preserveAspectRatio="none")
    // no distortion: nodes stay circular. Re-rendered on resize (see attachRefs).
    const W = Math.round(this.archDiagramEl.clientWidth) || 400;
    const H = Math.round(this.archDiagramEl.clientHeight) || 240;
    const pad = 26;
    const layerSpacing = (W - 2 * pad) / (nLayers - 1);

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;

    const maxNodes = 6;
    const nodeR = 6.5;

    const layerPositions: Array<Array<[number, number]>> = [];

    for (let li = 0; li < nLayers; li++) {
      const x = pad + li * layerSpacing;
      const count = layers[li];
      const shown = Math.min(count, maxNodes);
      const showEllipsis = count > maxNodes;
      const positions: Array<[number, number]> = [];

      const totalSlots = showEllipsis ? shown + 1 : shown;
      // Spread to span the full height (cap keeps sparse layers from drifting
      // too far apart); startY then re-centres the column.
      const spacing = Math.min(54, (H - 2 * pad) / Math.max(totalSlots - 1, 1));
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

    // P2 — `--t` is the layer's position along the network (0→1) so nodes and
    // edges "activate" left→right via a staggered CSS animation-delay, giving
    // each instance a distinct build-up that scales with its depth.
    const layerT = (li: number) => (nLayers > 1 ? li / (nLayers - 1) : 0).toFixed(3);

    for (let li = 0; li < nLayers - 1; li++) {
      const from = layerPositions[li];
      const to = layerPositions[li + 1];
      const t = layerT(li);
      for (const [x1, y1] of from) {
        for (const [x2, y2] of to) {
          svg += `<line class="nn-edge" style="--t:${t}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="var(--border-default)" stroke-width="0.5"/>`;
        }
      }
    }

    for (let li = 0; li < nLayers; li++) {
      const isFrozen = li >= nLayers - 2;
      const fill = isFrozen ? "var(--ink-dim)" : "var(--color-accent)";
      const stroke = isFrozen ? "var(--border-strong)" : "var(--color-accent-hov)";
      const t = layerT(li);
      const cls = isFrozen ? "nn-node" : "nn-node nn-node--trainable";
      for (const [x, y] of layerPositions[li]) {
        svg += `<circle class="${cls}" style="--t:${t}" cx="${x}" cy="${y}" r="${nodeR}" fill="${fill}" stroke="${stroke}" stroke-width="0.8"/>`;
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
