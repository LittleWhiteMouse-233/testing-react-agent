import validateStoredRunEvent from "./generated/validateStoredRunEvent.mjs";
import { runEventStreamUrl } from "./client";
import type { StoredRunEvent } from "./contracts";

export interface RunEventStreamCallbacks {
  onEvent: (stored: StoredRunEvent) => void;
  onTerminal: () => void;
  onContractError: (error: unknown) => void;
  onConnection?: (state: "connected" | "reconnecting") => void;
}

export interface RunEventSource {
  close: () => void;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onopen?: ((event: Event) => void) | null;
  onerror?: ((event: Event) => void) | null;
}

export type RunEventSourceFactory = (url: string) => RunEventSource;

export function parseStoredRunEvent(raw: string): StoredRunEvent {
  const value: unknown = JSON.parse(raw);
  if (!validateStoredRunEvent(value)) {
    throw new TypeError("SSE event does not satisfy the OpenAPI contract");
  }
  return value;
}

export function mergeStoredRunEvents(
  current: StoredRunEvent[],
  incoming: StoredRunEvent[]
): StoredRunEvent[] {
  // Both HTTP history and SSE increments arrive in sequence order.
  const merged: StoredRunEvent[] = [];
  let currentIndex = 0;
  let incomingIndex = 0;
  while (currentIndex < current.length && incomingIndex < incoming.length) {
    const existing = current[currentIndex]!;
    const next = incoming[incomingIndex]!;
    if (existing.sequence < next.sequence) {
      merged.push(existing); currentIndex++;
    } else {
      merged.push(next); incomingIndex++;
      if (existing.sequence === next.sequence) currentIndex++;
    }
  }
  return [...merged, ...current.slice(currentIndex), ...incoming.slice(incomingIndex)];
}

export function latestScreenshotArtifactId(events: StoredRunEvent[]): string | undefined {
  for (const stored of [...events].reverse()) {
    if (stored.event.type !== "message.appended") continue;
    const image = [...stored.event.message.content].reverse().find(
      (block) => block.type === "image_artifact"
    );
    if (image?.type === "image_artifact") return image.artifact_id;
  }
  return undefined;
}

export function isTerminalRunEvent(stored: StoredRunEvent): boolean {
  return stored.event.type === "run.finished" || stored.event.type === "run.cancelled";
}

export function openRunEventStream(
  testRunId: string,
  after: number,
  callbacks: RunEventStreamCallbacks,
  createEventSource: RunEventSourceFactory = (url) => new EventSource(url)
): RunEventSource {
  const source = createEventSource(runEventStreamUrl(testRunId, after));
  if (callbacks.onConnection) {
    source.onopen = () => callbacks.onConnection?.("connected");
    source.onerror = () => callbacks.onConnection?.("reconnecting");
  }
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
