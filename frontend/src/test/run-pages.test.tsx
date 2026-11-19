import { transferableAbortController } from "node:util";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { TestRunReport, TestRunVerdict } from "../api/contracts";
import type { RunEventSource } from "../api/runEvents";
import RunPage from "../pages/RunPage";
import ReportPage from "../pages/ReportPage";
import HistoryPage from "../pages/HistoryPage";
import CasesPage from "../pages/CasesPage";
import { useRunReport } from "../api/useRunReport";
import { PlanningProgressProvider } from "../api/usePlanningProgress";
import { EventTimeline, RunResources, taskMonitor } from "../runPresentation";
import { caseId, imageId, runEvent, runId, runReport, taskRunId } from "./run-fixtures";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  const { default: createFetchClient } = await import("openapi-fetch");
  const { default: createQueryClient } = await import("openapi-react-query");
  const apiFetch = createFetchClient<import("../api/schema").paths>({ baseUrl: "http://localhost", fetch: (request) => fetch(request) });
  return { ...original, apiFetch, $api: createQueryClient(apiFetch) };
});

let report: TestRunReport;
let queryClient: QueryClient;
let sources: TestEventSource[];
let requests: URL[];
let textHeights: Map<string, number>;
let resizeCallbacks: Set<() => void>;
class TestEventSource implements RunEventSource {
  onmessage: RunEventSource["onmessage"] = null;
  onopen: RunEventSource["onopen"] = null;
  onerror: RunEventSource["onerror"] = null;
  close = vi.fn();
  constructor(public url: string) { sources.push(this); }
}

beforeEach(() => {
  report = runReport(); sources = []; requests = [];
  textHeights = new Map(); resizeCallbacks = new Set();
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.stubGlobal("AbortController", class { constructor() { return transferableAbortController(); } });
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: () => void) {}
    observe() { resizeCallbacks.add(this.callback); }
    unobserve() {}
    disconnect() { resizeCallbacks.delete(this.callback); }
  });
  // jsdom has no text layout; provide measured dimensions at the browser boundary.
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("message-text") ? 200 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("message-text") ? textHeights.get(this.textContent ?? "") ?? (this.textContent ?? "").split("\n").length * 20 : 0;
  });
  const computedStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
    const style = computedStyle(element);
    return element.classList.contains("message-text") ? new Proxy(style, {
      get: (target, property) => property === "lineHeight" ? "20px" : Reflect.get(target, property)
    }) : style;
  });
  vi.stubGlobal("matchMedia", vi.fn((query) => ({ matches: false, media: query, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} })));
  vi.stubGlobal("EventSource", TestEventSource);
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const url = new URL(request.url); requests.push(url);
    if (request.method === "POST" && url.pathname.endsWith("/cancel")) return new Response(null, { status: 202 });
    if (url.pathname === "/api/test-cases" || url.pathname === "/api/runs/test-cases") return Response.json({ items: [], total: 0 });
    if (url.pathname === "/api/runs/statistics") return Response.json({ run_count: 24, verdict_counts: { PASS: 20, FAIL: 2, BLOCKED: 1, CANCELLED: 1 }, average_duration_seconds: 36, daily: [] });
    if (url.pathname === "/api/runs") return Response.json({ items: [{ ...report.detail.run, test_case_id: caseId, test_case_name: "设置页面验证", test_case_archived: report.test_case_archived, task_count: 1, task_status_counts: { passed: 1 }, image_count: 1 }], total: 24 });
    return reportResponse(url, report);
  }));
});
function reportResponse(url: URL, snapshot: TestRunReport): Response {
  const after = Number(url.searchParams.get("events_after") ?? 0);
  return Response.json({ ...snapshot, events: snapshot.events.filter((stored) => stored.sequence > after) });
}
afterEach(async () => {
  cleanup(); queryClient.clear();
  await act(async () => { message.destroy(); await new Promise((resolve) => setTimeout(resolve, 0)); });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function simulateImageLoading(fails: (source: string) => boolean) {
  const nativeSourceSetter = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src")!.set!;
  vi.spyOn(HTMLImageElement.prototype, "src", "set").mockImplementation(function (this: HTMLImageElement, source: string) {
    nativeSourceSetter.call(this, source);
    queueMicrotask(() => this.dispatchEvent(new Event(fails(source) ? "error" : "load")));
  });
}

it.each(["execution", "report"])("shows unavailable images in thumbnails and the %s page preview", async (page) => {
  simulateImageLoading((source) => source.includes("/api/artifacts/"));
  renderPage(`/runs/${runId}${page === "report" ? "/report" : ""}`);
  await screen.findByText("设置页面验证");
  if (page === "report") fireEvent.click(document.querySelector("details.timeline-block > summary")!);
  const thumbnails = screen.getAllByRole("img").filter((image) =>
    image.getAttribute("alt") === `图片 ${imageId}`);
  expect(thumbnails).toHaveLength(page === "report" ? 2 : 1);
  await waitFor(() => thumbnails.forEach((image) => expect(image.getAttribute("src")).toMatch(/^data:image\/svg\+xml/)));
  fireEvent.click(thumbnails[page === "report" ? 1 : 0]!);
  await waitFor(() => {
    const preview = document.querySelector<HTMLImageElement>(".ant-image-preview-img");
    expect(preview).not.toBeNull();
    expect(preview!.getAttribute("src")).toMatch(/^data:image\/svg\+xml/);
  });
});

it("updates context, active resources and tool counts from the task event stream", async () => {
  report = runReport(null);
  report.detail.environment.capability_catalog.resource_catalog.resources.push({
    source_id: "local", uri: "file:///guide", name: "Guide", is_skill_manifest: false
  });
  renderPage(`/runs/${runId}`);
  await screen.findByText("等待首次模型请求");
  await waitFor(() => expect(sources[0]).toBeDefined());
  const emit = (sequence: number, event: Parameters<typeof runEvent>[1]) => act(() => {
    sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(runEvent(sequence, event)) }));
  });
  const ids = { test_run_id: runId, task_run_id: taskRunId };
  emit(2, { ...ids, type: "context.updated", context: { input_budget_tokens: 1000,
    tokens: { system_prompt: 10, user_messages: 20, tool_definitions: 40, skill_body: 0, tool_calls: 0, tool_return_text: 0, tool_return_nontext: 0, assistant_messages: 0 },
    active_resources: { local: ["file:///guide"] } } });
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "70");
  expect(screen.getByText("激活")).toBeInTheDocument();
  emit(3, { ...ids, type: "tool.started", cycle_count: 1, tool_name: "fixture_inspect", call_id: "call" });
  expect(screen.getByText(/发起 1 次 · 成功 0 次/)).toBeInTheDocument();
  emit(4, { ...ids, type: "tool.succeeded", cycle_count: 1, tool_name: "fixture_inspect", call_id: "call" });
  expect(screen.getByText(/发起 1 次 · 成功 1 次/)).toBeInTheDocument();
  emit(5, { ...ids, type: "task.started", task_run_id: imageId });
  expect(screen.getByText("等待首次模型请求")).toBeInTheDocument();
  expect(screen.queryByText("激活")).toBeNull();
  expect(screen.getByText(/发起 0 次 · 成功 0 次/)).toBeInTheDocument();
});

