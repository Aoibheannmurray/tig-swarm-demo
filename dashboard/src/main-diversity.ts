import "./style.css";
import { initParticles } from "./lib/particles";
import { SwarmWebSocket } from "./lib/websocket";
import { MockDataGenerator } from "./mock";
import { DiversityPanel } from "./panels/diversity";
import { ChallengeSelectorPanel } from "./panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "./lib/swarmConfig";
import { onViewedChallengeChange } from "./lib/viewedChallenge";
import type { WSMessage } from "./types";

// ── Config ──
const params = new URLSearchParams(window.location.search);
const isMock = params.has("mock");
const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = params.get("ws") || `${wsProtocol}//${window.location.host}/ws/dashboard`;

// ── Background particles ──
const canvas = document.getElementById("particleCanvas") as HTMLCanvasElement;
initParticles(canvas);

// ── Initialize single panel ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const panel = new DiversityPanel();
panel.init(document.getElementById("panel-diversity")!);

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
}

onViewedChallengeChange(() => {
  panel.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
  // The DiversityPanel re-fetches /api/diversity on its own when the
  // challenge changes via the swarm_config_updated WS event flow; no
  // explicit setChallenge call needed here.
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
