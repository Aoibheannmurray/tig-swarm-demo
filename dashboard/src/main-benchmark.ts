import "./style.css";
import { initParticles } from "./lib/particles";
import { SwarmWebSocket } from "./lib/websocket";
import { MockDataGenerator } from "./mock";
import { ChartPanel } from "./panels/chart";
import { ChallengeSelectorPanel } from "./panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "./lib/swarmConfig";
import { getViewedChallenge, onViewedChallengeChange } from "./lib/viewedChallenge";
import type { WSMessage } from "./types";

// ── Config ──
const params = new URLSearchParams(window.location.search);
const isMock = params.has("mock");
const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = params.get("ws") || `${wsProtocol}//${window.location.host}/ws/dashboard`;

function getApiUrl(): string {
  const explicit = params.get("api");
  if (explicit) return explicit;
  return wsUrl
    .replace("ws://", "http://")
    .replace("wss://", "https://")
    .replace("/ws/dashboard", "");
}

// ── Background particles ──
const canvas = document.getElementById("particleCanvas") as HTMLCanvasElement;
initParticles(canvas);

// ── Initialize single panel ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const chartPanel = new ChartPanel();
chartPanel.init(document.getElementById("panel-chart")!);

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
  handleSwarmConfigEvent(getApiUrl(), msg);
  challengeSelector.handleMessage(msg);
  chartPanel.handleMessage(msg);
}

onViewedChallengeChange(() => {
  chartPanel.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
  void loadInitialState(getApiUrl());
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

// ── Keyboard navigation ──
document.addEventListener("keydown", (e) => {
  if (e.key === "1") window.location.href = "/";
  if (e.key === "2") window.location.href = "/ideas.html";
  if (e.key === "3") window.location.href = "/diversity.html";
});

// ── Connect ──
if (isMock) {
  console.log("[Benchmark] Running in MOCK mode");
  const mock = new MockDataGenerator();
  mock.onMessage(handleMessage);
  mock.start();
} else {
  const apiUrl = getApiUrl();
  console.log(`[Benchmark] Connecting to ${wsUrl}, API: ${apiUrl}`);
  void loadSwarmConfig(apiUrl).then(() => loadInitialState(apiUrl));
  const ws = new SwarmWebSocket(wsUrl);
  ws.onMessage(handleMessage);
  ws.connect();
}
