import { transferableAbortController } from "node:util";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { TestRunReport, TestRunVerdict } from "../api/contracts";
import type { RunEventSource } from "../api/runEvents";
import RunPage from "../pages/RunPage";
import ReportPage from "../pages/ReportPage";
import HistoryPage from "../pages/HistoryPage";
import { caseId, runEvent, runId, runReport, taskRunId } from "./run-fixtures";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  const { default: createFetchClient } = await import("openapi-fetch");
  const { default: createQueryClient } = await import("openapi-react-query");
  return { ...original, $api: createQueryClient(createFetchClient<import("../api/schema").paths>({ baseUrl: "http://localhost", fetch: (request) => fetch(request) })) };
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
    return Response.json(report);
  }));
});
afterEach(async () => {
  cleanup(); queryClient.clear();
  await act(async () => { message.destroy(); await new Promise((resolve) => setTimeout(resolve, 0)); });
  vi.unstubAllGlobals();
});

function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }
function renderPage(path: string, history = false) {
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}><Location /><Routes>
    <Route path="/runs" element={<HistoryPage />} />
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
