import "../../style.css";
import { SwarmWebSocket } from "../../lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { ChartPanel } from "../../panels/chart";
import { ChallengeSelectorPanel } from "../../panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "../../lib/swarmConfig";
import { getViewedChallenge, onViewedChallengeChange } from "../../lib/viewedChallenge";
import type { WSMessage } from "../../types";

const { wsUrl, apiUrl } = getDashboardUrls();

// ── Initialize single panel ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const panelEl = document.getElementById("panel-chart")!;
panelEl.innerHTML = `
  <div class="page-flex">
    <div class="ideas-header">
      <div class="ideas-title">
        <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />
        <span class="ideas-title-text">Benchmark Performance Graph</span>
      </div>
      <div class="ideas-nav">
        <a href="/" class="ideas-nav-link">Dashboard</a>
        <a href="/ideas.html" class="ideas-nav-link">Ideas</a>
        <a href="/diversity.html" class="ideas-nav-link">Diversity</a>
        <span class="ideas-nav-active">Benchmark</span>
        <a href="/trajectories.html" class="ideas-nav-link">Trajectories</a>
      </div>
    </div>
    <div class="page-body" id="panel-chart-body"></div>
  </div>
`;
const chartPanel = new ChartPanel();
chartPanel.init(document.getElementById("panel-chart-body")!);

const CHALLENGE_SCOPED: Record<string, true> = {
  experiment_published: true,
  hypothesis_proposed: true,
  new_global_best: true,
  leaderboard_update: true,
  chat_message: true,
  trajectory_reset: true,
  hypothesis_status_changed: true,
};

function handleMessage(msg: WSMessage) {
  const m = msg as any;
  if (CHALLENGE_SCOPED[m.type] && m.challenge && m.challenge !== getViewedChallenge()) {
    return;
  }
  handleSwarmConfigEvent(apiUrl, msg);
  challengeSelector.handleMessage(msg);
  chartPanel.handleMessage(msg);
}

onViewedChallengeChange(() => {
  chartPanel.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
  void loadInitialState(apiUrl);
});

// ── Hydrate from /api/state + /api/replay ──
async function loadInitialState(apiUrl: string) {
  try {
    const q = `?challenge=${encodeURIComponent(getViewedChallenge())}`;
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

    chartPanel.seedHistory(replay);

    if (state.leaderboard?.length) {
      handleMessage({
        type: "leaderboard_update",
        challenge: getViewedChallenge(),
        entries: state.leaderboard,
        timestamp: new Date().toISOString(),
      });
    }

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
