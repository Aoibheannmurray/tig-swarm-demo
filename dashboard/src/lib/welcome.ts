const STORAGE_KEY = "swarm-welcomed";

let overlayEl: HTMLElement | null = null;
let visible = false;
let dissolving = false;

const SYMBOLS = [
  "Σ", "∫", "π", "∞", "∇", "∂", "λ", "φ", "θ", "α", "β", "γ",
  "Δ", "Ω", "ε", "η", "μ", "ψ", "χ", "ρ", "σ", "τ", "ω", "Φ",
  "≈", "≠", "∈", "∀", "∃", "→", "⊕", "√", "∝", "∴", "Ψ", "Θ",
];

// Brand palette (matches the welcome image)
const PAL_TERRACOTTA = "#B8541F";
const PAL_GOLD       = "#C68F3E";
const PAL_OLIVE      = "#6B7F4E";
const PAL_SLATE      = "#4E6B85";
const PAL_MAUVE      = "#7A4F6E";
const PAL_TEAL       = "#4A8C8A";
const PAL_RUST       = "#A66E45";
const PAL_PURPLE     = "#8B6B8C";

const REPO_URL = "https://github.com/Aoibheannmurray/tig-swarm-demo.git";

const STEPS: { cmd: string }[] = [
  { cmd: `git clone ${REPO_URL} && cd tig-swarm-demo && python scripts/init_fleet.py` },
];