function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }

it("shows task-specific tool counts and node durations in the finished report", async () => {
  report.task_statistics = { [taskRunId]: {
    tools: { fixture_inspect: { calls: 3, successes: 2 } },
    nodes: { model: { elapsed_seconds: 9.125, task_percent: 25.347 } }
  } };
  renderPage(`/runs/${runId}/report`);
  expect(await screen.findByText("fixture_inspect · 发起 3 次 · 成功 2 次")).toBeInTheDocument();
  expect(screen.getByText("model · 9.125 秒 · 25.3%")).toBeInTheDocument();
});

it("shows frozen Skill resource addresses and templates in run resources", () => {
  report.detail.environment.capability_catalog.resource_catalog = {
    local_root: "D:/skills",
    resources: [{ source_id: "mcp:docs", uri: "skill://focus/SKILL.md", name: "Focus check", description: "Focus instructions", mime_type: "text/markdown", is_skill_manifest: false }],
    templates: [{ source_id: "mcp:docs", uri_template: "skill://focus/{path*}", name: "Attachments", description: null, mime_type: null }],
  };
  report.detail.environment.capability_catalog.columns.push({ id: "mcp:docs", selected: true });
  render(<RunResources report={report} />);
  expect(screen.getByText("Focus check")).toBeInTheDocument();
  expect(screen.getByText("skill://focus/SKILL.md")).toBeInTheDocument();
  expect(screen.getByText("skill://focus/{path*}")).toBeInTheDocument();
  expect(screen.getByText("D:/skills")).toBeInTheDocument();
});
it("shows every execution of a repeated task in the report", async () => {
  const first = report.detail.task_runs[0]!;
  report.detail.task_runs.push({ ...first, id: "55555555-5555-4555-8555-555555555555", sequence: 2,
    result: { reason_code: "completed", summary: "第二次执行的独立观察", memory: "第二轮节点" } });
  renderPage(`/runs/${runId}/report`);
  await screen.findByText("第二次执行的独立观察");
  expect(screen.getByText("已根据截图检查版本号")).toBeInTheDocument();
  expect(screen.getByText("第二轮节点")).toBeInTheDocument();
  expect(document.querySelectorAll("details.timeline-block")).toHaveLength(2);
  expect([...document.querySelectorAll<HTMLDetailsElement>("details.timeline-block")].every((task) => !task.open)).toBe(true);
});

function renderPage(path: string, history = false) {
  render(<QueryClientProvider client={queryClient}><PlanningProgressProvider><MemoryRouter initialEntries={[path]}><Location /><Routes>
    <Route path="/runs" element={<HistoryPage />} />
    <Route path="/" element={<CasesPage />} />
    <Route path="/runs/:runId" element={<RunPage />} />
    <Route path="/runs/:runId/report" element={history ? <HistoryPage /> : <ReportPage />} />
    <Route path="/cases/:caseId/plan" element={<div>确认最新计划</div>} />
  </Routes></MemoryRouter></PlanningProgressProvider></QueryClientProvider>);
}

