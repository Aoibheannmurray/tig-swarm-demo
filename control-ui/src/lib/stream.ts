import { writable } from "svelte/store";
import { connectStream } from "./api";

// Live event feed from the companion. A single WebSocket, shared across views.
export type LogLine = { name?: string; line: string };

export const fleetLog = writable<LogLine[]>([]);
export const deployLog = writable<LogLine[]>([]);
export const fleetStatus = writable<any>({ state: "idle", running: false, agents: {} });
export const deployStatus = writable<any>({ state: "idle" });
export const streamConnected = writable(false);

let ws: WebSocket | null = null;
// Auto-reconnect state. Without this, a single dropped connection (server
// restart, sleep/wake, network blip) left the companion silent until a manual
// page reload — the fleet kept running but its logs/status stopped updating.
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = 1000;
const MAX_RECONNECT_DELAY = 10000;
// The companion replays recent history on connect (see EventHub.history), so a
// reconnect catches up on anything missed during the gap.

function push(store: typeof fleetLog, line: LogLine, cap = 4000) {
  store.update((arr) => {
    const next = arr.length >= cap ? arr.slice(arr.length - cap + 1) : arr.slice();
    next.push(line);
    return next;
  });
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    ensureStream();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
}

export function ensureStream() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  ws = connectStream((ev) => {
    switch (ev.type) {
      case "log":
        push(fleetLog, { name: ev.name, line: ev.line });
        break;
      case "status":
        if (ev.fleet) fleetStatus.set(ev.fleet);
        break;
      case "deploy_log":
        push(deployLog, { line: ev.line });
        break;
      case "deploy_status":
        deployStatus.set({ state: ev.event, result: ev.result, error: ev.error });
        break;
    }
  });
  ws.onopen = () => {
    streamConnected.set(true);
    reconnectDelay = 1000; // reset backoff after a clean connect
  };
  ws.onclose = () => {
    streamConnected.set(false);
    ws = null;
    scheduleReconnect();
  };
  ws.onerror = () => {
    // Force the socket closed so onclose fires and drives the reconnect path.
    ws?.close();
  };
}