export function initWelcome() {
  overlayEl = document.createElement("div");
  overlayEl.className = "welcome-overlay";
  const stepsHtml = STEPS.map((s, i) => `
        <div class="welcome-prompt">
          <code>${escapeHtml(s.cmd)}</code>
          <button type="button" class="welcome-copy-btn" data-cmd-idx="${i}">Copy</button>
        </div>
  `).join("");
  overlayEl.innerHTML = `
    <div class="welcome-card">
      <div class="welcome-art">
        <img src="/prometheus.png" alt="" draggable="false" />
      </div>
      <div class="welcome-title">Welcome to Prometheus</div>
      <p class="welcome-subtitle">
        A live swarm of AI agents discovering better algorithms together. Ask the swarm host for the <code>server_url</code>, <code>username</code>, and <code>swarm_password</code>, then run:
      </p>
      <div class="welcome-steps">
        ${stepsHtml}
      </div>
      <div class="welcome-hint">Click outside the card to close &middot; press J to reopen</div>
    </div>
  `;
  overlayEl.style.display = "none";
  document.body.appendChild(overlayEl);

  // Backdrop click closes; clicks inside the card don't bubble up so
  // copy buttons / text selection don't dismiss the overlay.
  overlayEl.addEventListener("click", (e) => {
    if (dissolving) return;
    if (e.target === overlayEl) hideWelcome();
  });
  const card = overlayEl.querySelector(".welcome-card");
  card?.addEventListener("click", (e) => e.stopPropagation());

  overlayEl.querySelectorAll<HTMLButtonElement>(".welcome-copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = Number(btn.dataset.cmdIdx);
      const step = STEPS[idx];
      if (!step) return;
      try {
        await navigator.clipboard.writeText(step.cmd);
      } catch {
        // Older browsers / insecure contexts — fall back to a hidden textarea.
        const ta = document.createElement("textarea");
        ta.value = step.cmd;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } finally { ta.remove(); }
      }
      const original = btn.textContent;
      btn.textContent = "Copied";
      btn.classList.add("welcome-copy-btn--copied");
      setTimeout(() => {
        btn.textContent = original;
        btn.classList.remove("welcome-copy-btn--copied");
      }, 1400);
    });
  });

  if (!localStorage.getItem(STORAGE_KEY)) {
    showWelcome();
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function showWelcome() {
  visible = true;
  if (!overlayEl) return;
  overlayEl.style.display = "flex";
  overlayEl.classList.remove("welcome-overlay--dissolving");
  // Clear any inline opacity/filter left behind so the next dissolve can
  // fade from a clean baseline.
  const art = overlayEl.querySelector(".welcome-art") as HTMLElement | null;
  if (art) {
    art.style.removeProperty("opacity");
    art.style.removeProperty("filter");
  }
  const card = overlayEl.querySelector(".welcome-card") as HTMLElement | null;
  if (card) card.style.removeProperty("opacity");
}

function hideWelcome() {
  if (!overlayEl || dissolving) return;
  dissolving = true;
  const art = overlayEl.querySelector(".welcome-art") as HTMLElement | null;
  if (art) {
    runDissolve(art, () => {
      visible = false;
      if (overlayEl) overlayEl.style.display = "none";
      localStorage.setItem(STORAGE_KEY, "1");
      dissolving = false;
    });
  } else {
    visible = false;
    overlayEl.style.display = "none";
    localStorage.setItem(STORAGE_KEY, "1");
    dissolving = false;
  }
}

export function toggleWelcome() {
  if (visible) {
    hideWelcome();
  } else {
    showWelcome();
  }
}

/* ════════════════════════════════════════════════════════════
   DISSOLVE — image breaks apart into drifting math symbols
   ════════════════════════════════════════════════════════════ */

interface Particle {
  x: number; y: number;
  vx: number; vy: number;
  rot: number; vrot: number;
  symbol: string;
  size: number;
  colorAt: (a: number) => string;
  delay: number;
  ttl: number;
}

function runDissolve(artEl: HTMLElement, onDone: () => void) {
  const rect = artEl.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  const canvas = document.createElement("canvas");
  canvas.className = "welcome-dissolve-canvas";
  canvas.width = window.innerWidth * dpr;
  canvas.height = window.innerHeight * dpr;
  canvas.style.width = window.innerWidth + "px";
  canvas.style.height = window.innerHeight + "px";
  document.body.appendChild(canvas);
  const ctx = canvas.getContext("2d")!;
  ctx.scale(dpr, dpr);

  const particles: Particle[] = [];
  const N = 320;
  for (let i = 0; i < N; i++) {
    // Bias particle origin toward the upper portion of the image (flame-heavy)
    const yBias = Math.pow(Math.random(), 1.4);
    const py = rect.top + yBias * rect.height;
    const px = rect.left + (0.05 + Math.random() * 0.90) * rect.width;
    const yFrac = (py - rect.top) / rect.height;
    const xFrac = (px - rect.left) / rect.width;

    const colorAt = pickColor(yFrac, xFrac);

    const angle = -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.15;
    const speed = 1.2 + Math.random() * 3.0;

    particles.push({
      x: px,
      y: py,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      rot: Math.random() * Math.PI * 2,
      vrot: (Math.random() - 0.5) * 0.05,
      symbol: SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)],
      size: 11 + Math.random() * 22,
      colorAt,
      delay: Math.random() * 380,
      ttl: 1700 + Math.random() * 1100,
    });
  }

  if (overlayEl) overlayEl.classList.add("welcome-overlay--dissolving");

  const start = performance.now();
  const HARD_STOP = 3400;

  function frame(t: number) {
    const elapsed = t - start;
    ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);

    let anyAlive = false;
    for (const p of particles) {
      const local = elapsed - p.delay;
      if (local < 0) {
        anyAlive = true;
        continue;
      }
      const lifeT = local / p.ttl;
      if (lifeT >= 1) continue;
      anyAlive = true;

      const fadeIn = Math.min(1, local / 180);
      const fadeOut = Math.max(0, 1 - Math.max(0, local - p.ttl * 0.45) / (p.ttl * 0.55));
      const alpha = Math.min(fadeIn, fadeOut);

      p.x += p.vx;
      p.y += p.vy;
      p.vy += 0.006;
      p.vx *= 0.9985;
      p.vy *= 0.9985;
      p.rot += p.vrot;

      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.font = `italic ${p.size}px 'EB Garamond', Georgia, serif`;
      ctx.fillStyle = p.colorAt(alpha * 0.9);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(p.symbol, 0, 0);
      ctx.restore();
    }

    if (elapsed > HARD_STOP || !anyAlive) {
      canvas.remove();
      onDone();
      return;
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

/* Color zones tuned to the welcome image:
     y 0 - 0.78  flame ribbons (spans most of the height)
     y 0.78 - 1  hand silhouette (slate blue)
     edges       cream background
*/
function pickColor(yFrac: number, xFrac: number): (a: number) => string {
  const xDist = Math.abs(xFrac - 0.5) * 2; // 0 centre → 1 edge

  // Hand silhouette at the bottom
  if (yFrac > 0.78 && xDist < 0.82) {
    return hexAt(pick([PAL_SLATE, "#3A5874", "#5A7A95", "#456378", "#345066"]));
  }
  // Lower-flame transition (warm tones at the bottom of the flame)
  if (yFrac > 0.62 && yFrac <= 0.78 && xDist < 0.66) {
    return hexAt(pick([PAL_GOLD, PAL_RUST, PAL_TERRACOTTA, "#FFD974", PAL_OLIVE]));
  }
  // Main flame body — full neon palette
  if (yFrac > 0.05 && yFrac <= 0.62 && xDist < 0.6) {
    return hexAt(pick([
      PAL_GOLD, PAL_TERRACOTTA, PAL_RUST, PAL_TEAL, PAL_SLATE,
      PAL_MAUVE, PAL_PURPLE, PAL_OLIVE, "#FFD974",
    ]));
  }
  // Cream background (corners / outside the figure)
  return hexAt(pick(["#EFE9DD", "#E8DFD0", "#D8CFB8", "#C9BBA6"]));
}

function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

function hexAt(hex: string): (a: number) => string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return (a) => `rgba(${r}, ${g}, ${b}, ${a})`;
}
