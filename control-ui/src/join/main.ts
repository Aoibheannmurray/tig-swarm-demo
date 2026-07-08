import "../app.css";
import { mount } from "svelte";
import App from "./App.svelte";

// Register the pass-through service worker so the join page is installable.
// Best-effort: unsupported browsers / non-secure origins just skip it.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

const app = mount(App, { target: document.getElementById("app")! });
export default app;
