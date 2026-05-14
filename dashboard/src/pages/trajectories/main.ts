import "../../style.css";
import { getDashboardUrls, installKeyboardNav } from "../../lib/bootstrap";
import { TrajectoriesPanel } from "./trajectories";
import { ChallengeSelectorPanel } from "../../panels/challenge-selector";
import { loadSwarmConfig } from "../../lib/swarmConfig";
import { onViewedChallengeChange } from "../../lib/viewedChallenge";

const { apiUrl } = getDashboardUrls();

const selectorMount = document.getElementById("panel-challenge-selector");
const challengeSelector = new ChallengeSelectorPanel();
if (selectorMount) challengeSelector.init(selectorMount);

void loadSwarmConfig(apiUrl);

const panel = new TrajectoriesPanel();
panel.init(document.getElementById("panel-trajectories")!, apiUrl);

onViewedChallengeChange(() => {
  // Trajectories panel hits its own REST endpoint — re-init to pick up
  // the new ?challenge= filter on its next fetch.
  panel.init(document.getElementById("panel-trajectories")!, apiUrl);
});

installKeyboardNav("trajectories");
