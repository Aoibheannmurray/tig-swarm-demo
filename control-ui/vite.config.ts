import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Two entries, one shared codebase (see plan):
//   index.html        → local companion (contributor + host setup)   [mode local]
//   admin/index.html  → admin console served by the hosted server      [mode hosted]
// The hosted FastAPI server mounts the built dist as static (html=true), so the
// admin console is reachable at /admin/  (admin/index.html).
export default defineConfig({
  plugins: [svelte()],
  build: {
    target: "es2020",
    rollupOptions: {
      input: {
        main: "index.html",
        admin: "admin/index.html",
      },
    },
  },
  server: {
    port: 5273,
    // During `npm run dev`, proxy the companion API to the running
    // `python control_server.py` so the UI works without a build.
    proxy: {
      "/local-api": {
        target: "http://127.0.0.1:8787",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
