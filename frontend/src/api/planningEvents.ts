import validatePlanningEvent from "./generated/validatePlanningEvent.mjs";
import type { PlanningEvent } from "./contracts";
import { planningEventStreamUrl } from "./client";
import { openEventStream } from "./eventStream";

export function parsePlanningEvent(raw: string): PlanningEvent {
  const event: unknown = JSON.parse(raw);
  if (!validatePlanningEvent(event)) throw new TypeError("规划事件不符合 API 契约");
  return event;
}

export function openPlanningEventStream(
  testCaseId: string,
  callbacks: {
    onReady: () => void;
    onEvent: (event: PlanningEvent) => void;
    onContractError: (error: unknown) => void;
    onConnection: (state: "connected" | "reconnecting") => void;
  },
  createSource: (url: string) => EventSource = (url) => new EventSource(url)
): { close: () => void } {
  const source = openEventStream(planningEventStreamUrl(testCaseId), parsePlanningEvent, callbacks, createSource);
  source.addEventListener("ready", callbacks.onReady);
  return { close: () => {
    source.removeEventListener("ready", callbacks.onReady);
    source.onmessage = source.onopen = source.onerror = null;
    source.close();
  } };
}
