import type { WSMessage } from "../types";
import { formatScore } from "./format";
import { getViewedChallenge } from "./viewedChallenge";

let playing = false;

export async function startReplay(
  apiUrl: string,
  handleMessage: (msg: WSMessage) => void,
) {
  if (playing) return;
  playing = true;

  // Fetch history + current state (for num_instances)
  let history: any[];
  let numInstances = 1;
  try {
    const [historyRes, stateRes] = await Promise.all([
      fetch(`${apiUrl}/api/replay`),
      fetch(`${apiUrl}/api/state`),
    ]);
    history = await historyRes.json();
    if (stateRes.ok) {
      const state = await stateRes.json();
      numInstances = state.num_instances || 1;
    }
  } catch {
    playing = false;
    return;
  }

  if (!history.length) {
    playing = false;
    return;
  }

  // Show replay overlay
  const overlay = document.createElement("div");
  overlay.className = "replay-overlay";
  overlay.innerHTML = `
    <div class="replay-banner">EVOLUTION REPLAY</div>
    <div class="replay-progress">
      <span class="replay-step" id="replay-step">0 / ${history.length}</span>
      <span class="replay-score" id="replay-score"></span>
    </div>
  `;
  document.body.appendChild(overlay);

  const stepEl = document.getElementById("replay-step")!;
  const scoreEl = document.getElementById("replay-score")!;
  const firstScore = history[0].score;
  // The history is a stream of global bests and every challenge maximises its
  // (baseline-relative) quality score, so improvement is `current - prev`.
  // The earlier code computed `prev - current`, which read negative for
  // genuine progress on these maximising scores.
  const pct = (cur: number, base: number): number | null =>
    base !== 0 ? ((cur - base) / Math.abs(base)) * 100 : null;

  // Play through each best
  for (let i = 0; i < history.length; i++) {
    const entry = history[i];
    stepEl.textContent = `${i + 1} / ${history.length}`;
    scoreEl.textContent = `Score: ${formatScore(entry.score)}`;

    if (entry.solution_data) {
      const prevScore = i > 0 ? history[i - 1].score : null;
      const incremental = prevScore != null ? pct(entry.score, prevScore) : null;
      handleMessage({
        type: "new_global_best",
        challenge: getViewedChallenge(),
        experiment_id: entry.experiment_id,
        agent_name: entry.agent_name,
        agent_id: "",
        score: entry.score,
        improvement_pct: pct(entry.score, firstScore) ?? 0,
        incremental_improvement_pct: incremental,
        num_instances: numInstances,
        solution_data: entry.solution_data,
        timestamp: entry.created_at,
      });
    }

    // Wait between steps (faster for early ones, slower for later)
    const delay = i < 3 ? 2000 : 1500;
    await new Promise((r) => setTimeout(r, delay));
  }

  // Show final result
  const lastScore = history[history.length - 1].score;
  const totalImprovement = pct(lastScore, firstScore) ?? 0;

  overlay.innerHTML = `
    <div class="replay-banner">EVOLUTION COMPLETE</div>
    <div class="replay-final">
      <div class="replay-final-score">${formatScore(lastScore)}</div>
      <div class="replay-final-improvement">${totalImprovement.toFixed(1)}% improvement</div>
      <div class="replay-final-steps">${history.length} breakthroughs</div>
    </div>
  `;

  // Dismiss after 8s or on click — whichever first. Clearing the timer on
  // click stops the overlay closure (and its references to `resolve` /
  // `overlay`) from being held for the rest of the 8s window.
  await new Promise<void>((resolve) => {
    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(timer);
      overlay.removeEventListener("click", dismiss);
      resolve();
    };
    overlay.addEventListener("click", dismiss);
    const timer = setTimeout(dismiss, 8000);
  });

  overlay.remove();
  playing = false;
}