it.each(["execution", "report"])("shows Goal facts without a failure limit snapshot in the %s page", async (page) => {
  const goalTaskId = "55555555-5555-4555-8555-555555555555";
  report.detail.run.verdict = "BLOCKED";
  report.detail.test_plan.content.tasks.push({ test_task_id: goalTaskId, definition: {
    task_no: 2, type: "goal", title: "遍历结束", goal: "确认节点已全部检查", success_criteria: ["没有待检查节点"], jump_back_to_task_no: 1
  } });
  report.detail.task_runs.push({ ...report.detail.task_runs[0]!, id: goalTaskId, test_task_id: goalTaskId,
    sequence: 2, status: "failed", result: { reason_code: "goal_failure_limit", summary: "仍有未处理节点", memory: "尚有一个节点" } });
  renderPage(`/runs/${runId}${page === "report" ? "/report" : ""}`);
  await screen.findByText("遍历结束");
  expect(screen.queryAllByText(/累计失败上限/)).toHaveLength(0);
  expect(screen.getByText(/未满足时回到任务 1/)).toBeInTheDocument();
  expect(screen.getByText("仍有未处理节点")).toBeInTheDocument();
  expect(screen.getByText("尚有一个节点")).toBeInTheDocument();
  if (page === "execution") expect(screen.getByText(/累计失败 1/)).toBeInTheDocument();
  else expect(screen.getByText(/goal_failure_limit/)).toBeInTheDocument();
});

it("shows undefined prompt fingerprints without replacing defined fingerprints", () => {
  report.detail.environment.prompt_fingerprints_by_task_type.act = null;
  report.detail.test_plan.planning_context.planning_prompt_fingerprint = null;
  render(<RunResources report={report} />);
  expect(screen.getAllByText("未定义")).toHaveLength(2);
  expect(screen.getByText("judge-v1")).toBeInTheDocument();
});

it.each<TestRunVerdict>(["PASS", "FAIL", "BLOCKED", "CANCELLED"])("shows the authoritative %s result and saved images", async (verdict) => {
  report = runReport(verdict);
  renderPage(`/runs/${runId}/report`);
  await screen.findByText("设置页面验证");
  expect(document.querySelector(`.report-summary .status-${verdict.toLowerCase()}`)).toBeInTheDocument();
  expect(screen.getByText("截图前观察")).toBeInTheDocument();
  expect(screen.getByText("截图后观察")).toBeInTheDocument();
  expect(screen.getByText("act-v1")).toBeInTheDocument();
  expect(screen.getByText("judge-v1")).toBeInTheDocument();
  expect(screen.queryByText("判定证据")).not.toBeInTheDocument();
  expect(sources).toHaveLength(0);
});

it.each(["execution", "report"])("shows task memory as plain text in the %s page", async (page) => {
  const memory = "A: TV-X1\n<img src=x onerror=alert(1)>";
  report.detail.task_runs[0]!.result!.memory = memory;
  renderPage(`/runs/${runId}${page === "report" ? "/report" : ""}`);
  expect(await screen.findByText("跨任务记忆")).toBeInTheDocument();
  expect(screen.getByText(memory, { normalizer: (text) => text })).toBeInTheDocument();
  expect(document.querySelector(".task-memory img")).toBeNull();
});

it.each(["execution", "report"])("omits an empty task memory section in the %s page", async (page) => {
  renderPage(`/runs/${runId}${page === "report" ? "/report" : ""}`);
  await screen.findByText("设置页面验证");
  expect(screen.queryByText("跨任务记忆")).not.toBeInTheDocument();
});

it.each(["execution", "report"])("renders saved finish receipts independently of task results in the %s page", async (page) => {
  const receipt = "passed: Observed A\n\nSubmitted memory:\nA: <original>\nfirmware V2.3.7";
  report.detail.task_runs[0]!.result!.memory = "Task result memory";
  report.events = [runEvent(1, { type: "message.appended", test_run_id: runId, task_run_id: taskRunId, message: {
    role: "tool", message_id: "finish-receipt", tool_call_id: "finish-1", name: "finish_task", status: "success",
    content: [{ type: "text", text: receipt }],
  } })];
  renderPage(`/runs/${runId}${page === "report" ? "/report" : ""}`);
  expect(await screen.findByText(receipt, { normalizer: (text) => text })).toBeInTheDocument();
  expect(screen.getByText("Task result memory")).toBeInTheDocument();
  expect(document.querySelector(".message-text")?.textContent).toBe(receipt);
  expect(document.querySelector(".message-text img")).toBeNull();
});

it("updates memory after task.finished fetches the persisted task result", async () => {
  report = runReport(null);
  renderPage(`/runs/${runId}`);
  await waitFor(() => expect(sources).toHaveLength(1));
  expect(screen.queryByText("跨任务记忆")).not.toBeInTheDocument();
  report.detail.task_runs[0] = runReport("PASS").detail.task_runs[0]!;
  report.detail.task_runs[0]!.result!.memory = "A: firmware V2.3.7";
  const finished = runEvent(2, { type: "task.finished", test_run_id: runId, task_run_id: taskRunId });
  report.events.push(finished);
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(finished) })));
  expect(await screen.findByText("A: firmware V2.3.7")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /终止/ })).toBeEnabled();
  expect(requests.some((url) => url.pathname === `/api/runs/${runId}` && url.searchParams.get("events_after") === "1")).toBe(true);
});

