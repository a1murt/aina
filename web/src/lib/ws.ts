"use client";

import { useEffect } from "react";

import { getToken, logout } from "@/lib/api/client";
import type { Snapshot } from "@/lib/api/types";
import { useLive } from "@/lib/live-store";

const MAX_BACKOFF_MS = 10_000;
/** The API sends a clock tick every 5 s: silence this long shows the badge as not live. */
const QUIET_MS = 12_000;
/** Silence this long means a dead connection: close and reconnect. */
const STALE_MS = 20_000;
const PING_MS = 25_000;

/**
 * One WebSocket per tab (/ws/live, same origin through the Next proxy): snapshot on connect,
 * deltas into the Zustand store, automatic reconnect with exponential backoff (the server sends
 * a fresh snapshot after every reconnect). Close code 4401 = expired token → login.
 *
 * The badge follows the data, not `navigator.onLine` (DEMO-8, FE-8): the browser reports
 * "offline" when Wi-Fi drops even though the plant stack on the same host keeps streaming.
 * Any message on the open socket means "live"; an open but silent socket is shown as
 * offline / reconnecting after {@link QUIET_MS}; coming back online asks for a fresh snapshot.
 */
export function useLiveConnection(enabled: boolean = true): void {
  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    /** performance.now() when the current socket opened (watchdog grace for a fresh socket). */
    let openedAt = 0;
    let attempt = 0;
    let stopped = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const { setStatus, applySnapshot, applyMessage } = useLive.getState();

    const markLive = () => {
      if (useLive.getState().status !== "live") setStatus("live");
    };
    const notLive = () => (navigator.onLine ? "reconnecting" : "offline");

    const scheduleReconnect = () => {
      if (stopped) return;
      setStatus(notLive());
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
      socket.onopen = () => {
        openedAt = performance.now();
      };
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
          markLive();
          return;
        }
        // data flows again after a blip (the socket survived): the badge follows the data
        if (useLive.getState().ready) markLive();
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
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const s = useLive.getState();
      const silentMs = performance.now() - Math.max(s.lastMessageAt, openedAt);
      if (silentMs > STALE_MS) ws.close();
      else if (silentMs > QUIET_MS && s.status === "live") setStatus(notLive());
    }, 2_000);
    const ping = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ping: Date.now() }));
    }, PING_MS);
    const onOnline = () => {
      if (ws?.readyState === WebSocket.OPEN) {
        // the socket survived the blip: a fresh snapshot brings the badge back to live
        ws.send(JSON.stringify({ resync: true }));
      } else if (!ws) {
        attempt = 0;
        clearTimeout(retryTimer);
        connect();
      }
    };
    const onOffline = () => {
      // an open socket may still stream from a local stack — let the watchdog judge by data
      if (ws?.readyState !== WebSocket.OPEN) setStatus("offline");
    };
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
