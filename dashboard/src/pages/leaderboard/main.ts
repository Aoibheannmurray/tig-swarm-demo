import "../../style.css";
import { SwarmWebSocket } from "../../lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { renderPageHeader } from "../../lib/pageChrome";
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
    ${renderPageHeader("leaderboard", "Leaderboard")}
    <div class="page-body leaderboard-page" id="panel-leaderboard-body"></div>
  </div>
`;
// Dedicated page: no row cap — show every participating agent (the list
// scrolls). The dashboard tile keeps the default cap.
const leaderboardPanel = new LeaderboardPanel({ maxRows: Infinity });
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
    // The mainnet bar for this challenge — drawn as a threshold on the
    // chart and a ranked row in the leaderboard. Sent even when null so a
    // challenge switch clears the previous challenge's bar.
    handleMessage({
      type: "mainnet_baseline",
      challenge: getViewedChallenge(),
      baseline: state.mainnet_baseline ?? null,
      timestamp: new Date().toISOString(),
    });
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
