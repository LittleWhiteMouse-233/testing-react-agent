import validateStoredRunEvent from "./generated/validateStoredRunEvent.mjs";
import { apiFetch, runEventStreamUrl } from "./client";
import type { StoredRunEvent } from "./contracts";

export interface RunEventStreamCallbacks {
  onEvent: (stored: StoredRunEvent) => void;
  onTerminal: () => void;
  onContractError: (error: unknown) => void;
}

export interface RunEventSource {
  close: () => void;
  onmessage: ((event: MessageEvent<string>) => void) | null;
}

export type RunEventSourceFactory = (url: string) => RunEventSource;
export type RunEventHistoryLoader = (
  testRunId: string
) => Promise<StoredRunEvent[]>;

export function parseStoredRunEvent(raw: string): StoredRunEvent {
  const value: unknown = JSON.parse(raw);
  if (!validateStoredRunEvent(value)) {
    throw new TypeError("SSE event does not satisfy the OpenAPI contract");
  }
  return value;
}

export function mergeStoredRunEvent(
  current: StoredRunEvent[],
  incoming: StoredRunEvent
): StoredRunEvent[] {
  return [...current.filter((entry) => entry.sequence !== incoming.sequence), incoming]
    .sort((left, right) => left.sequence - right.sequence);
}

export function isTerminalRunEvent(stored: StoredRunEvent): boolean {
  return stored.event.type === "run.finished" || stored.event.type === "run.cancelled";
}

export async function restoreStoredRunEvents(
  testRunId: string
): Promise<StoredRunEvent[]> {
  const { data, error } = await apiFetch.GET("/api/runs/{test_run_id}/events", {
    params: {
      path: { test_run_id: testRunId },
      query: { after: 0 }
    }
  });
  if (error) throw error;
  if (!data) throw new Error("Run event history response has no body");
  return [...data.items].sort((left, right) => left.sequence - right.sequence);
}

export function openRunEventStream(
  testRunId: string,
  after: number,
  callbacks: RunEventStreamCallbacks,
  createEventSource: RunEventSourceFactory = (url) => new EventSource(url)
): RunEventSource {
  const source = createEventSource(runEventStreamUrl(testRunId, after));
  source.onmessage = (raw) => {
    let stored: StoredRunEvent;
    try {
      stored = parseStoredRunEvent(raw.data);
    } catch (error) {
      source.close();
      callbacks.onContractError(error);
      return;
    }
    callbacks.onEvent(stored);
    if (isTerminalRunEvent(stored)) {
      source.close();
      callbacks.onTerminal();
    }
  };
  return source;
}

export async function startRunEventStream(
  testRunId: string,
  callbacks: RunEventStreamCallbacks,
  loadHistory: RunEventHistoryLoader = restoreStoredRunEvents,
  createEventSource?: RunEventSourceFactory
): Promise<{ events: StoredRunEvent[]; source: RunEventSource | null }> {
  let events: StoredRunEvent[] = [];
  try {
    events = await loadHistory(testRunId);
  } catch {
    // The persisted SSE endpoint can still replay from sequence zero.
  }
  if (events.some(isTerminalRunEvent)) return { events, source: null };
  const after = events.at(-1)?.sequence ?? 0;
  return {
    events,
    source: openRunEventStream(
      testRunId,
      after,
      callbacks,
      createEventSource
    )
  };
}