it.each(["act", "judge"] as const)("shows the configured %s budget in the report", async (type) => {
  report.detail.test_plan.content.tasks[0]!.definition.type = type;
  report.detail.environment.execution.max_cycles_by_task_type.act = 37;
  report.detail.environment.execution.max_cycles_by_task_type.judge = 14;
  renderPage(`/runs/${runId}/report`);
  await screen.findByText("设置页面验证");
  expect(screen.getByText(`completed · 2 / ${type === "act" ? 37 : 14} 轮`)).toBeInTheDocument();
});

it("keeps archived reports readable and exportable but disables rerun", async () => {
  report.test_case_archived = true;
  renderPage(`/runs/${runId}/report`);
  await screen.findByText("用例已归档");
  expect(screen.queryByText("判定证据")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /无法重新执行/ })).toBeDisabled();
  expect(screen.queryByRole("link", { name: /重新执行/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /JSON 报告/ })).toBeEnabled();
});

it("uses the history case query to filter archived runs", async () => {
  report.test_case_archived = true;
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (request) => {
    const url = new URL((request as Request).url);
    if (url.pathname === "/api/runs/test-cases") {
      requests.push(url);
      return Response.json({ items: [{ id: caseId, content: { name: "设置页面验证", source_text: "原始描述" }, is_archived: true, created_at: report.detail.run.created_at }], total: 1 });
    }
    return originalFetch(request);
  });
  renderPage("/runs", true);
  await screen.findByText("已归档");
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "筛选用例" }));
  fireEvent.click(await screen.findByText("设置页面验证 · 已归档"));
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/runs" && url.searchParams.get("test_case_id") === caseId)).toBe(true));
  expect(requests.some((url) => url.pathname === "/api/test-cases")).toBe(false);
});

it("keeps cancelled unstarted tasks distinct from fail-fast skipped tasks", async () => {
  report = runReport("CANCELLED");
  report.detail.test_plan.content.tasks.push({ test_task_id: caseId, definition: { ...report.detail.test_plan.content.tasks[0]!.definition, task_no: 2, title: "后续任务" } });
  renderPage(`/runs/${runId}/report`);
  expect(await screen.findByText("未执行")).toBeInTheDocument();
  expect(screen.queryByText("跳过")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: /重新执行/ }));
  expect(await screen.findByText("确认最新计划")).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent(`/cases/${caseId}/plan`);
});

it.each(["act", "judge"] as const)("restores history and merges live cycles with the configured %s budget", async (type) => {
  report = runReport(null);
  report.detail.test_plan.content.tasks[0]!.definition.type = type;
  report.detail.environment.execution.max_cycles_by_task_type.act = 37;
  report.detail.environment.execution.max_cycles_by_task_type.judge = 14;
  const limit = type === "act" ? 37 : 14;
  report.detail.test_plan.content.tasks.push({ test_task_id: caseId, definition: { ...report.detail.test_plan.content.tasks[0]!.definition, task_no: 2, title: "后续任务" } });
  renderPage(`/runs/${runId}`);
  await waitFor(() => expect(sources).toHaveLength(1));
  expect(screen.getByText("待执行")).toBeInTheDocument();
  expect(screen.getByText(`— / ${limit} 轮`)).toBeInTheDocument();
  const source = sources[0]!;
  expect(source.url).toContain("after=1");
  act(() => source.onmessage?.(new MessageEvent("message", { data: JSON.stringify(runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 3 })) })));
  expect(await screen.findByText(`3 / ${limit} 轮`)).toBeInTheDocument();
  act(() => source.onerror?.(new Event("error")));
  expect(screen.getByText("正在重连")).toBeInTheDocument();
  act(() => source.onopen?.(new Event("open")));
  expect(screen.getByText("实时连接")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /终止/ }));
  expect(await screen.findByRole("button", { name: /等待取消生效/ })).toBeDisabled();
  expect(screen.queryByText("查看执行记录")).not.toBeInTheDocument();
  report = runReport("CANCELLED");
  act(() => source.onmessage?.(new MessageEvent("message", { data: JSON.stringify(runEvent(3, { type: "run.cancelled", test_run_id: runId, task_run_id: null })) })));
  await screen.findByText("查看执行记录");
  expect(source.close).toHaveBeenCalled();
  expect(screen.getByText("第 3 轮")).toBeInTheDocument();
  expect(screen.getByText("截图前观察")).toBeInTheDocument();
});

it("stops malformed live events without discarding persisted history", async () => {
  report = runReport(null);
  renderPage(`/runs/${runId}`);
  await waitFor(() => expect(sources).toHaveLength(1));
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: "{}" })));
  expect(await screen.findByText("事件流违反 OpenAPI 契约，实时更新已停止")).toBeInTheDocument();
  expect(screen.getByText("截图前观察")).toBeInTheDocument();
  expect(sources[0]!.close).toHaveBeenCalled();
});

