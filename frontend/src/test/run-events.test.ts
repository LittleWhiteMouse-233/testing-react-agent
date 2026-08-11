import { describe, expect, it } from "vitest";
import { parseStoredRunEvent } from "../api/runEvents";

describe("parseStoredRunEvent", () => {
  it("accepts a generated-contract event envelope", () => {
    const event = parseStoredRunEvent(JSON.stringify({
      event_id: "11111111-1111-4111-8111-111111111111",
      sequence: 1,
      occurred_at: "2026-08-09T00:00:00Z",
      event: {
        type: "run.started",
        test_run_id: "22222222-2222-4222-8222-222222222222",
        task_run_id: null
      }
    }));
    expect(event.event.type).toBe("run.started");
  });

  it("rejects an envelope without an event discriminator", () => {
    expect(() => parseStoredRunEvent(JSON.stringify({
      event_id: "event",
      sequence: 1,
      occurred_at: "now",
      event: {}
    }))).toThrow("Invalid StoredRunEvent envelope");
  });
});
