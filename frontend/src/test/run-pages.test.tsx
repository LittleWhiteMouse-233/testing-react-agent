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
import { caseId, runEvent, runId, runReport, taskRunId } from "./run-fixtures";

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
class TestEventSource implements RunEventSource {
  onmessage: RunEventSource["onmessage"] = null;
  onopen: RunEventSource["onopen"] = null;
  onerror: RunEventSource["onerror"] = null;
  close = vi.fn();
  constructor(public url: string) { sources.push(this); }
}

beforeEach(() => {
  report = runReport(); sources = []; requests = [];
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.stubGlobal("AbortController", class { constructor() { return transferableAbortController(); } });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", vi.fn((query) => ({ matches: false, media: query, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} })));
  vi.stubGlobal("EventSource", TestEventSource);
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const url = new URL(request.url); requests.push(url);
    if (request.method === "POST" && url.pathname.endsWith("/cancel")) return new Response(null, { status: 202 });
    if (url.pathname === "/api/test-cases") return Response.json({ items: [], total: 0 });
    if (url.pathname === "/api/runs/statistics") return Response.json({ run_count: 24, verdict_counts: { PASS: 20, FAIL: 2, BLOCKED: 1, CANCELLED: 1 }, average_duration_seconds: 36, daily: [] });
    if (url.pathname === "/api/runs") return Response.json({ items: [{ ...report.detail.run, test_case_id: caseId, test_case_name: "设置页面验证", task_count: 1, task_status_counts: { passed: 1 }, screenshot_count: 1 }], total: 24 });
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
  vi.unstubAllGlobals();
});

function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }
function renderPage(path: string, history = false) {
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}><Location /><Routes>
    <Route path="/runs" element={<HistoryPage />} />
    <Route path="/" element={<CasesPage />} />
    <Route path="/runs/:runId" element={<RunPage />} />
    <Route path="/runs/:runId/report" element={history ? <HistoryPage /> : <ReportPage />} />
    <Route path="/cases/:caseId/plan" element={<div>确认最新计划</div>} />
  </Routes></MemoryRouter></QueryClientProvider>);
}

it.each<TestRunVerdict>(["PASS", "FAIL", "BLOCKED", "CANCELLED"])("shows the authoritative %s result and real evidence", async (verdict) => {
  report = runReport(verdict);
  renderPage(`/runs/${runId}/report`);
  await screen.findByText("设置页面验证");
  expect(document.querySelector(`.report-summary .status-${verdict.toLowerCase()}`)).toBeInTheDocument();
  expect(screen.getByText("截图前观察")).toBeInTheDocument();
  expect(screen.getByText("截图后观察")).toBeInTheDocument();
  expect(screen.getByText("判定证据")).toBeInTheDocument();
  expect(sources).toHaveLength(0);
});

it("keeps cancelled unstarted tasks distinct from fail-fast skipped tasks", async () => {
  report = runReport("CANCELLED");
  report.detail.test_plan.content.tasks.push({ test_task_id: caseId, definition: { ...report.detail.test_plan.content.tasks[0]!.definition, title: "后续任务" } });
  renderPage(`/runs/${runId}/report`);
  expect(await screen.findByText("未执行")).toBeInTheDocument();
  expect(screen.queryByText("跳过")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: /重新执行/ }));
  expect(await screen.findByText("确认最新计划")).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent(`/cases/${caseId}/plan`);
});

it("restores history, merges live cycles, reports reconnection and waits for cancellation to finish", async () => {
  report = runReport(null);
  report.detail.test_plan.content.tasks.push({ test_task_id: caseId, definition: { ...report.detail.test_plan.content.tasks[0]!.definition, title: "后续任务" } });
  renderPage(`/runs/${runId}`);
  await waitFor(() => expect(sources).toHaveLength(1));
  expect(screen.getByText("待执行")).toBeInTheDocument();
  expect(screen.getByText("— / 10 轮")).toBeInTheDocument();
  const source = sources[0]!;
  expect(source.url).toContain("after=1");
  act(() => source.onmessage?.(new MessageEvent("message", { data: JSON.stringify(runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 3 })) })));
  expect(await screen.findByText("3 / 10 轮")).toBeInTheDocument();
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
  fireEvent.click(link);
  await screen.findByRole("dialog");
  expect(screen.getByTestId("location")).toHaveTextContent("page=2");
  fireEvent.click(screen.getByRole("button", { name: /close/i }));
  await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/runs?from=2026-09-01&to=2026-09-30&page=2&verdict=PASS"));
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
  const missed = runEvent(2, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 3 });
  const received = runEvent(3, { type: "cycle.started", test_run_id: runId, task_run_id: taskRunId, cycle_count: 4 });
  report.events.push(missed, received);
  act(() => {
    sources[0]!.onerror?.(new Event("error"));
    sources[0]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify(received) }));
  });
  expect(result.current.events.map((stored) => stored.sequence)).toEqual([1, 3]);
  act(() => sources[0]!.onopen?.(new Event("open")));
  await waitFor(() => expect(result.current.data?.events.map((stored) => stored.sequence)).toEqual([1, 2, 3]));
  expect(result.current.events.map((stored) => stored.sequence)).toEqual([1, 2, 3]);
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
  report.detail.test_plan.content.tasks[0]!.definition.max_cycles = 30;
  report.detail.test_plan.content.tasks.push({ test_task_id: caseId, definition: { ...report.detail.test_plan.content.tasks[0]!.definition, title: "第二任务" } });
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
  await waitFor(() => expect(requests.some((url) => url.pathname === "/api/test-cases")).toBe(true));
  expect(requests.some((url) => url.pathname.startsWith("/api/runs"))).toBe(false);
});
