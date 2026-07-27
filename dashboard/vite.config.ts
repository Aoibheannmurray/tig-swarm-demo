/// <reference types="vitest/config" />
import { defineConfig } from "vite";

export default defineConfig({
  publicDir: "static",
  test: {
    // Vitest stubs CSS imports to an empty string by default. Process them
    // instead, so a test can read style.css (via ?inline) and assert that the
    // classes a panel emits are actually styled — a mismatch type-checks and
    // builds cleanly, so nothing else catches it.
    css: true,
  },
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        ideas: "ideas.html",
        diversity: "diversity.html",
        benchmark: "benchmark.html",
        trajectories: "trajectories.html",
        leaderboard: "leaderboard.html",
      },
    },
  },
});
