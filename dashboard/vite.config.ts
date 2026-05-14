import { defineConfig } from "vite";

export default defineConfig({
  publicDir: "static",
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        ideas: "ideas.html",
        diversity: "diversity.html",
        benchmark: "benchmark.html",
        trajectories: "trajectories.html",
      },
    },
  },
});
