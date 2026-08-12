import { describe, expect, it, vi } from "vitest";
import {
  mergeStoredRunEvent,
  openRunEventStream,
  parseStoredRunEvent,
  startRunEventStream,
  type RunEventSource
} from "../api/runEvents";

const EVENT_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "22222222-2222-4222-8222-222222222222";
const TASK_RUN_ID = "33333333-3333-4333-8333-333333333333";

function storedRunEvent(event: Record<string, unknown>) {
  return {
    event_id: EVENT_ID,
    sequence: 1,
    occurred_at: "2026-08-09T00:00:00Z",
    event
  };
}

function lifecycleEvent(type = "run.started") {
  return storedRunEvent({
    type,
    test_run_id: RUN_ID,
    task_run_id: null
  });
}

const validEventBranches = [
  { type: "run.started", test_run_id: RUN_ID, task_run_id: null },
  { type: "run.finished", test_run_id: RUN_ID, task_run_id: null },
  { type: "run.cancelled", test_run_id: RUN_ID, task_run_id: null },
  { type: "task.started", test_run_id: RUN_ID, task_run_id: TASK_RUN_ID },
  { type: "task.finished", test_run_id: RUN_ID, task_run_id: TASK_RUN_ID },
  {
    type: "cycle.started",
    test_run_id: RUN_ID,
    task_run_id: TASK_RUN_ID,
    cycle_count: 1
  },
  {
    type: "tool.started",
    test_run_id: RUN_ID,
    task_run_id: TASK_RUN_ID,
    call_id: "tool-call-1"
  },
  {
    type: "execution.error",
    test_run_id: RUN_ID,
    task_run_id: TASK_RUN_ID,
    reason_code: "tool_failed",
    message: "Tool failed"
  },
  {
    type: "tasks.skipped",
    test_run_id: RUN_ID,
    task_run_id: null,
    task_run_ids: [TASK_RUN_ID]
  },
  {
    type: "message.validation_failed",
    test_run_id: RUN_ID,
    task_run_id: TASK_RUN_ID,
    message_id: "message-1",
    attempt: 1,
    reason: "Invalid tool call"
  },
  {
    type: "message.appended",
    test_run_id: RUN_ID,
    task_run_id: TASK_RUN_ID,
    message: {
      role: "system",
      message_id: "message-2",
      content: [{ type: "text", text: "System message" }]
    }
  }
];

describe("parseStoredRunEvent", () => {
  it("accepts an event that satisfies the generated contract", () => {
    const event = parseStoredRunEvent(JSON.stringify(lifecycleEvent()));
    expect(event.event.type).toBe("run.started");
  });

  it.each(validEventBranches)("accepts the $type event branch", (event) => {
    expect(
      parseStoredRunEvent(JSON.stringify(storedRunEvent(event))).event.type
    ).toBe(event.type);
  });

  it.each([
    ["event id", { ...lifecycleEvent(), event_id: "not-a-uuid" }],
    ["date-time", { ...lifecycleEvent(), occurred_at: "now" }],
    ["sequence", { ...lifecycleEvent(), sequence: 0 }],
    ["discriminator", storedRunEvent({ test_run_id: RUN_ID, task_run_id: null })],
    ["unknown discriminator", storedRunEvent({ type: "unknown", test_run_id: RUN_ID, task_run_id: null })],
    ["event branch", storedRunEvent({ type: "cycle.started", test_run_id: RUN_ID, task_run_id: null })],
    ["envelope additional property", { ...lifecycleEvent(), unexpected: true }],
    ["event additional property", storedRunEvent({
      type: "run.started",
      test_run_id: RUN_ID,
      task_run_id: null,
      unexpected: true
    })]
  ])("rejects an invalid %s", (_name, value) => {
    expect(() => parseStoredRunEvent(JSON.stringify(value))).toThrow(
      "SSE event does not satisfy the OpenAPI contract"
    );
  });
});

describe("run event stream boundary", () => {
  it("falls back to sequence zero when REST history loading fails", async () => {
    const source: RunEventSource = { close: vi.fn(), onmessage: null };
    const createEventSource = vi.fn(() => source);
    const started = await startRunEventStream(
      RUN_ID,
      { onEvent: vi.fn(), onTerminal: vi.fn(), onContractError: vi.fn() },
      () => Promise.reject(new TypeError("offline")),
      createEventSource
    );
    expect(started.events).toEqual([]);
    expect(started.source).toBe(source);
    expect(createEventSource).toHaveBeenCalledWith(
      `/api/runs/${RUN_ID}/stream?after=0`
    );
  });

  it("does not reconnect after restored history is already terminal", async () => {
    const terminalEvent = parseStoredRunEvent(
      JSON.stringify(lifecycleEvent("run.finished"))
    );
    const createEventSource = vi.fn();
    const started = await startRunEventStream(
      RUN_ID,
      { onEvent: vi.fn(), onTerminal: vi.fn(), onContractError: vi.fn() },
      () => Promise.resolve([terminalEvent]),
      createEventSource
    );
    expect(started).toEqual({ events: [terminalEvent], source: null });
    expect(createEventSource).not.toHaveBeenCalled();
  });

  it("leaves network reconnection to the browser for non-terminal events", () => {
    const source: RunEventSource = { close: vi.fn(), onmessage: null };
    const onEvent = vi.fn();
    const onTerminal = vi.fn();

    openRunEventStream(
      RUN_ID,
      0,
      { onEvent, onTerminal, onContractError: vi.fn() },
      () => source
    );

    expect("onerror" in source).toBe(false);
    source.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify(lifecycleEvent("run.started"))
    }));
    expect(onEvent).toHaveBeenCalledOnce();
    expect(onTerminal).not.toHaveBeenCalled();
    expect(source.close).not.toHaveBeenCalled();
  });

  it("closes and refreshes when a terminal event arrives", () => {
    const source: RunEventSource = { close: vi.fn(), onmessage: null };
    const createEventSource = vi.fn(() => source);
    const onEvent = vi.fn();
    const onTerminal = vi.fn();

    openRunEventStream(
      RUN_ID,
      4,
      { onEvent, onTerminal, onContractError: vi.fn() },
      createEventSource
    );

    expect(createEventSource).toHaveBeenCalledWith(
      `/api/runs/${RUN_ID}/stream?after=4`
    );
    source.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify(lifecycleEvent("run.finished"))
    }));
    expect(onEvent).toHaveBeenCalledOnce();
    expect(onTerminal).toHaveBeenCalledOnce();
    expect(source.close).toHaveBeenCalledOnce();
  });

  it("closes and reports a contract violation without publishing the event", () => {
    const source: RunEventSource = { close: vi.fn(), onmessage: null };
    const onEvent = vi.fn();
    const onContractError = vi.fn();
    openRunEventStream(
      RUN_ID,
      0,
      { onEvent, onTerminal: vi.fn(), onContractError },
      () => source
    );

    source.onmessage?.(new MessageEvent("message", { data: "{}" }));
    expect(onEvent).not.toHaveBeenCalled();
    expect(onContractError).toHaveBeenCalledOnce();
    expect(source.close).toHaveBeenCalledOnce();
  });

  it("replaces a duplicate sequence instead of duplicating the timeline", () => {
    const first = parseStoredRunEvent(JSON.stringify(lifecycleEvent("run.started")));
    const replacement = parseStoredRunEvent(JSON.stringify(lifecycleEvent("run.finished")));
    expect(mergeStoredRunEvent([first], replacement)).toEqual([replacement]);
  });
});
