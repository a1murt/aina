"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { isRetryable } from "@/lib/api/client";
import { sendAction, type Action } from "@/lib/actions";

/**
 * Offline action queue of the operator terminal (SPEC §13.2): an action that cannot reach the API
 * (no network, proxy or API down: network error / 502–504) is stored in IndexedDB and retried
 * automatically — every few seconds and when the tablet comes back online — in creation order.
 */
const DB_NAME = "qost-terminal";
const STORE = "actions";
const RETRY_MS = 5_000;

export interface QueuedAction {
  id: string;
  created: number;
  label: string;
  action: Action;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE, { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await openDb();
  return new Promise<T>((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const req = fn(t.objectStore(STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    t.oncomplete = () => db.close();
  });
}

export const queueStore = {
  add: (item: QueuedAction) => tx("readwrite", (s) => s.put(item)),
  remove: (id: string) => tx("readwrite", (s) => s.delete(id)),
  all: async () => ((await tx("readonly", (s) => s.getAll())) as QueuedAction[]).sort((a, b) => a.created - b.created),
};

export type SubmitResult = "sent" | "queued";

/** Submit actions through the queue; `pending` = actions not delivered yet. */
export function useOfflineQueue() {
  const [pending, setPending] = useState<QueuedAction[]>([]);
  const flushing = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setPending(await queueStore.all());
    } catch {
      /* IndexedDB unavailable (private mode): nothing queued */
    }
  }, []);

  const flush = useCallback(async () => {
    if (flushing.current) return;
    flushing.current = true;
    try {
      for (const item of await queueStore.all()) {
        try {
          await sendAction(item.action);
          await queueStore.remove(item.id);
        } catch (err) {
          if (isRetryable(err)) break; // still offline: keep the order, try later
          await queueStore.remove(item.id); // rejected by the API (4xx): drop it
        }
      }
    } catch {
      /* IndexedDB unavailable */
    } finally {
      flushing.current = false;
      await refresh();
    }
  }, [refresh]);

  useEffect(() => {
    void refresh().then(flush);
    const id = setInterval(() => void flush(), RETRY_MS);
    const onOnline = () => void flush();
    window.addEventListener("online", onOnline);
    return () => {
      clearInterval(id);
      window.removeEventListener("online", onOnline);
    };
  }, [flush, refresh]);

  const submit = useCallback(
    async (action: Action, label: string): Promise<SubmitResult> => {
      try {
        await sendAction(action);
        return "sent";
      } catch (err) {
        if (!isRetryable(err)) throw err;
        await queueStore.add({ id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, created: Date.now(), label, action });
        await refresh();
        return "queued";
      }
    },
    [refresh],
  );

  return { pending, submit, flush };
}
