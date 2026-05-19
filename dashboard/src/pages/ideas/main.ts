import "../../style.css";
import { SwarmWebSocket } from "../../lib/websocket";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { IdeasTree } from "./ideas-tree";
import { StrategyLeaderboardPanel } from "./strategy-leaderboard";
import { ChallengeSelectorPanel } from "../../panels/challenge-selector";
import { loadSwarmConfig, handleWsEvent as handleSwarmConfigEvent } from "../../lib/swarmConfig";
import { getViewedChallenge, onViewedChallengeChange } from "../../lib/viewedChallenge";
import { isMessageForChallenge } from "../../lib/messageScope";
import type { WSMessage } from "../../types";

const { wsUrl, apiUrl } = getDashboardUrls();

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

function handleMessage(msg: WSMessage) {
  if (!isMessageForChallenge(msg, getViewedChallenge())) return;
  handleSwarmConfigEvent(apiUrl, msg);
  challengeSelector.handleMessage(msg);
  ideasTree.handleMessage(msg);
  strategyLb.handleMessage(msg);
}

onViewedChallengeChange(() => {
  // Reset and re-load for the new challenge.
  ideasTree.handleMessage({ type: "reset", timestamp: new Date().toISOString() });
  strategyLb.handleMessage({ type: "reset", timestamp: new Date().toISOString() });
  void loadInitialState(apiUrl);
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

installKeyboardNav("ideas");

// ── Connect ──
console.log(`[Ideas] Connecting to ${wsUrl}, API: ${apiUrl}`);
void loadSwarmConfig(apiUrl).then(() => {
  void loadInitialState(apiUrl);
});
const ws = new SwarmWebSocket(wsUrl);
ws.onMessage(handleMessage);
ws.connect();
