import "@phosphor-icons/web/regular/style.css";
import "./style.css";
import { SwarmWebSocket } from "./lib/websocket";
import { MockDataGenerator } from "./mock";
import { DiversityPanel } from "./panels/diversity";
import { InspirationMatrixPanel } from "./panels/inspiration-matrix";
import { ChallengeSelectorPanel } from "./panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "./lib/swarmConfig";
import { onViewedChallengeChange } from "./lib/viewedChallenge";
import type { WSMessage } from "./types";

// ── Config ──
const params = new URLSearchParams(window.location.search);
const isMock = params.has("mock");
const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = params.get("ws") || `${wsProtocol}//${window.location.host}/ws/dashboard`;

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

function getApiUrl(): string {
  const explicit = params.get("api");
  if (explicit) return explicit;
  return wsUrl
    .replace("ws://", "http://")
    .replace("wss://", "https://")
    .replace("/ws/dashboard", "");
}

function handleMessage(msg: WSMessage) {
  handleSwarmConfigEvent(getApiUrl(), msg);
  challengeSelector.handleMessage(msg);
  panel.handleMessage(msg);
  inspirationPanel.handleMessage(msg);
}

onViewedChallengeChange(() => {
  const resetMsg = { type: "reset", timestamp: new Date().toISOString() } as any;
  panel.handleMessage(resetMsg);
  inspirationPanel.handleMessage(resetMsg);
});

// ── Keyboard navigation ──
document.addEventListener("keydown", (e) => {
  if (e.key === "1") window.location.href = "/";
  if (e.key === "2") window.location.href = "/ideas.html";
  if (e.key === "4") window.location.href = "/benchmark.html";
});

// ── Connect ──
if (isMock) {
  console.log("[Diversity] Running in MOCK mode");
  const mock = new MockDataGenerator();
  mock.onMessage(handleMessage);
  mock.start();
} else {
  console.log(`[Diversity] Connecting to ${wsUrl}`);
  void loadSwarmConfig(getApiUrl());
  const ws = new SwarmWebSocket(wsUrl);
  ws.onMessage(handleMessage);
  ws.connect();
}
