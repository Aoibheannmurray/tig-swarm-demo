import "../../style.css";
import { SwarmWebSocket } from "../../lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { LeaderboardPanel } from "../../panels/leaderboard";
import { ChallengeSelectorPanel } from "../../panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "../../lib/swarmConfig";
import { getViewedChallenge, onViewedChallengeChange } from "../../lib/viewedChallenge";
import { isMessageForChallenge } from "../../lib/messageScope";
import type { WSMessage } from "../../types";

const { wsUrl, apiUrl } = getDashboardUrls();

// ── Initialize panels ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const panelEl = document.getElementById("panel-leaderboard-page")!;
panelEl.innerHTML = `
  <div class="page-flex">
    <div class="ideas-header">
      <div class="ideas-title">
        <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />
        <span class="ideas-title-text">Leaderboard</span>
      </div>
      <div class="ideas-nav">
        <a href="/" class="ideas-nav-link">Dashboard</a>
        <a href="/ideas.html" class="ideas-nav-link">Ideas</a>
        <a href="/diversity.html" class="ideas-nav-link">Diversity</a>
        <a href="/benchmark.html" class="ideas-nav-link">Benchmark</a>
        <a href="/trajectories.html" class="ideas-nav-link">Trajectories</a>
        <span class="ideas-nav-active">Leaderboard</span>
      </div>
    </div>
    <div class="page-body leaderboard-page" id="panel-leaderboard-body"></div>
  </div>
`;
const leaderboardPanel = new LeaderboardPanel();
leaderboardPanel.init(document.getElementById("panel-leaderboard-body")!);

function handleMessage(msg: WSMessage) {
  if (!isMessageForChallenge(msg, getViewedChallenge())) return;
  handleSwarmConfigEvent(apiUrl, msg);
  challengeSelector.handleMessage(msg);
  leaderboardPanel.handleMessage(msg);
}

onViewedChallengeChange(() => {
  leaderboardPanel.handleMessage({ type: "reset", timestamp: new Date().toISOString() });
  void loadInitialState(apiUrl);
});

// ── Hydrate from /api/state ──
async function loadInitialState(apiUrl: string) {
  try {
    const q = `?challenge=${encodeURIComponent(getViewedChallenge())}`;
    const stateRes = await fetch(`${apiUrl}/api/state${q}`);
    if (!stateRes.ok) return;
    const state = await stateRes.json();
    if (state.leaderboard?.length) {
      handleMessage({
        type: "leaderboard_update",
        challenge: getViewedChallenge(),
        entries: state.leaderboard,
        timestamp: new Date().toISOString(),
      });
    }
    console.log(`[Leaderboard] Loaded ${state.leaderboard?.length ?? 0} entries`);
  } catch (e) {
    console.warn("[Leaderboard] Failed to load initial state:", e);
  }
}

installKeyboardNav("leaderboard");

// ── Connect ──
console.log(`[Leaderboard] Connecting to ${wsUrl}, API: ${apiUrl}`);
void loadSwarmConfig(apiUrl).then(() => loadInitialState(apiUrl));
const ws = new SwarmWebSocket(wsUrl);
ws.onMessage(handleMessage);
ws.connect();
