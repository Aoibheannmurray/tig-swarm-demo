import "../../style.css";
import { SwarmWebSocket } from "../../lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { DiversityPanel } from "../../panels/diversity";
import { InspirationMatrixPanel } from "./inspiration-matrix";
import { ChallengeSelectorPanel } from "../../panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "../../lib/swarmConfig";
import { onViewedChallengeChange } from "../../lib/viewedChallenge";
import type { WSMessage } from "../../types";

const { wsUrl, apiUrl } = getDashboardUrls();

// ── Initialize single panel ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const panelEl = document.getElementById("panel-diversity")!;
panelEl.innerHTML = `
  <div class="page-flex">
    <div class="ideas-header">
      <div class="ideas-title">
        <img class="stats-mark" src="/prometheus-icon.png" alt="" draggable="false" />
        <span class="ideas-title-text">Diversity Map</span>
      </div>
      <div class="ideas-nav">
        <a href="/" class="ideas-nav-link">Dashboard</a>
        <a href="/ideas.html" class="ideas-nav-link">Ideas</a>
        <span class="ideas-nav-active">Diversity</span>
        <a href="/benchmark.html" class="ideas-nav-link">Benchmark</a>
        <a href="/trajectories.html" class="ideas-nav-link">Trajectories</a>
      </div>
    </div>
    <div class="page-body diversity-page-body">
      <div id="panel-diversity-body"></div>
      <div id="panel-inspiration-body"></div>
    </div>
  </div>
`;
const panel = new DiversityPanel();
panel.init(document.getElementById("panel-diversity-body")!);
const inspirationPanel = new InspirationMatrixPanel();
inspirationPanel.init(document.getElementById("panel-inspiration-body")!);

function handleMessage(msg: WSMessage) {
  handleSwarmConfigEvent(apiUrl, msg);
  challengeSelector.handleMessage(msg);
  panel.handleMessage(msg);
  inspirationPanel.handleMessage(msg);
}

onViewedChallengeChange((c) => {
  const resetMsg = { type: "reset", timestamp: new Date().toISOString() } as any;
  panel.handleMessage(resetMsg);
  inspirationPanel.handleMessage(resetMsg);
  // Each panel's setChallenge clears its rendered state and triggers a
  // fresh fetch for the new challenge. Without this, switching challenges
  // leaves both grids blank until the next leaderboard_update event
  // happens to arrive — which on a quiet historical challenge may never.
  panel.setChallenge(c);
  inspirationPanel.setChallenge(c);
});

installKeyboardNav("diversity");

// ── Connect ──
console.log(`[Diversity] Connecting to ${wsUrl}`);
void loadSwarmConfig(apiUrl);
const ws = new SwarmWebSocket(wsUrl);
ws.onMessage(handleMessage);
ws.connect();
