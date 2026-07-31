import "../../style.css";
import { SwarmWebSocket } from "../../lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { renderPageHeader } from "../../lib/pageChrome";
import { ChartPanel } from "../../panels/chart";
import { ChallengeSelectorPanel } from "../../panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "../../lib/swarmConfig";
import { getViewedChallenge, onViewedChallengeChange } from "../../lib/viewedChallenge";
import { isMessageForChallenge } from "../../lib/messageScope";
import type { WSMessage } from "../../types";

const { wsUrl, apiUrl } = getDashboardUrls();

// ── Initialize single panel ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const panelEl = document.getElementById("panel-chart")!;
panelEl.innerHTML = `
  <div class="page-flex">
    ${renderPageHeader("benchmark", "Benchmark Performance Graph")}
    <div class="page-body" id="panel-chart-body"></div>
  </div>
`;
const chartPanel = new ChartPanel();
chartPanel.init(document.getElementById("panel-chart-body")!);

function handleMessage(msg: WSMessage) {
  if (!isMessageForChallenge(msg, getViewedChallenge())) return;
  handleSwarmConfigEvent(apiUrl, msg);
  challengeSelector.handleMessage(msg);
  chartPanel.handleMessage(msg);
}

onViewedChallengeChange(() => {
  chartPanel.handleMessage({ type: "reset", timestamp: new Date().toISOString() });
  void loadInitialState(apiUrl);
});

// ── Hydrate from /api/state + /api/replay ──
async function loadInitialState(apiUrl: string) {
  try {
    const ch = getViewedChallenge();
    const q = `?challenge=${encodeURIComponent(ch)}`;
    const [stateRes, replayRes] = await Promise.all([
      fetch(`${apiUrl}/api/state${q}`),
      fetch(`${apiUrl}/api/replay${q}`),
    ]);
    if (!stateRes.ok) return;
    const state = await stateRes.json();
    const replay: Array<{
      experiment_id: string;
      agent_name: string;
      agent_id?: string;
      score: number;
      created_at: string;
    }> = replayRes.ok ? await replayRes.json() : [];

    // Drop stale responses if the user switched challenges while the
    // fetches were in flight — otherwise the previous challenge's history
    // seeds the freshly-reset chart.
    if (ch !== getViewedChallenge()) return;

    chartPanel.seedHistory(replay);

    if (state.leaderboard?.length) {
      handleMessage({
        type: "leaderboard_update",
        challenge: ch,
        entries: state.leaderboard,
        timestamp: new Date().toISOString(),
      });
    }
    // The mainnet bar for this challenge — drawn as a threshold on the
    // chart and a ranked row in the leaderboard. Sent even when null so a
    // challenge switch clears the previous challenge's bar.
    handleMessage({
      type: "mainnet_baseline",
      challenge: ch,
      baseline: state.mainnet_baseline ?? null,
      timestamp: new Date().toISOString(),
    });

    console.log(`[Benchmark] Loaded ${replay.length} best-history points`);
  } catch (e) {
    console.warn("[Benchmark] Failed to load initial state:", e);
  }
}

installKeyboardNav("benchmark");

// ── Connect ──
console.log(`[Benchmark] Connecting to ${wsUrl}, API: ${apiUrl}`);
void loadSwarmConfig(apiUrl).then(() => loadInitialState(apiUrl));
const ws = new SwarmWebSocket(wsUrl);
ws.onMessage(handleMessage);
ws.connect();
