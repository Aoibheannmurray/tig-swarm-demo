import { DisplayPanelBase } from "./base";

interface NeuralnetData {
  epochs_used: number;
  max_epochs: number;
  num_hidden_layers: number;
  total_params: number;
  noise_floor: number | null;
  model_loss: number | null;
  // Optional downsampled per-epoch loss history (P1). Absent on older
  // benchmark builds — the panel falls back to the architecture/meter
  // animations when these aren't present.
  loss_curve?: number[];
  val_loss_curve?: number[];
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
  private vizStackEl!: HTMLElement;
  private bottomBarEl!: HTMLElement;
  private lossSectionEl!: HTMLElement;
  private epochsFlagsEl!: HTMLElement;
  private lossCurveWrapEl!: HTMLElement;
  private lossCurveSvgEl!: HTMLElement;

  protected scaffoldHtml(): string {
    return `
      <div class="panel-inner nn-panel">
        <div class="panel-label">NEURAL NET OPTIMIZER</div>
        <div class="solution-agent-name" id="nn-agent-name"></div>
        ${this.navsScaffold()}
        <div class="nn-svg-wrap" id="nn-svg-wrap">
          <div class="nn-viz-stack" id="nn-viz-stack">
            <div class="nn-arch-diagram" id="nn-arch-diagram"></div>
            <div class="nn-loss-curve" id="nn-loss-curve" style="display:none">
              <div class="nn-meter-head">TRAINING LOSS</div>
              <div class="nn-loss-curve-svg" id="nn-loss-curve-svg"></div>
            </div>
            <div class="nn-meters">
              <div class="nn-meter">
                <div class="nn-meter-head">CONVERGENCE</div>
                <div class="nn-meter-bar-wrap"><div class="nn-meter-bar" id="nn-epochs-bar"></div><div class="nn-flags" id="nn-epochs-flags"></div></div>
                <div class="nn-meter-label" id="nn-epochs-label">---</div>
              </div>
              <div class="nn-meter" id="nn-loss-section" style="display:none">
                <div class="nn-meter-head">LOSS vs NOISE FLOOR</div>
                <div class="nn-loss-rows">
                  <div class="nn-loss-row">
                    <span class="nn-loss-tag">model</span>
                    <div class="nn-loss-bar-wrap"><div class="nn-loss-bar nn-loss-bar-model" id="nn-loss-bar"></div></div>
                  </div>
                  <div class="nn-loss-row">
                    <span class="nn-loss-tag">noise</span>
                    <div class="nn-loss-bar-wrap"><div class="nn-loss-bar nn-loss-bar-noise" id="nn-noise-bar"></div></div>
                  </div>
                </div>
                <div class="nn-meter-label" id="nn-loss-label">---</div>
              </div>
            </div>
          </div>
          <div class="solution-empty-state" id="nn-empty-state">
            <div class="solution-empty-state-title">Challenge not started yet</div>
            <div class="solution-empty-state-hint">No iterations have been published for this challenge.</div>
          </div>
        </div>
        ${this.statBarScaffold([
          { label: "HIDDEN LAYERS", id: "nn-layers" },
          { label: "PARAMETERS", id: "nn-params" },
        ])}
      </div>
    `;
  }