it("uses whole-filter counts and retains pagination when opening and closing a report", async () => {
  renderPage("/runs?from=2026-09-01&to=2026-09-30&page=2&verdict=PASS", true);
  const link = await screen.findByRole("link", { name: runId.slice(0, 8) });
  expect(requests.find((url) => url.pathname === "/api/runs")?.searchParams.get("offset")).toBe("8");
  expect(requests.find((url) => url.pathname.endsWith("statistics"))?.searchParams.has("verdict")).toBe(false);
  expect(requests.find((url) => url.pathname.endsWith("statistics"))?.searchParams.get("time_zone")).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
  fireEvent.click(link);
  await screen.findByRole("dialog");
  await screen.findByText("执行概要");
  const taskDetails = document.querySelector<HTMLDetailsElement>("details.timeline-block")!;
  expect(taskDetails.open).toBe(false);
  fireEvent.click(taskDetails.querySelector("summary")!);
  expect(taskDetails.open).toBe(true);
  expect(screen.getByTestId("location")).toHaveTextContent("page=2");
  fireEvent.click(screen.getByRole("button", { name: /close/i }));
  await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/runs?from=2026-09-01&to=2026-09-30&page=2&verdict=PASS"));
  fireEvent.click(await screen.findByRole("link", { name: runId.slice(0, 8) }));
  await screen.findByRole("dialog");
  await screen.findByText("执行概要");
  expect(document.querySelector<HTMLDetailsElement>("details.timeline-block")!.open).toBe(false);
});

it.each([false, true])("previews long text with its original character count when live controls=%s", (controls) => {
  const text = "首段 😀 \r\n第二行\n第三行\n第四行";
  const stored = runEvent(1, { type: "message.appended", test_run_id: runId, task_run_id: taskRunId,
    message: { role: "human", message_id: "long-text", content: [{ type: "text", text }, { type: "text", text: "短消息" }] } });
  const page = render(<EventTimeline events={[stored]} controls={controls} />);
  expect(screen.getByText(`共 ${Array.from(text).length} 字`)).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "展开" })).toHaveLength(1);
  const preview = screen.getByText(text, { exact: true, normalizer: (value) => value });
  expect(preview).toHaveClass("message-preview");
  fireEvent.click(screen.getByRole("button", { name: "展开" }));
  expect(preview).not.toHaveClass("message-preview");
  page.rerender(<EventTimeline events={[stored, runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 2 })]} controls={controls} />);
  expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
  textHeights.set(text, 40);
  act(() => resizeCallbacks.forEach((callback) => callback()));
  expect(screen.queryByRole("button", { name: "收起" })).not.toBeInTheDocument();
  expect(screen.queryByText(/^共 \d+ 字$/)).not.toBeInTheDocument();
  textHeights.set(text, 100);
  act(() => resizeCallbacks.forEach((callback) => callback()));
  expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  expect(preview).toHaveClass("message-preview");
  expect(screen.getByText(`共 ${Array.from(text).length} 字`)).toBeInTheDocument();
});

it("shows short message text in full without an expansion control", () => {
  render(<EventTimeline events={report.events} controls />);
  expect(screen.queryByRole("button", { name: "展开" })).not.toBeInTheDocument();
  expect(screen.queryByText(/^共 \d+ 字$/)).not.toBeInTheDocument();
});

it("keeps tool call text and identifiers together in the event body", () => {
  const stored = runEvent(7, { type: "tool.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 1, tool_name: "finish_task", call_id: "finish-blocked" });
  const page = render(<EventTimeline events={[stored]} />);
  const row = page.container.querySelector(".event-row")!;
  expect(row.children).toHaveLength(3);
  expect(row.children[2]).toHaveTextContent("finish_task finish-blocked");
});

it("collapses each message block independently and retains expansion when events arrive", () => {
  const stored = runEvent(1, { type: "message.appended", test_run_id: runId, task_run_id: taskRunId,
    message: { role: "tool", message_id: "mixed-message", tool_call_id: "call-mixed", name: "fixture_inspect", status: "success",
      content: [{ type: "text", text: "第一段\n第二行\n第三行\n第四行" }, { type: "image_artifact", artifact_id: imageId },
        { type: "text", text: "另一段" }, { type: "image_artifact", artifact_id: imageId }] } });
  const page = render(<EventTimeline events={[stored]} controls />);
  expect(page.container.querySelectorAll(".message-block")).toHaveLength(4);
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "回到最新" })).toBeInTheDocument();
  const imageBlocks = page.container.querySelectorAll<HTMLDetailsElement>("details.message-block");
  expect(imageBlocks).toHaveLength(2);
  expect(imageBlocks[0]!.open).toBe(false);
  fireEvent.click(imageBlocks[0]!.querySelector("summary")!);
  expect(imageBlocks[0]!.open).toBe(true);
  page.rerender(<EventTimeline events={[stored, runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 2 })]} controls />);
  expect(page.container.querySelector<HTMLDetailsElement>("details.message-block")!.open).toBe(true);
});

