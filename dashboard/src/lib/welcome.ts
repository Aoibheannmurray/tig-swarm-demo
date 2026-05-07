const STORAGE_KEY = "swarm-welcomed";

let overlayEl: HTMLElement | null = null;
let visible = false;

export function initWelcome() {
  overlayEl = document.createElement("div");
  overlayEl.className = "welcome-overlay";
  overlayEl.innerHTML = `
    <div class="welcome-card">
      <div class="welcome-title">Welcome to Prometheus</div>
      <p class="welcome-subtitle">
        Help a swarm of AI agents collaboratively discover better algorithms in real time.
      </p>
      <div class="welcome-hint">Click anywhere to close &middot; press J to reopen</div>
    </div>
  `;
  overlayEl.style.display = "none";
  document.body.appendChild(overlayEl);

  overlayEl.addEventListener("click", () => {
    hideWelcome();
  });

  if (!localStorage.getItem(STORAGE_KEY)) {
    showWelcome();
  }
}

function showWelcome() {
  visible = true;
  if (overlayEl) overlayEl.style.display = "flex";
}

function hideWelcome() {
  visible = false;
  if (overlayEl) overlayEl.style.display = "none";
  localStorage.setItem(STORAGE_KEY, "1");
}

export function toggleWelcome() {
  if (visible) {
    hideWelcome();
  } else {
    showWelcome();
  }
}
