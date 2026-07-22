import { useEffect, useState } from "react";

import {
  getRuntimeEvents,
  runtimeEventStreamUrl,
  type RuntimeEvent,
} from "./api";

export type RuntimeEventState = {
  lastSequence: number;
  events: RuntimeEvent[];
  connected: boolean;
};

type SelectedRuntimeEventState = {
  runId?: string;
  value: RuntimeEventState;
};

const runtimeEventCache = new Map<string, RuntimeEventState>();
const emptyState: RuntimeEventState = { lastSequence: 0, events: [], connected: false };
const eventTypes = [
  "run.created", "run.started", "run.completed", "run.failed", "run.interrupted",
  "run.cancel_requested", "run.cancelled", "attempt.started", "attempt.completed",
  "attempt.interrupted", "phase.started", "phase.completed", "phase.failed",
  "phase.skipped", "agent.started", "agent.completed", "agent.failed",
  "model.started", "model.completed", "model.failed", "tool.proposed", "tool.started",
  "tool.completed", "tool.failed", "tool.rejected", "tool.skipped",
  "evidence.persisted", "evidence.rejected", "checkpoint.created",
  "recovery.started", "recovery.completed", "recovery.rejected",
  "runtime.opaque",
];

function mergeRuntimeEvents(runId: string, incoming: RuntimeEvent[]): RuntimeEventState {
  const current = runtimeEventCache.get(runId) ?? emptyState;
  const bySequence = new Map(current.events.map((event) => [event.sequence, event]));
  for (const event of incoming) {
    if (event.sequence <= current.lastSequence && bySequence.has(event.sequence)) {
      continue;
    }
    bySequence.set(event.sequence, event);
  }
  const events = [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
  const next = {
    lastSequence: events[events.length - 1]?.sequence ?? current.lastSequence,
    events,
    connected: current.connected,
  };
  runtimeEventCache.set(runId, next);
  return next;
}

export function useRuntimeEvents(runId?: string): RuntimeEventState {
  const [selected, setSelected] = useState<SelectedRuntimeEventState>(() => ({
    runId,
    value: runId ? runtimeEventCache.get(runId) ?? emptyState : emptyState,
  }));

  useEffect(() => {
    if (!runId) {
      setSelected({ runId, value: emptyState });
      return;
    }
    let disposed = false;
    let source: EventSource | undefined;
    let reconnectTimer: number | undefined;
    let retry = 0;

    const connect = () => {
      if (disposed) return;
      const current = runtimeEventCache.get(runId) ?? emptyState;
      source = new EventSource(runtimeEventStreamUrl(runId, current.lastSequence));
      const receive = (message: MessageEvent<string>) => {
        try {
          const event = JSON.parse(message.data) as RuntimeEvent;
          const latest = runtimeEventCache.get(runId) ?? emptyState;
          if (event.sequence <= latest.lastSequence) return;
          const next = mergeRuntimeEvents(runId, [event]);
          next.connected = true;
          runtimeEventCache.set(runId, next);
          setSelected({ runId, value: { ...next } });
          retry = 0;
        } catch {
          // 非法 frame 不进入投影，durable catch-up 会恢复可信序列。
        }
      };
      for (const eventType of eventTypes) source.addEventListener(eventType, receive as EventListener);
      source.onopen = () => {
        const latest = runtimeEventCache.get(runId) ?? emptyState;
        const next = { ...latest, connected: true };
        runtimeEventCache.set(runId, next);
        setSelected({ runId, value: next });
      };
      source.onerror = async () => {
        if (source) source.close();
        if (disposed) return;
        const current = runtimeEventCache.get(runId) ?? emptyState;
        try {
          const durable = await getRuntimeEvents(runId, current.lastSequence, 500);
          if (!disposed) {
            setSelected({
              runId,
              value: { ...mergeRuntimeEvents(runId, durable), connected: false },
            });
          }
        } catch {
          // 后续有界退避继续从最后 durable sequence 重试。
        }
        const delay = Math.min(15_000, 1_000 * 2 ** retry);
        retry = Math.min(retry + 1, 4);
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    setSelected({ runId, value: runtimeEventCache.get(runId) ?? emptyState });
    connect();
    return () => {
      disposed = true;
      if (source) source.close();
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    };
  }, [runId]);

  return selected.runId === runId
    ? selected.value
    : runId
      ? runtimeEventCache.get(runId) ?? emptyState
      : emptyState;
}