it("resumes following when the user reaches bottom before the queued scroll callback", () => {
  const events = [runEvent(1, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 1 })];
  const page = render(<EventTimeline events={events} controls />);
  const scroller = page.container.querySelector<HTMLDivElement>(".event-scroll")!;
  let scrollTop = 0;
  let scrollHeight = 1000;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => 200 }, scrollHeight: { get: () => scrollHeight },
    scrollTop: { get: () => scrollTop, set: (value: number) => { scrollTop = Math.max(0, Math.min(value, scrollHeight - 200)); } }
  });
  const append = () => {
    events.push(runEvent(events.length + 1, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: events.length + 1 }));
    page.rerender(<EventTimeline events={[...events]} controls />);
  };
  append();
  expect(scrollTop).toBe(800);
  fireEvent.wheel(scroller, { deltaY: -200 }); scrollTop = 600; fireEvent.scroll(scroller);
  scrollHeight = 1100; append();
  expect(scrollTop).toBe(600);
  fireEvent.keyDown(scroller, { key: "End" }); scrollTop = 900;
  scrollHeight = 1200; append();
  expect(scrollTop).toBe(1000);
  fireEvent.scroll(scroller);
  scrollHeight = 1300; append();
  expect(scrollTop).toBe(1100);
  fireEvent.pointerDown(scroller); scrollTop = 900;
  scrollHeight = 1400; append();
  expect(scrollTop).toBe(900);
});

it.each(["pointer", "keyboard"])("keeps following when a %s-collapsed message clamps the scroll position", (activation) => {
  const event = runEvent(1, { type: "message.appended", test_run_id: runId, task_run_id: taskRunId,
    message: { role: "tool", message_id: "long-message", tool_call_id: "call-long", name: "fixture_inspect", status: "success",
      content: [{ type: "text", text: "长观察\n".repeat(20) }] } });
  const page = render(<EventTimeline events={[event]} controls />);
  const scroller = page.container.querySelector<HTMLDivElement>(".event-scroll")!;
  let scrollTop = 0;
  let scrollHeight = 1200;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => 200 }, scrollHeight: { get: () => scrollHeight },
    scrollTop: { get: () => scrollTop, set: (value: number) => { scrollTop = Math.max(0, Math.min(value, scrollHeight - 200)); } }
  });
  fireEvent.click(screen.getByRole("button", { name: "展开" }));
  page.rerender(<EventTimeline events={[event]} controls />);
  expect(scrollTop).toBe(1000);
  if (activation === "keyboard") {
    fireEvent.pointerDown(scroller);
    fireEvent.keyDown(screen.getByRole("button", { name: "收起" }), { key: "Enter" });
  } else fireEvent.pointerDown(screen.getByRole("button", { name: "收起" }));
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  scrollHeight = 1000; scrollTop = 800; fireEvent.scroll(scroller);
  scrollHeight = 1200;
  page.rerender(<EventTimeline events={[event, runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 2 })]} controls />);
  expect(scrollTop).toBe(1000);
});

it("distinguishes unmet Goal conditions from product failures in successful history summaries", async () => {
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (request) => {
    if (new URL((request as Request).url).pathname === "/api/runs") {
      return Response.json({ items: [{ ...report.detail.run, test_case_id: caseId, test_case_name: "设置页面验证",
        test_case_archived: false, task_count: 3, task_status_counts: { passed: 12, failed: 9 }, image_count: 2 }], total: 1 });
    }
    return originalFetch(request);
  });
  renderPage("/runs");
  expect(await screen.findByText(/失败\/条件未满足 9/)).toBeInTheDocument();
  expect(document.querySelector(".status-pass")).toBeInTheDocument();
});

it("opens a report directly from its address", async () => {
  renderPage(`/runs/${runId}/report?from=2026-09-01&to=2026-09-30`, true);
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(await screen.findByText("执行概要")).toBeInTheDocument();
});

it.each(["execution", "modal"])("retries the final report after a terminal refresh failure in %s", async (page) => {
  report = runReport(null);
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  let failReport = false;
  vi.mocked(fetch).mockImplementation(async (request) => {
    if (failReport && new URL((request as Request).url).pathname === `/api/runs/${runId}`) {
      requests.push(new URL((request as Request).url));
      return Response.json({ code: "unavailable", message: "Temporary failure" }, { status: 503 });
    }
    return originalFetch(request);
  });
  renderPage(`/runs/${runId}${page === "modal" ? "/report" : ""}`, page === "modal");
  await waitFor(() => expect(sources).toHaveLength(1));
  failReport = true;
  const terminal = runEvent(2, { type: "run.finished", test_run_id: runId, task_run_id: null });
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(terminal) })));
  expect(await screen.findByText("运行详情刷新失败，保留最近一次读取的记录")).toBeInTheDocument();
  expect(screen.getByText("已收到结束通知，等待服务端最终结果")).toBeInTheDocument();
  expect(sources[0]!.close).toHaveBeenCalled();
  failReport = false;
  report = runReport("FAIL"); report.events.push(terminal);
  fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
  await waitFor(() => expect(screen.queryByText("已收到结束通知，等待服务端最终结果")).not.toBeInTheDocument());
  expect(document.querySelector(".status-fail")).toBeInTheDocument();
  expect(screen.getByText("截图前观察")).toBeInTheDocument();
  expect(sources).toHaveLength(1);
  expect(vi.mocked(fetch).mock.calls.filter(([request]) => (request as Request).method === "POST")).toHaveLength(0);
  expect(requests.filter((url) => url.pathname === `/api/runs/${runId}`).map((url) => url.searchParams.get("events_after"))).toEqual(["0", "1", "1"]);
});

