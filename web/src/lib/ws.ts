"use client";

import { useEffect } from "react";

import { getToken, logout } from "@/lib/api/client";
import type { Snapshot } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";

const MAX_BACKOFF_MS = 10_000;
/** The API sends a clock tick every 5 s: silence for longer means a dead connection. */
const STALE_MS = 20_000;
const PING_MS = 25_000;

/**
 * One WebSocket per tab (/ws/live, same origin through the Next proxy): snapshot on connect,
 * deltas into the Zustand store, automatic reconnect with exponential backoff (the server sends
 * a fresh snapshot after every reconnect). Close code 4401 = expired token → login.
 */
export function useLiveConnection(enabled: boolean = true): void {
  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    let attempt = 0;
    let stopped = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const { setStatus, applySnapshot, applyMessage } = useLive.getState();

    const scheduleReconnect = () => {
      if (stopped) return;
      setStatus(navigator.onLine ? "reconnecting" : "offline");
      const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** attempt);
      attempt += 1;
      clearTimeout(retryTimer);
      retryTimer = setTimeout(connect, delay);
    };

    function connect() {
      if (stopped) return;
      const token = getToken();
      if (!token) {
        logout();
        return;
      }
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${proto}://${window.location.host}/ws/live?token=${encodeURIComponent(token)}`);
      ws = socket;
      socket.onmessage = (ev) => {
        let msg: { type?: string; data?: unknown };
        try {
          msg = JSON.parse(String(ev.data)) as { type?: string; data?: unknown };
        } catch {
          return;
        }
        if (msg.type === "snapshot" && msg.data && typeof msg.data === "object" && "lines" in msg.data) {
          applySnapshot(msg.data as Snapshot);
          attempt = 0;
          setStatus("live");
          return;
        }
        if (msg.type === "snapshot") {
          // engine rebuilt its view (reset/restart): ask for a fresh snapshot
          socket.send(JSON.stringify({ resync: true }));
          return;
        }
        if (msg.type && msg.data && typeof msg.data === "object") {
          applyMessage(msg.type, msg.data as Record<string, unknown>);
        }
      };
      socket.onclose = (ev) => {
        if (ws !== socket) return;
        ws = null;
        if (ev.code === 4401) {
          logout();
          return;
        }
        scheduleReconnect();
      };
      socket.onerror = () => socket.close();
    }

    const watchdog = setInterval(() => {
      const s = useLive.getState();
      if (ws && ws.readyState === WebSocket.OPEN && s.lastMessageAt && performance.now() - s.lastMessageAt > STALE_MS) {
        ws.close();
      }
    }, 5_000);
    const ping = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ping: Date.now() }));
    }, PING_MS);
    const onOnline = () => {
      if (!ws) {
        attempt = 0;
        clearTimeout(retryTimer);
        connect();
      }
    };
    const onOffline = () => setStatus("offline");
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);

    setStatus("connecting");
    connect();
    return () => {
      stopped = true;
      clearTimeout(retryTimer);
      clearInterval(watchdog);
      clearInterval(ping);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
      const s = ws;
      ws = null;
      s?.close();
    };
  }, [enabled]);
}
