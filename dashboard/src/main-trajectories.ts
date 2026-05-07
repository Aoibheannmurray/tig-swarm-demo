import "@phosphor-icons/web/regular/style.css";
import "./style.css";
import { TrajectoriesPanel } from "./panels/trajectories";
import { ChallengeSelectorPanel } from "./panels/challenge-selector";
import { loadSwarmConfig } from "./lib/swarmConfig";
import { onViewedChallengeChange } from "./lib/viewedChallenge";

const params = new URLSearchParams(window.location.search);
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

const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

void loadSwarmConfig(getApiUrl());

const panel = new TrajectoriesPanel();
panel.init(document.getElementById("panel-trajectories")!, getApiUrl());

onViewedChallengeChange(() => {
  // Trajectories panel hits its own REST endpoint — re-init to pick up
  // the new ?challenge= filter on its next fetch.
  panel.init(document.getElementById("panel-trajectories")!, getApiUrl());
});

document.addEventListener("keydown", (e) => {
  if (e.key === "1") window.location.href = "/";
  if (e.key === "2") window.location.href = "/ideas.html";
  if (e.key === "3") window.location.href = "/diversity.html";
  if (e.key === "4") window.location.href = "/benchmark.html";
});