it("avoids the initial onopen refetch and preserves newer SSE events across older HTTP snapshots", async () => {
  report = runReport(null);
  const { result } = renderHook(() => useRunReport(runId), {
    wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  });
  await waitFor(() => expect(sources).toHaveLength(1));
  const reportRequests = () => requests.filter((url) => url.pathname === `/api/runs/${runId}`).length;
  expect(reportRequests()).toBe(1);
  await act(async () => { sources[0]!.onopen?.(new Event("open")); });
  expect(reportRequests()).toBe(1);
  const cycle = runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 3 });
  const newerCycle = runEvent(3, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 4 });
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(cycle) })));
  report.events.push(cycle);
  const olderSnapshot = structuredClone(report);
  let respond: (response: Response) => void = () => { throw new Error("Refresh has not started"); };
  vi.mocked(fetch).mockImplementationOnce(async (request) => {
    requests.push(new URL((request as Request).url));
    return new Promise<Response>((resolve) => { respond = resolve; });
  });
  act(() => {
    sources[0]!.onerror?.(new Event("error"));
    sources[0]!.onopen?.(new Event("open"));
  });
  await waitFor(() => expect(reportRequests()).toBe(2));
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(newerCycle) })));
  await act(async () => { respond(reportResponse(requests.at(-1)!, olderSnapshot)); });
  await waitFor(() => expect(result.current.isFetching).toBe(false));
  expect(result.current.data?.events.map((event) => event.sequence)).toEqual([1, 2]);
  expect(result.current.events.map((event) => event.sequence)).toEqual([1, 2, 3]);
  report.events.push(newerCycle);
  await act(async () => { await result.current.refetch(); });
  expect(reportRequests()).toBe(3);
  expect(result.current.events.map((event) => event.sequence)).toEqual([1, 2, 3]);
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(cycle) })));
  expect(result.current.events.map((event) => event.sequence)).toEqual([1, 2, 3]);
  expect(requests.map((url) => url.searchParams.get("events_after"))).toEqual(["0", "1", "2"]);
});

it("fills missed events on reconnect even when SSE has delivered a newer sequence", async () => {
  report = runReport(null);
  const { result } = renderHook(() => useRunReport(runId), {
    wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  });
  await waitFor(() => expect(sources).toHaveLength(1));
  const call = { test_run_id: runId, task_run_id: taskRunId, cycle_count: 1, tool_name: "fixture_inspect", call_id: "call" };
  const missed = runEvent(2, { type: "tool.started", ...call });
  const received = runEvent(3, { type: "tool.succeeded", ...call });
  report.events.push(missed, received);
  act(() => {
    sources[0]!.onerror?.(new Event("error"));
    sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(received) }));
  });
  expect(result.current.events.map((stored) => stored.sequence)).toEqual([1, 3]);
  act(() => sources[0]!.onopen?.(new Event("open")));
  await waitFor(() => expect(result.current.data?.events.map((stored) => stored.sequence)).toEqual([1, 2, 3]));
  expect(result.current.events.map((stored) => stored.sequence)).toEqual([1, 2, 3]);
  expect(taskMonitor(result.current.events, taskRunId).tools.get("fixture_inspect")).toEqual({ calls: 1, successes: 1 });
  expect(requests.map((url) => url.searchParams.get("events_after"))).toEqual(["0", "1"]);
});

it("cancels an overlapping refresh and commits the newer report without advancing the cancelled cursor", async () => {
  report = runReport(null);
  const { result } = renderHook(() => useRunReport(runId), {
    wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  });
  await waitFor(() => expect(sources).toHaveLength(1));
  const cycle = runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 3 });
  report.events.push(cycle);
  const cancelledRequests: Request[] = [];
  vi.mocked(fetch).mockImplementationOnce(async (input) => {
    const request = input as Request;
    requests.push(new URL(request.url));
    cancelledRequests.push(request);
    return new Promise<Response>((_resolve, reject) => {
      request.signal.addEventListener("abort", () => reject(request.signal.reason), { once: true });
    });
  });
  act(() => { void result.current.refetch(); });
  await waitFor(() => expect(cancelledRequests).toHaveLength(1));
  report = runReport("FAIL");
  report.events.push(cycle, runEvent(3, { type: "run.finished", test_run_id: runId, task_run_id: null }));
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(report.events.at(-1)) })));
  await waitFor(() => expect(result.current.data?.detail.run.verdict).toBe("FAIL"));
  expect(cancelledRequests[0]!.signal.aborted).toBe(true);
  expect(result.current.events.map((stored) => stored.sequence)).toEqual([1, 2, 3]);
  expect(result.current.error).toBeNull();
  expect(requests.map((url) => url.searchParams.get("events_after"))).toEqual(["0", "1", "1"]);
});

