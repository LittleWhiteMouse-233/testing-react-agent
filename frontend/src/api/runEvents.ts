import type { StoredRunEvent } from "./contracts";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseStoredRunEvent(raw: string): StoredRunEvent {
  const value: unknown = JSON.parse(raw);
  if (
    !isRecord(value) ||
    typeof value.event_id !== "string" ||
    typeof value.sequence !== "number" ||
    !Number.isInteger(value.sequence) ||
    value.sequence < 1 ||
    typeof value.occurred_at !== "string" ||
    !isRecord(value.event) ||
    typeof value.event.type !== "string"
  ) {
    throw new TypeError("Invalid StoredRunEvent envelope");
  }
  return value as StoredRunEvent;
}