  protected attachRefs(_root: HTMLElement): void {
    this.epochsBarEl = document.getElementById("nn-epochs-bar")!;
    this.epochsLabelEl = document.getElementById("nn-epochs-label")!;
    this.layersEl = document.getElementById("nn-layers")!;
    this.paramsEl = document.getElementById("nn-params")!;
    this.archDiagramEl = document.getElementById("nn-arch-diagram")!;
    this.lossBarEl = document.getElementById("nn-loss-bar")!;
    this.noiseBarEl = document.getElementById("nn-noise-bar")!;
    this.lossLabelEl = document.getElementById("nn-loss-label")!;
    this.vizStackEl = document.getElementById("nn-viz-stack")!;
    this.bottomBarEl = document.getElementById("nn-stat-bar")!;
    this.lossSectionEl = document.getElementById("nn-loss-section")!;
    this.epochsFlagsEl = document.getElementById("nn-epochs-flags")!;
    this.lossCurveWrapEl = document.getElementById("nn-loss-curve")!;
    this.lossCurveSvgEl = document.getElementById("nn-loss-curve-svg")!;
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
    this.lossSectionEl.style.display = "none";
    this.epochsFlagsEl.innerHTML = "";
    this.lossCurveWrapEl.style.display = "none";
    this.lossCurveSvgEl.innerHTML = "";
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

    const pct = data.max_epochs > 0
      ? (data.epochs_used / data.max_epochs) * 100
      : 0;
    this.epochsBarEl.style.width = `${pct}%`;
    this.epochsLabelEl.textContent =
      `${data.epochs_used.toLocaleString()} / ${data.max_epochs.toLocaleString()} epochs (${pct.toFixed(1)}%)`;

    this.layersEl.textContent = String(data.num_hidden_layers);
    this.paramsEl.textContent = data.total_params.toLocaleString();

    this.renderEpochMilestones(data);
    this.renderArchDiagram(data);
    this.renderLossCurve(data);
    this.renderLossComparison(data);
  }

  // P3 — milestone flags overlaid on the convergence bar: 25/50/75/100% of
  // max_epochs, lit once that many epochs were actually run, popping in
  // left→right in time with the bar fill.
  private renderEpochMilestones(data: NeuralnetData) {
    const maxE = data.max_epochs || 1;
    const used = data.epochs_used;
    const milestones = [0.25, 0.5, 0.75, 1.0];
    this.epochsFlagsEl.innerHTML = milestones
      .map((m) => {
        const reached = maxE * m <= used + 1e-9;
        return `<span class="nn-flag${reached ? " nn-flag--lit" : ""}" style="left:${(m * 100).toFixed(1)}%;--t:${m.toFixed(2)}"></span>`;
      })
      .join("");
  }

  // P1 — animated training-loss curve. Only renders when the benchmark payload
  // carries a (downsampled) loss history; otherwise stays hidden and the panel
  // leans on the architecture/meter animations. The training line draws itself
  // in via stroke-dashoffset; the optional validation line fades in after.
  private renderLossCurve(data: NeuralnetData) {
    const curve = data.loss_curve;
    if (!curve || curve.length < 2) {
      this.lossCurveWrapEl.style.display = "none";
      this.lossCurveSvgEl.innerHTML = "";
      return;
    }
    this.lossCurveWrapEl.style.display = "";

    const val = data.val_loss_curve && data.val_loss_curve.length >= 2
      ? data.val_loss_curve
      : null;
    const W = 400;
    const H = 90;
    const pad = 6;
    let lo = Infinity;
    let hi = -Infinity;
    for (const v of val ? curve.concat(val) : curve) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    const span = hi - lo || 1;
    const xOf = (i: number, n: number) =>
      pad + (n > 1 ? (i / (n - 1)) * (W - 2 * pad) : 0);
    const yOf = (v: number) => pad + (1 - (v - lo) / span) * (H - 2 * pad);
    const pathOf = (arr: number[]) =>
      arr
        .map((v, i) => `${i ? "L" : "M"}${xOf(i, arr.length).toFixed(1)},${yOf(v).toFixed(1)}`)
        .join(" ");

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
    if (val) {
      svg += `<path class="nn-loss-line-val" d="${pathOf(val)}" fill="none" stroke="var(--ink-dim)" stroke-width="1" stroke-dasharray="3 2"/>`;
    }
    svg += `<path class="nn-loss-line" pathLength="1" d="${pathOf(curve)}" fill="none" stroke="var(--color-accent)" stroke-width="1.5"/>`;
    svg += `</svg>`;
    this.lossCurveSvgEl.innerHTML = svg;
  }

  private renderLossComparison(data: NeuralnetData) {
    if (data.noise_floor == null || data.model_loss == null) {
      this.lossSectionEl.style.display = "none";
      return;
    }
    this.lossSectionEl.style.display = "";
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

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">`;

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
