import "./style.css";
import { initParticles } from "./lib/particles";
import { SwarmWebSocket } from "./lib/websocket";
import { MockDataGenerator } from "./mock";
import { IdeasTree } from "./panels/ideas-tree";
import { StrategyLeaderboardPanel } from "./panels/strategy-leaderboard";
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

// ── Initialize ideas tree ──
const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

const root = document.getElementById("ideas-root")!;
const ideasTree = new IdeasTree();
ideasTree.init(root);

const strategyLb = new StrategyLeaderboardPanel();
const strategyMount = document.getElementById("strategy-lb-mount");
if (strategyMount) strategyLb.init(strategyMount);

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
  ideasTree.handleMessage(msg);
  strategyLb.handleMessage(msg);
}

onViewedChallengeChange(() => {
  // Reset and re-load for the new challenge.
  ideasTree.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
  strategyLb.handleMessage({ type: "reset", timestamp: new Date().toISOString() } as any);
  void loadInitialState(getApiUrl());
});

// ── Keyboard navigation ──
document.addEventListener("keydown", (e) => {
  if (e.key === "1") window.location.href = "/";
  if (e.key === "3") window.location.href = "/diversity.html";
  if (e.key === "4") window.location.href = "/benchmark.html";
});

// ── Fetch initial state ──
async function loadInitialState(apiUrl: string) {
  try {
    const ch = getViewedChallenge();
    const q = `?challenge=${encodeURIComponent(ch)}`;
    const res = await fetch(`${apiUrl}/api/state${q}`);
    if (!res.ok) return;
    const state = await res.json();

    // Replay all hypothesis outcomes.
    const allHyps = state.recent_hypotheses || [];

    for (const h of allHyps) {
      handleMessage({
        type: "hypothesis_proposed",
        hypothesis_id: h.id,
        agent_name: h.agent_name,
        agent_id: h.agent_id || "",
        title: h.title,
        description: h.description || "",
        strategy_tag: h.strategy_tag,
        parent_hypothesis_id: h.parent_hypothesis_id || null,
        // Use the original `created_at` from the server so timestamps don't
        // get refreshed to "just now" on every challenge switch.
        timestamp: h.created_at || new Date().toISOString(),
      });
    }

    console.log(`[Ideas] Loaded ${allHyps.length} hypotheses`);

    const msgRes = await fetch(`${apiUrl}/api/messages?limit=50&challenge=${encodeURIComponent(getViewedChallenge())}`);

    if (msgRes.ok) {
      const messages = await msgRes.json();
      for (const m of messages.reverse()) {
        handleMessage({
          type: "chat_message",
          challenge: getViewedChallenge(),
          message_id: m.id,
          agent_name: m.agent_name,
          agent_id: m.agent_id,
          content: m.content,
          msg_type: m.msg_type,
          timestamp: m.created_at,
        });
      }
    }
  } catch (e) {
    console.warn("[Ideas] Failed to load initial state:", e);
  }
}

// ── Connect ──
if (isMock) {
  console.log("[Ideas] Running in MOCK mode");
  const mock = new MockDataGenerator();
  mock.onMessage(handleMessage);
  mock.start();
} else {
  const apiUrl = getApiUrl();
  console.log(`[Ideas] Connecting to ${wsUrl}, API: ${apiUrl}`);
  void loadSwarmConfig(apiUrl).then(() => {
    void loadInitialState(apiUrl);
  });
  const ws = new SwarmWebSocket(wsUrl);
  ws.onMessage(handleMessage);
  ws.connect();
}