it("resumes cached HTTP history on remount and isolates reports by run", async () => {
  report = runReport(null);
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  const first = renderHook(() => useRunReport(runId), { wrapper });
  await waitFor(() => expect(sources).toHaveLength(1));
  const cycle = runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 3 });
  act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(cycle) })));
  expect(first.result.current.events).toHaveLength(2);
  first.unmount();
  report.events.push(cycle);
  const reopened = renderHook(() => useRunReport(runId), { wrapper });
  await waitFor(() => expect(reopened.result.current.data?.events).toHaveLength(2));
  expect(reopened.result.current.events.map((stored) => stored.sequence)).toEqual([1, 2]);
  expect(requests.map((url) => url.searchParams.get("events_after"))).toEqual(["0", "1"]);
  reopened.unmount();

  const otherRunId = "55555555-5555-4555-8555-555555555555";
  report = runReport();
  report.detail.run.id = otherRunId;
  report.detail.task_runs.forEach((task) => { task.test_run_id = otherRunId; });
  report.events.forEach((stored) => { stored.event.test_run_id = otherRunId; });
  const other = renderHook(() => useRunReport(otherRunId), { wrapper });
  await waitFor(() => expect(other.result.current.data?.detail.run.id).toBe(otherRunId));
  expect(other.result.current.events.map((stored) => stored.event.test_run_id)).toEqual([otherRunId]);
  expect(requests.at(-1)?.searchParams.get("events_after")).toBe("0");
  expect(queryClient.getQueryCache().getAll()).toHaveLength(2);
});

it.each(["1.5", "Infinity"])("uses a whole first page for invalid URL page %s on both lists", async (page) => {
  renderPage(`/?page=${page}`);
  await waitFor(() => expect(requests.find((url) => url.pathname === "/api/test-cases")?.searchParams.get("offset")).toBe("0"));
  cleanup(); requests = [];
  renderPage(`/runs?from=2026-02-01&to=2026-02-28&page=${page}`);
  await waitFor(() => expect(requests.find((url) => url.pathname === "/api/runs")?.searchParams.get("offset")).toBe("0"));
});

it("refreshes only on lifecycle changes and reconnection for a multi-task event history", async () => {
  report = runReport(null);
  report.detail.environment.execution.max_cycles_by_task_type.judge = 30;
  report.detail.test_plan.content.tasks.push({ test_task_id: caseId, definition: { ...report.detail.test_plan.content.tasks[0]!.definition, task_no: 2, title: "第二任务" } });
  const transmittedEventCounts: number[] = [];
  const originalFetch = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (request) => {
    const response = await originalFetch(request);
    if (new URL((request as Request).url).pathname === `/api/runs/${runId}`) {
      const increment = await response.clone().json() as TestRunReport;
      transmittedEventCounts.push(increment.events.length);
    }
    return response;
  });
  const { result } = renderHook(() => useRunReport(runId), {
    wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  });
  await waitFor(() => expect(sources).toHaveLength(1));
  const emit = (stored: ReturnType<typeof runEvent>) => {
    report.events.push(stored);
    act(() => sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(stored) })));
  };
  act(() => sources[0]!.onopen?.(new Event("open")));
  for (let sequence = 2; sequence <= 20; sequence++) {
    emit(runEvent(sequence, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: sequence }));
  }
  expect(transmittedEventCounts).toEqual([1]);
  report.detail.task_runs[0] = runReport("PASS").detail.task_runs[0]!;
  report.detail.task_runs[0]!.cycle_count = 20;
  emit(runEvent(21, { type: "task.finished", test_run_id: runId, task_run_id: taskRunId }));
  await waitFor(() => expect(result.current.data?.events).toHaveLength(21));
  report.detail.task_runs.push({ ...runReport(null).detail.task_runs[0]!, id: caseId, test_task_id: caseId });
  emit(runEvent(22, { type: "task.started", test_run_id: runId, task_run_id: caseId }));
  await waitFor(() => expect(result.current.data?.events).toHaveLength(22));
  act(() => { sources[0]!.onerror?.(new Event("error")); sources[0]!.onopen?.(new Event("open")); });
  await waitFor(() => expect(transmittedEventCounts).toEqual([1, 20, 1, 0]));
  await waitFor(() => expect(result.current.isFetching).toBe(false));
  expect(result.current.events.map((event) => event.sequence)).toEqual(Array.from({ length: 22 }, (_, index) => index + 1));
  expect(transmittedEventCounts.reduce((total, count) => total + count, 0)).toBe(22);
  expect(requests.map((url) => url.searchParams.get("events_after"))).toEqual(["0", "1", "21", "22"]);
});

it.each(["2026-02-30", "2026-02-29"])("does not request an invalid URL date range starting %s", async (from) => {
  renderPage(`/runs?from=${from}&to=2026-03-01`);
  expect(await screen.findByText("请选择有效的日期范围，开始日期不得晚于结束日期")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /刷新/ })).toBeDisabled();
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/runs/test-cases")).toBe(true));
  expect(requests.some((url) => url.pathname === "/api/runs" || url.pathname === "/api/runs/statistics")).toBe(false);
});
