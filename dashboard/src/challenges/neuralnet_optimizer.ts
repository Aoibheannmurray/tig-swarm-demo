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

  private epochsLabelEl!: HTMLElement;
  private layersEl!: HTMLElement;
  private paramsEl!: HTMLElement;
  private archDiagramEl!: HTMLElement;
  private vizStackEl!: HTMLElement;
  private bottomBarEl!: HTMLElement;
  private lossCurveWrapEl!: HTMLElement;
  private lossCurveSvgEl!: HTMLElement;
  private lossHeadlineEl!: HTMLElement;
  private lossSubEl!: HTMLElement;
  private lossRefsEl!: HTMLElement;
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
            <div class="nn-side">
              <div class="nn-loss-curve" id="nn-loss-curve" style="display:none">
                <div class="nn-loss-head">
                  <span class="nn-meter-head">TRAINING LOSS</span>
                  <span class="nn-loss-legend">
                    <span class="nn-lg nn-lg--train">train</span>
                    <span class="nn-lg nn-lg--val">val</span>
                  </span>
                  <span class="nn-loss-headline" id="nn-loss-headline"></span>
                </div>
                <div class="nn-loss-chart">
                  <div class="nn-loss-curve-svg" id="nn-loss-curve-svg"></div>
                  <div class="nn-loss-refs" id="nn-loss-refs"></div>
                </div>
                <div class="nn-loss-sub" id="nn-loss-sub"></div>
              </div>
            </div>
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
    this.lossCurveWrapEl = document.getElementById("nn-loss-curve")!;
    this.lossCurveSvgEl = document.getElementById("nn-loss-curve-svg")!;
    this.lossHeadlineEl = document.getElementById("nn-loss-headline")!;
    this.lossSubEl = document.getElementById("nn-loss-sub")!;
    this.lossRefsEl = document.getElementById("nn-loss-refs")!;

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
    this.lossCurveWrapEl.style.display = "none";
    this.lossCurveSvgEl.innerHTML = "";
    this.lossRefsEl.innerHTML = "";
    this.lossHeadlineEl.textContent = "";
    this.lossSubEl.textContent = "";
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
    this.renderLossCurve(data);
  }

  // P1 — training-loss chart with the noise-limit reference folded in.
  //
  // The challenge is denoising: labels carry noise of variance σ², so the best
  // any model can do is recover the true signal, bottoming out at loss = σ²
  // (the "noise limit" — unbeatable). The score is normalised against a
  // baseline of 4σ² (= `noise_floor` from the payload, the quality-0 point):
  //     quality = (4σ² − test_loss) / 4σ²,  maxing at 0.75 when test_loss = σ².
  // So we draw the train/val curves against the noise limit (σ² = noise_floor/4)
  // and headline how far the model got toward that σ² ceiling. Renders when we
  // have either a loss curve or the reference value; the train line draws in.
  private renderLossCurve(data: NeuralnetData) {
    const curve = data.loss_curve && data.loss_curve.length >= 2 ? data.loss_curve : null;
    const val = data.val_loss_curve && data.val_loss_curve.length >= 2 ? data.val_loss_curve : null;
    const nf = data.noise_floor;        // baseline = 4σ²
    const ml = data.model_loss;          // final test loss
    const hasRefs = nf != null && nf > 0;
    const limit = hasRefs ? nf! / 4 : null;   // σ², the irreducible floor

    if (!curve && !hasRefs) {
      this.lossCurveWrapEl.style.display = "none";
      this.lossCurveSvgEl.innerHTML = "";
      this.lossRefsEl.innerHTML = "";
      this.lossHeadlineEl.textContent = "";
      this.lossSubEl.textContent = "";
      return;
    }
    this.lossCurveWrapEl.style.display = "";

    const W = 400;
    const H = 120;
    const padX = 4;
    const padY = 8;
    // Scale to cover every line we draw — the curves, the noise limit and the
    // final-loss marker — so nothing clips off the top or bottom.
    const ys: number[] = [];
    if (curve) ys.push(...curve);
    if (val) ys.push(...val);
    if (limit != null) ys.push(limit);
    if (ml != null) ys.push(ml);
    let lo = Math.min(...ys);
    let hi = Math.max(...ys);
    const margin = (hi - lo) * 0.06 || 1;
    lo -= margin;
    hi += margin;
    const span = hi - lo || 1;
    const xOf = (i: number, n: number) =>
      padX + (n > 1 ? (i / (n - 1)) * (W - 2 * padX) : 0);
    const yOf = (v: number) => padY + (1 - (v - lo) / span) * (H - 2 * padY);
    const pathOf = (arr: number[]) =>
      arr
        .map((v, i) => `${i ? "L" : "M"}${xOf(i, arr.length).toFixed(1)},${yOf(v).toFixed(1)}`)
        .join(" ");

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
    // Faint band below the noise limit — the region no model can reach.
    if (limit != null) {
      const yL = yOf(limit);
      svg += `<rect class="nn-loss-floorband" x="0" y="${yL.toFixed(1)}" width="${W}" height="${(H - yL).toFixed(1)}"/>`;
    }
    if (limit != null) {
      const yL = yOf(limit).toFixed(1);
      svg += `<line class="nn-loss-limit" x1="0" y1="${yL}" x2="${W}" y2="${yL}"/>`;
    }
    if (val) {
      svg += `<path class="nn-loss-line-val" d="${pathOf(val)}" fill="none" stroke-dasharray="3 2"/>`;
    }
    if (curve) {
      svg += `<path class="nn-loss-line" pathLength="1" d="${pathOf(curve)}" fill="none"/>`;
    }
    svg += `</svg>`;
    this.lossCurveSvgEl.innerHTML = svg;

    // HTML overlays — SVG text/circles would shear under
    // preserveAspectRatio="none". top% maps linearly through the stretched
    // viewBox, so yOf(v)/H positions an element on that loss value.
    let refs = "";
    if (limit != null) {
      refs += `<span class="nn-ref nn-ref--limit" style="top:${(yOf(limit) / H * 100).toFixed(1)}%">noise limit</span>`;
    }
    // Final test-loss marker, sitting on the curve's right edge.
    if (ml != null) {
      refs += `<span class="nn-loss-dot" style="top:${(yOf(ml) / H * 100).toFixed(1)}%"></span>`;
    }
    this.lossRefsEl.innerHTML = refs;

    // Headline: fraction of the recoverable signal the model pulled out of the
    // noise. quality 0.75 (test_loss == σ², the noise limit) = 100% recovered;
    // quality <= 0 (no better than the 4σ² baseline) = nothing recovered.
    if (hasRefs && ml != null) {
      const quality = 1 - ml / nf!;
      if (quality <= 0) {
        this.lossHeadlineEl.textContent = "no signal recovered";
        this.lossHeadlineEl.className = "nn-loss-headline nn-loss-headline--bad";
      } else {
        const pct = Math.min(100, Math.round((quality / 0.75) * 100));
        this.lossHeadlineEl.textContent = `${pct}% of signal recovered`;
        this.lossHeadlineEl.className = "nn-loss-headline";
      }
      this.lossSubEl.textContent =
        `final loss ${ml.toFixed(3)} · noise limit ${limit!.toFixed(3)}`;
    } else {
      this.lossHeadlineEl.textContent = "";
      this.lossSubEl.textContent = "";
    }
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
