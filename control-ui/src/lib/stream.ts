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

function push(store: typeof fleetLog, line: LogLine, cap = 4000) {
  store.update((arr) => {
    const next = arr.length >= cap ? arr.slice(arr.length - cap + 1) : arr.slice();
    next.push(line);
    return next;
  });
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
  ws.onopen = () => streamConnected.set(true);
  ws.onclose = () => {
    streamConnected.set(false);
    ws = null;
  };
}
