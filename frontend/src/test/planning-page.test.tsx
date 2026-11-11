import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { transferableAbortController } from "node:util";
import { message } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, createMemoryRouter, RouterProvider, Route, Routes } from "react-router-dom";
import { StrictMode, useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CapabilityPreview, TestCase, TestPlan, TestPlanDraft, TestPlanGenerateRequest, TestRunListResponse, TestRunReport } from "../api/contracts";
import PlanPage from "../pages/PlanPage";
import { $api } from "../api/client";
import CasesPage from "../pages/CasesPage";
import ReportPage from "../pages/ReportPage";
import { PlanningProgressProvider } from "../api/usePlanningProgress";

function TestRouter({ children, initialEntries }: { children: ReactNode; initialEntries: string[] }) {
  const [router] = useState(() => createMemoryRouter([{ path: "*", element: children }], { initialEntries }));
  return <PlanningProgressProvider><RouterProvider router={router} /></PlanningProgressProvider>;
}

class PlanningEventSource extends EventTarget {
  static instances: PlanningEventSource[] = [];
  static autoReady = true;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();
  constructor(readonly url: string) {
    super();
    PlanningEventSource.instances.push(this);
    queueMicrotask(() => { if (PlanningEventSource.autoReady) this.dispatchEvent(new Event("ready")); });
  }
}

// Keep the real OpenAPI and React Query lifecycle; only the HTTP boundary is fake.
vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  const { default: createFetchClient } = await import("openapi-fetch");
  const { default: createQueryClient } = await import("openapi-react-query");
  const apiFetch = createFetchClient<import("../api/schema").paths>({
    baseUrl: "http://localhost",
    fetch: (request) => fetch(request)
  });
  return { ...original, apiFetch, $api: createQueryClient(apiFetch) };
});

const testCase: TestCase = {
  is_archived: false,
  id: "22222222-2222-4222-8222-222222222222",
  content: { name: "设置页面用例", source_text: "检查原始设置页面" },
  created_at: "2026-01-01T00:00:00Z"
};

const otherCase: TestCase = {
  ...testCase, id: "55555555-5555-4555-8555-555555555555", content: { name: "另一用例", source_text: "另一个原始目标" }
};

function savedPlan(version = 1, userInput: string | null = null, sourceCase = testCase): TestPlan {
  return {
    id: `${sourceCase.id === testCase.id ? "11111111" : "44444444"}-1111-4111-8111-${String(version).padStart(12, "0")}`,
    test_case_id: sourceCase.id,
    version_number: version,
    origin: version === 1 ? "planning" : "replanning",
    derived_from_plan_id: version === 1 ? null : savedPlan(version - 1, null, sourceCase).id,
    planning_context: {
      test_case_content: sourceCase.content,
      user_input: userInput,
      planning_prompt_fingerprint: "planning-fingerprint-2",
      planning_setting: { max_cycles: 20, model_response_max_attempts: 3, model_call_max_attempts: 3, tool_call_timeout_seconds: 120 },
      capability_catalog: { ...capabilities, columns: capabilities.columns.map(({ id }) => ({ id, selected: true })) },
      planning_model: {
        id: "default", mode: "scripted", model: "deterministic",
        base_url: null, temperature: 0, timeout_seconds: 60,
        context_window_tokens: 32768, max_output_tokens: 2048,
        characters_per_token: 1.5, tokens_per_image: 1024,
        context_safety_margin_tokens: 1024
      }
    },
    content: {
      title: `计划 ${version}`, assumptions: ["电视已开机"], setup_steps: [],
      tasks: [{
        test_task_id: `33333333-3333-4333-8333-${String(version).padStart(12, "0")}`,
        definition: { task_no: 1, type: "judge", title: "检查页面", goal: "确认设置页面",
          success_criteria: ["设置标题可见"] }
      }]
    },
    created_at: "2026-01-01T00:00:00Z"
  };
}

let plans: TestPlan[];
let otherPlans: TestPlan[];
let showOtherCase: boolean;
let activeRuns: TestRunListResponse[];
let requests: Request[];
let generateResponse: ((request: Request) => Promise<Response>) | undefined;
let plansResponse: (() => Promise<Response>) | undefined;
let queryClient: QueryClient;
let currentCase: TestCase;
let archived: boolean;
let updateResponse: ((request: Request) => Promise<Response>) | undefined;
let archiveResponse: (() => Promise<Response>) | undefined;
let startResponse: (() => Promise<Response>) | undefined;
let capabilities: CapabilityPreview;
let capabilityFailure: boolean;

beforeEach(() => {
  PlanningEventSource.instances = [];
  PlanningEventSource.autoReady = true;
  vi.stubGlobal("EventSource", PlanningEventSource);
  plans = [];
  otherPlans = []; showOtherCase = false; activeRuns = [];
  requests = [];
  generateResponse = undefined;
  plansResponse = undefined;
  updateResponse = undefined; archiveResponse = undefined; startResponse = undefined;
  currentCase = { ...testCase, content: { ...testCase.content } }; archived = false;
  capabilityFailure = false;
  capabilities = {
    tool_catalog: { tools: [{ name: "fixture_inspect", source_id: "mcp:fixture", description: "读取观察", input_schema: { type: "object" }, annotations: {} }] },
    resource_catalog: { local_root: null, resources: [], templates: [] },
    columns: [{ id: "mcp:fixture", fingerprint: "a".repeat(64) }, { id: "local:other", fingerprint: "b".repeat(64) }]
  };
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  // Node's Request requires its native signal, whereas jsdom replaces AbortController.
  vi.stubGlobal("AbortController", class {
    constructor() { return transferableAbortController(); }
  });
  // jsdom has no layout or resize events; these tests exercise form/API behavior.
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(),
    removeEventListener: vi.fn(), dispatchEvent: vi.fn()
  })));
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    requests.push(request.clone());
    const path = new URL(request.url).pathname;
    if (request.method === "GET") {
      if (path === "/api/capabilities") return capabilityFailure ? Response.json({ code: "mcp_unavailable", message: "目录发现失败" }, { status: 502 }) : Response.json(capabilities);
      if (path === "/api/runs") {
        const items = new URL(request.url).searchParams.has("status") ? activeRuns : [];
        return Response.json({ items, total: items.length });
      }
      if (path === "/api/test-cases") {
        const items = [...(archived ? [] : [{ ...currentCase, latest_plan_version: plans[0]?.version_number ?? null }]),
          ...(showOtherCase ? [{ ...otherCase, latest_plan_version: otherPlans[0]?.version_number ?? null }] : [])];
        return Response.json({ items, total: items.length });
      }
      if (archived && path.startsWith(`/api/test-cases/${testCase.id}`)) return Response.json({ code: "test_case_not_found", message: "用例不存在或已归档" }, { status: 404 });
      if (path === `/api/test-cases/${otherCase.id}/plans`) return Response.json({ items: otherPlans, total: otherPlans.length });
      if (path === `/api/test-cases/${otherCase.id}`) return Response.json(otherCase);
      if (path.endsWith("/plans")) return plansResponse ? plansResponse() : Response.json({ items: plans, total: plans.length });
      return Response.json(currentCase);
    }
    if (path === "/api/capabilities/refresh") return Response.json(capabilities);
    if (request.method === "PATCH") {
      if (updateResponse) return updateResponse(request);
      currentCase = { ...currentCase, content: await request.json() as TestCase["content"] };
      return Response.json(currentCase);
    }
    if (path.endsWith("/archive")) {
      if (archiveResponse) return archiveResponse();
      archived = true;
      return new Response(null, { status: 204 });
    }
    if (path.endsWith("/plans")) {
      if (generateResponse) return generateResponse(request);
      const body = await request.json() as TestPlanGenerateRequest;
      const isOtherCase = path === `/api/test-cases/${otherCase.id}/plans`;
      const plan = savedPlan((isOtherCase ? otherPlans : plans).length + 1, body.user_input ?? null, isOtherCase ? otherCase : currentCase);
      plan.planning_context.capability_catalog.columns.forEach((column) => {
        column.selected = body.selected_columns.some((selected) => selected.id === column.id);
      });
      if (isOtherCase) otherPlans = [plan, ...otherPlans];
      else plans = [plan, ...plans];
      return Response.json(plan, { status: 201 });
    }
    if (path.endsWith("/revisions")) {
      const body = await request.json() as { content: TestPlanDraft };
      const previous = plans[0];
      if (!previous) throw new Error("Expected a saved parent plan");
      if (!path.includes(previous.id)) return Response.json({ code: "test_plan_not_latest", message: "计划版本已变化" }, { status: 409 });
      const plan: TestPlan = {
        ...savedPlan(plans.length + 1), origin: "manual_revision",
        planning_context: previous.planning_context,
        content: { ...body.content, tasks: body.content.tasks.map((definition, index) => ({
          test_task_id: `revised-task-${index}`, definition
        })) }
      };
      plans = [plan, ...plans];
      return Response.json(plan, { status: 201 });
    }
    if (path === "/api/runs") return startResponse ? startResponse() : Response.json({ id: "created-run" }, { status: 201 });
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  }));
});

afterEach(async () => {
  cleanup();
  vi.useRealTimers();
  queryClient.clear();
  onlineManager.setOnline(true);
  await act(async () => { message.destroy(); await new Promise((resolve) => setTimeout(resolve, 0)); });
  vi.unstubAllGlobals();
});

function renderPlanPage() {
  render(
    <QueryClientProvider client={queryClient}>
      <StrictMode>
      <TestRouter initialEntries={[`/cases/${testCase.id}/plan`]}>
        <Link to={`/cases/${otherCase.id}/plan`}>切换用例</Link>
        <Link to={`/cases/${testCase.id}/plan`}>返回原用例</Link>
        <Link to="/runs">切换历史页面</Link>
        <Routes>
          <Route path="/cases/:caseId/plan" element={<PlanPage />} />
          <Route path="/runs/:runId" element={<div>运行已创建</div>} />
          <Route path="/runs" element={<div>历史页面</div>} />
        </Routes>
      </TestRouter>
      </StrictMode>
    </QueryClientProvider>
  );
}

it("starts with the checked preview columns without opening the resource selector", async () => {
  plans = [savedPlan()];
  renderPlanPage();
  await screen.findByRole("button", { name: "选择资源与 MCP" });
  expect(screen.getByRole("link", { name: "查看本用例历史记录 →" })).toHaveAttribute("href", "/runs?case=" + testCase.id);
  expect(screen.queryByText("最近 5 次 · 按时间倒序")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: /我已确认计划/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: /启动运行/ })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: /启动运行/ }));
  await waitFor(() => expect(requests.some((request) => request.method === "POST" && new URL(request.url).pathname === "/api/runs")).toBe(true));
  const request = requests.find((entry) => entry.method === "POST" && new URL(entry.url).pathname === "/api/runs")!;
  expect((await request.json()).selected_columns).toEqual(capabilities.columns);
});

it("keeps start disabled when the current preview request fails despite a previously cached directory", async () => {
  plans = [savedPlan()];
  queryClient.setQueryData($api.queryOptions("get", "/api/capabilities", {}).queryKey, capabilities);
  capabilityFailure = true;
  renderPlanPage();
  await screen.findByText("目录发现失败");
  fireEvent.click(screen.getByRole("checkbox", { name: /我已确认计划/ }));
  expect(screen.getByRole("button", { name: /启动运行/ })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重试发现" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /启动运行/ })).toBeEnabled());
});

it("requires a manual directory refresh after a start scope conflict", async () => {
  plans = [savedPlan()];
  startResponse = async () => Response.json({ code: "capability_catalog_changed", message: "所选目录已变化" }, { status: 409 });
  renderPlanPage();
  await screen.findByRole("button", { name: "选择资源与 MCP" });
  fireEvent.click(screen.getByRole("checkbox", { name: /我已确认计划/ }));
  await waitFor(() => expect(startButton()).toBeEnabled());
  fireEvent.click(startButton());
  await screen.findByText("所选目录已变化，请刷新并检查本次选择");
  expect(startButton()).toBeDisabled();
  expect(requests.filter((request) => request.method === "POST" && new URL(request.url).pathname === "/api/runs")).toHaveLength(1);
  capabilities.columns[0]!.fingerprint = "d".repeat(64);
  fireEvent.click(screen.getByRole("button", { name: "选择资源与 MCP" }));
  fireEvent.click(await screen.findByRole("button", { name: "刷新目录" }));
  await waitFor(() => expect(screen.queryByText("所选目录已变化，请刷新并检查本次选择")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /完\s*成/ }));
  await waitFor(() => expect(startButton()).toBeEnabled());
});

it("clears external capabilities and preserves choices while newly refreshed columns remain unchecked", async () => {
  plans = [savedPlan()];
  renderPlanPage();
  fireEvent.click(await screen.findByRole("button", { name: "选择资源与 MCP" }));
  fireEvent.click(await screen.findByRole("button", { name: "全部取消" }));
  capabilities.columns.push({ id: "mcp:new", fingerprint: "c".repeat(64) });
  fireEvent.click(screen.getByRole("button", { name: "刷新目录" }));
  await waitFor(() => expect(screen.getByRole("checkbox", { name: /MCP · new/ })).not.toBeChecked());
  expect(screen.getByRole("checkbox", { name: /MCP · fixture/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: /完\s*成/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /我已确认计划/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: /启动运行/ })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: /启动运行/ }));
  await waitFor(() => expect(requests.some((request) => request.method === "POST" && new URL(request.url).pathname === "/api/runs")).toBe(true));
  const request = requests.find((entry) => entry.method === "POST" && new URL(entry.url).pathname === "/api/runs")!;
  expect((await request.json()).selected_columns).toEqual([]);
});

const planningInput = () => screen.getByRole("textbox", { name: "规划额外输入" });
const startButton = () => screen.getByRole("button", { name: /启动运行/ });
const planningPosts = () => requests.filter((request) => request.method === "POST" && new URL(request.url).pathname.endsWith("/plans"));

it("keeps the two planning cards and execution choices independent", async () => {
  renderPlanPage();
  fireEvent.click(await screen.findByRole("button", { name: "选择规划 MCP" }));
  fireEvent.click(await screen.findByRole("button", { name: "全部取消" }));
  fireEvent.click(screen.getByRole("button", { name: /完\s*成/ }));
  fireEvent.click(screen.getByRole("button", { name: "选择规划本地资源" }));
  expect(await screen.findByRole("checkbox", { name: /其他本地资源/ })).toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: /完\s*成/ }));
  fireEvent.click(screen.getByRole("button", { name: "生成计划" }));
  await screen.findByText("计划 1");
  expect((await planningPosts()[0]!.json()).selected_columns).toEqual([capabilities.columns[1]]);
  fireEvent.click(screen.getByRole("checkbox", { name: /我已确认计划/ }));
  await waitFor(() => expect(startButton()).toBeEnabled());
  fireEvent.click(startButton());
  await waitFor(() => expect(requests.some((request) => request.method === "POST" && new URL(request.url).pathname === "/api/runs")).toBe(true));
  const runRequest = requests.find((request) => request.method === "POST" && new URL(request.url).pathname === "/api/runs")!;
  expect((await runRequest.json()).selected_columns).toEqual(capabilities.columns);
});

it("retains a planning conflict across pages and refreshes only the planning selection", async () => {
  plans = [savedPlan()];
  generateResponse = async () => Response.json({ code: "capability_catalog_changed", message: "规划所选目录已变化" }, { status: 409 });
  renderPlanPage();
  await screen.findByText("计划 1");
  fireEvent.change(planningInput(), { target: { value: "保留规划要求" } });
  fireEvent.click(screen.getByRole("button", { name: "重新规划" }));
  await screen.findByText("规划所选目录已变化");
  fireEvent.click(screen.getByRole("link", { name: "切换历史页面" }));
  fireEvent.click(await screen.findByRole("link", { name: "返回原用例" }));
  fireEvent.click(await screen.findByRole("button", { name: "关闭" }));
  expect(await screen.findByText("规划参考目录已变化，请刷新并检查选择")).toBeInTheDocument();
  expect(planningInput()).toHaveValue("保留规划要求");
  expect(screen.getByRole("button", { name: "重新规划" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /我已确认计划/ }));
  await waitFor(() => expect(startButton()).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "选择规划 MCP" }));
  fireEvent.click(await screen.findByRole("button", { name: "全部取消" }));
  capabilities.columns.push({ id: "mcp:new", fingerprint: "c".repeat(64) });
  fireEvent.click(screen.getByRole("button", { name: "刷新目录" }));
  await waitFor(() => expect(screen.getByRole("checkbox", { name: /MCP · new/ })).not.toBeChecked());
  expect(screen.getByRole("checkbox", { name: /MCP · fixture/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: /完\s*成/ }));
  await waitFor(() => expect(screen.getByRole("button", { name: "重新规划" })).toBeEnabled());
  expect(screen.queryByText("规划参考目录已变化，请刷新并检查选择")).not.toBeInTheDocument();
  expect(startButton()).toBeEnabled();
  expect(planningPosts()).toHaveLength(1);
});

describe("planning page", () => {
  it.each([true, false])("cleans a pending subscription on unmount without submitting when online=%s", async (online) => {
    PlanningEventSource.autoReady = false;
    renderPlanPage();
    const generateButton = await screen.findByRole("button", { name: "生成计划" });
    onlineManager.setOnline(online);
    fireEvent.click(generateButton);
    await screen.findByRole("dialog", { name: "规划进度" });
    expect(PlanningEventSource.instances).toHaveLength(1);
    const stream = PlanningEventSource.instances[0]!;
    await act(async () => cleanup());
    expect(stream.close).toHaveBeenCalledOnce();
    expect(stream.onmessage).toBeNull();
    await act(async () => {
      onlineManager.setOnline(true);
      await queryClient.resumePausedMutations();
      stream.dispatchEvent(new Event("ready"));
    });
    expect(PlanningEventSource.instances).toHaveLength(1);
    expect(planningPosts()).toHaveLength(0);
  });

  it("waits for ready, submits once, filters logs and succeeds without a terminal SSE event", async () => {
    PlanningEventSource.autoReady = false;
    const selectedColumns = capabilities.columns.map((column) => ({ ...column }));
    let complete!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { complete = resolve; });
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: "生成计划" }));
    expect(await screen.findByRole("dialog", { name: "规划进度" })).toHaveAttribute("aria-busy", "true");
    expect(planningPosts()).toHaveLength(0);
    capabilities.columns[0]!.fingerprint = "d".repeat(64);
    expect(screen.queryByRole("button", { name: "关闭" })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const stream = PlanningEventSource.instances[0]!;
    act(() => { stream.dispatchEvent(new Event("ready")); stream.dispatchEvent(new Event("ready")); });
    await waitFor(() => expect(planningPosts()).toHaveLength(1));
    expect((await planningPosts()[0]!.clone().json()).selected_columns).toEqual(selectedColumns);
    const { planning_request_id } = await planningPosts()[0]!.clone().json();
    const event = { test_case_id: testCase.id, planning_request_id, occurred_at: "2026-01-01T00:00:00Z", type: "planning.cycle_started", cycle_count: 1, max_cycles: 7 };
    act(() => {
      stream.onmessage?.(new MessageEvent("message", { data: JSON.stringify({ ...event, planning_request_id: "99999999-9999-4999-8999-999999999999", cycle_count: 2 }) }));
      stream.onmessage?.(new MessageEvent("message", { data: JSON.stringify({ ...event, test_case_id: "99999999-9999-4999-8999-999999999999", cycle_count: 3 }) }));
      stream.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
      stream.onerror?.(new Event("error"));
      stream.onopen?.(new Event("open"));
      stream.dispatchEvent(new Event("ready"));
    });
    expect(screen.getByRole("log")).toHaveTextContent("第 1/7 轮决策");
    expect(screen.getByRole("log")).not.toHaveTextContent("第 2/7 轮决策");
    expect(screen.getByRole("log")).not.toHaveTextContent("第 3/7 轮决策");
    expect(screen.getByText(/日志连接已恢复/)).toBeInTheDocument();
    expect(planningPosts()).toHaveLength(1);
    plans = [savedPlan()];
    await act(async () => complete(Response.json(plans[0], { status: 201 })));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(stream.close).toHaveBeenCalled();
  });

  it.each([true, false])("times out before POST and clears failed logs when online=%s", async (online) => {
    PlanningEventSource.autoReady = false;
    renderPlanPage();
    const generateButton = await screen.findByRole("button", { name: "生成计划" });
    onlineManager.setOnline(online);
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(generateButton); });
    expect(screen.getByRole("dialog", { name: "规划进度" })).toHaveAttribute("aria-busy", "true");
    await act(async () => { fireEvent.click(screen.getByRole("link", { name: "切换用例" })); });
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_001); });
    await act(async () => { fireEvent.click(screen.getByRole("link", { name: "返回原用例" })); });
    expect(screen.getByRole("log")).toHaveTextContent("日志连接超时");
    expect(planningPosts()).toHaveLength(0);
    expect(PlanningEventSource.instances[0]!.close).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    vi.useRealTimers();
    act(() => onlineManager.setOnline(true));
    await screen.findByRole("button", { name: "生成计划" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "生成计划" })); });
    expect(screen.getByRole("log")).toBeEmptyDOMElement();
  });

  it.each([true, false])("reports request network failure without queueing or retrying when online=%s", async (online) => {
    PlanningEventSource.autoReady = false;
    generateResponse = async () => { throw new TypeError("fetch failed"); };
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: "生成计划" }));
    await screen.findByRole("dialog", { name: "规划进度" });
    act(() => {
      onlineManager.setOnline(online);
      PlanningEventSource.instances[0]!.dispatchEvent(new Event("ready"));
    });
    expect(await screen.findByText(/规划结果未确认/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭" })).toBeEnabled();
    expect(PlanningEventSource.instances[0]!.close).toHaveBeenCalled();
    await act(async () => {
      onlineManager.setOnline(true);
      await queryClient.resumePausedMutations();
    });
    expect(planningPosts()).toHaveLength(1);
    cleanup();
    expect(PlanningEventSource.instances[0]!.onmessage).toBeNull();
  });

  it("waits for HTTP after malformed SSE and retains a failure panel when the case disappears", async () => {
    let complete!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { complete = resolve; });
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: "生成计划" }));
    await waitFor(() => expect(planningPosts()).toHaveLength(1));
    const stream = PlanningEventSource.instances[0]!;
    act(() => stream.onmessage?.(new MessageEvent("message", { data: "{}" })));
    expect(screen.getByText(/日志不可用/)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-busy", "true");
    archived = true;
    await act(async () => complete(Response.json({ code: "planning_input_not_found", message: "用例已归档" }, { status: 404 })));
    expect(await screen.findByRole("button", { name: "关闭" })).toBeEnabled();
    expect(screen.getByRole("dialog")).toHaveTextContent("用例已归档");
  });

  it("keeps planning and logs across navigation and rebuilds the case before restoring progress", async () => {
    plans = [savedPlan()];
    let completePlanning!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { completePlanning = resolve; });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "调整计划" } });
    fireEvent.click(screen.getByRole("button", { name: "重新规划" }));
    await waitFor(() => expect(completePlanning).toBeDefined());
    const stream = PlanningEventSource.instances[0]!;
    const initialReads = requests.filter((request) => request.method === "GET" && new URL(request.url).pathname === `/api/test-cases/${testCase.id}`).length;
    const { planning_request_id } = await planningPosts()[0]!.clone().json();

    fireEvent.click(screen.getByRole("link", { name: "切换用例" }));
    await screen.findByRole("heading", { name: "另一用例" });
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    expect(stream.close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("link", { name: "切换历史页面" }));
    await screen.findByText("历史页面");
    act(() => stream.onmessage?.(new MessageEvent("message", { data: JSON.stringify({
      type: "planning.cycle_started", cycle_count: 1, max_cycles: 7, test_case_id: testCase.id,
      planning_request_id, occurred_at: "2026-01-01T00:00:00Z"
    }) })));
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    await screen.findByRole("heading", { name: testCase.content.name });
    expect(screen.getByRole("log")).toHaveTextContent("第 1/7 轮决策");
    expect(requests.filter((request) => request.method === "GET" && new URL(request.url).pathname === `/api/test-cases/${testCase.id}`).length).toBeGreaterThan(initialReads);
    expect(await screen.findByRole("button", { name: /编辑用例/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /编辑计划/ })).toBeDisabled();
    expect(planningInput()).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(startButton()).toBeDisabled();
    expect(planningPosts()).toHaveLength(1);
    expect(screen.getByRole("link", { name: "查看本用例历史记录 →" }).closest("[inert]")).not.toBeNull();

    plans = [savedPlan(2, "调整计划"), ...plans];
    await act(async () => { completePlanning(Response.json(plans[0], { status: 201 })); });
    await waitFor(() => expect(screen.getByRole("button", { name: /编辑用例/ })).toBeEnabled());
    expect(screen.getByRole("combobox", { name: "计划版本" })).toBeEnabled();
    expect(planningInput()).toHaveValue("");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  it("edits both case fields atomically, validates blanks and requires confirmation again", async () => {
    plans = [savedPlan()];
    const originalPlan = structuredClone(plans[0]);
    renderPlanPage();
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /编辑用例/ }));
    expect(startButton()).toBeDisabled();
    expect(screen.getByRole("button", { name: /编辑计划/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新规划" })).toBeDisabled();
    const name = screen.getByRole("textbox", { name: "用例名称" });
    const description = screen.getByRole("textbox", { name: "用例描述" });
    expect(name).toHaveAttribute("maxlength", "200");
    fireEvent.change(name, { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "保存用例" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "新的名称" } });
    fireEvent.change(description, { target: { value: "\n " } });
    expect(screen.getByRole("button", { name: "保存用例" })).toBeDisabled();
    fireEvent.change(description, { target: { value: "新的描述" } });
    fireEvent.click(screen.getByRole("button", { name: "保存用例" }));
    await screen.findByRole("heading", { name: "新的名称" });
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(startButton()).toBeDisabled();
    expect(screen.getAllByText("用例已更新，请检查计划或重新规划").length).toBeGreaterThan(0);
    const updates = requests.filter((request) => request.method === "PATCH");
    expect(updates).toHaveLength(1);
    expect(await updates[0]!.json()).toEqual({ name: "新的名称", source_text: "新的描述" });
    expect(plans[0]).toEqual(originalPlan);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(startButton()).toBeEnabled();
  });

  it("continues connecting across navigation and submits only after ready", async () => {
    PlanningEventSource.autoReady = false;
    let complete!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { complete = resolve; });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "连接期间的输入" } });
    fireEvent.click(screen.getByRole("button", { name: "生成计划" }));
    const stream = PlanningEventSource.instances[0]!;
    fireEvent.click(screen.getByRole("link", { name: "切换历史页面" }));
    await screen.findByText("历史页面");
    expect(stream.close).not.toHaveBeenCalled();
    expect(planningPosts()).toHaveLength(0);
    act(() => { stream.dispatchEvent(new Event("ready")); stream.dispatchEvent(new Event("ready")); });
    await waitFor(() => expect(planningPosts()).toHaveLength(1));
    plans = [savedPlan(1, "连接期间的输入")];
    await act(async () => complete(Response.json(plans[0], { status: 201 })));
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    await screen.findByText("版本 1 · 最新");
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    expect(planningInput()).toHaveValue("");
    expect(planningPosts()).toHaveLength(1);
  });

  it("displays the newly saved plan when planning succeeds on another page", async () => {
    plans = [savedPlan()];
    let complete!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { complete = resolve; });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "后台成功" } });
    fireEvent.click(screen.getByRole("button", { name: "重新规划" }));
    await waitFor(() => expect(complete).toBeDefined());
    fireEvent.click(screen.getByRole("link", { name: "切换历史页面" }));
    await screen.findByText("历史页面");
    plans = [savedPlan(2, "后台成功"), ...plans];
    await act(async () => complete(Response.json(plans[0], { status: 201 })));
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    await screen.findByText("版本 2 · 最新");
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    expect(planningInput()).toHaveValue("");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  it("restores off-page failures and returns only their input to the form on close", async () => {
    let complete!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { complete = resolve; });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "  保留原始输入  " } });
    fireEvent.click(screen.getByRole("button", { name: "生成计划" }));
    await waitFor(() => expect(complete).toBeDefined());
    fireEvent.click(screen.getByRole("link", { name: "切换历史页面" }));
    await screen.findByText("历史页面");
    await act(async () => complete(Response.json({ code: "planning_failed", message: "后台规划失败" }, { status: 502 })));
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    expect(await screen.findByRole("log")).toHaveTextContent("后台规划失败");
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await screen.findByRole("heading", { name: testCase.content.name });
    expect(planningInput()).toHaveValue("  保留原始输入  ");
    fireEvent.click(screen.getByRole("link", { name: "切换用例" }));
    await screen.findByRole("heading", { name: "另一用例" });
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    await screen.findByRole("heading", { name: testCase.content.name });
    expect(planningInput()).toHaveValue("");
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    expect(planningPosts()).toHaveLength(1);
  });

  it("refreshes a cache-expired case when a stale initial read overlaps planning success", async () => {
    queryClient.setQueryDefaults(["get", "/api/test-cases/{test_case_id}/plans"], { gcTime: 0 });
    plans = [savedPlan()];
    let completePlanning!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { completePlanning = resolve; });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "缓存过期后的规划" } });
    fireEvent.click(screen.getByRole("button", { name: "重新规划" }));
    await waitFor(() => expect(completePlanning).toBeDefined());
    fireEvent.click(screen.getByRole("link", { name: "切换历史页面" }));
    await screen.findByText("历史页面");
    await waitFor(() => expect(queryClient.getQueryData(["get", "/api/test-cases/{test_case_id}/plans", {
      params: { path: { test_case_id: testCase.id } }
    }])).toBeUndefined());
    const staleResponse = Response.json({ items: plans, total: plans.length });
    const staleReads: ((response: Response) => void)[] = [];
    let planSaved = false;
    let readsAfterSave = 0;
    plansResponse = () => {
      if (!planSaved) return new Promise((resolve) => { staleReads.push(resolve); });
      readsAfterSave++;
      return Promise.resolve(Response.json({ items: plans, total: plans.length }));
    };
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    await waitFor(() => expect(staleReads.length).toBeGreaterThan(0));
    plans = [savedPlan(2, "缓存过期后的规划"), ...plans];
    planSaved = true;
    await act(async () => completePlanning(Response.json(plans[0], { status: 201 })));
    await act(async () => staleReads.forEach((complete) => complete(staleResponse.clone())));
    await screen.findByText("版本 2 · 最新");
    expect(readsAfterSave).toBeGreaterThan(0);
    expect(planningPosts()).toHaveLength(1);
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
  });

  it("isolates simultaneous planning requests and their completion by case", async () => {
    const completions = new Map<string, (response: Response) => void>();
    generateResponse = (request) => new Promise((resolve) => { completions.set(new URL(request.url).pathname, resolve); });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "原用例输入" } });
    fireEvent.click(screen.getByRole("button", { name: "生成计划" }));
    await waitFor(() => expect(planningPosts()).toHaveLength(1));
    fireEvent.click(screen.getByRole("link", { name: "切换用例" }));
    await screen.findByRole("heading", { name: "另一用例" });
    fireEvent.change(planningInput(), { target: { value: "另一用例输入" } });
    fireEvent.click(screen.getByRole("button", { name: "生成计划" }));
    await waitFor(() => expect(planningPosts()).toHaveLength(2));
    for (const [index, request] of planningPosts().entries()) {
      const { planning_request_id } = await request.clone().json();
      const caseId = index === 0 ? testCase.id : otherCase.id;
      act(() => PlanningEventSource.instances[index]!.onmessage?.(new MessageEvent("message", { data: JSON.stringify({
        type: "planning.cycle_started", cycle_count: index + 1, max_cycles: 7, test_case_id: caseId,
        planning_request_id, occurred_at: "2026-01-01T00:00:00Z"
      }) })));
    }
    expect(screen.getByRole("log")).toHaveTextContent("第 2/7 轮决策");
    expect(screen.getByRole("log")).not.toHaveTextContent("第 1/7 轮决策");
    plans = [savedPlan(1, "原用例输入")];
    await act(async () => completions.get(`/api/test-cases/${testCase.id}/plans`)!(Response.json(plans[0], { status: 201 })));
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-busy", "true");
    expect(PlanningEventSource.instances[1]!.close).not.toHaveBeenCalled();
    await act(async () => completions.get(`/api/test-cases/${otherCase.id}/plans`)!(Response.json({ code: "planning_failed", message: "另一请求失败" }, { status: 502 })));
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(planningInput()).toHaveValue("另一用例输入");
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    await screen.findByText("版本 1 · 最新");
    expect(planningInput()).toHaveValue("");
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
  });

  it("starts a new application session without restoring an old pending request", async () => {
    let complete!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { complete = resolve; });
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: "生成计划" }));
    await waitFor(() => expect(complete).toBeDefined());
    cleanup();
    expect(PlanningEventSource.instances[0]!.close).toHaveBeenCalledOnce();
    renderPlanPage();
    await screen.findByRole("button", { name: "生成计划" });
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    expect(planningPosts()).toHaveLength(1);
    plans = [savedPlan()];
    await act(async () => complete(Response.json(plans[0], { status: 201 })));
    expect(screen.queryByRole("dialog", { name: "规划进度" })).not.toBeInTheDocument();
    expect(PlanningEventSource.instances).toHaveLength(1);
  });

  it("cancels local case changes without a request and preserves input on failure", async () => {
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: /编辑用例/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "用例名称" }), { target: { value: "放弃的修改" } });
    fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    expect(requests.filter((request) => request.method === "PATCH")).toHaveLength(0);
    expect(screen.getByRole("heading", { name: testCase.content.name })).toBeInTheDocument();
    updateResponse = async () => Response.json({ code: "test_case_planning", message: "用例正在生成计划" }, { status: 409 });
    fireEvent.click(screen.getByRole("button", { name: /编辑用例/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "用例描述" }), { target: { value: "保留编辑内容" } });
    fireEvent.click(screen.getByRole("button", { name: "保存用例" }));
    await screen.findByText("用例正在生成计划");
    expect(screen.getByRole("textbox", { name: "用例描述" })).toHaveValue("保留编辑内容");
  });

  it.each(["", "本轮补充要求"])("allows initial generation with optional input: %s", async (userInput) => {
    renderPlanPage();
    const generate = await screen.findByRole("button", { name: "生成计划" });
    expect(screen.getByText(testCase.content.source_text)).toBeInTheDocument();
    expect(screen.getByText("额外输入（可选）")).toBeInTheDocument();
    fireEvent.change(planningInput(), { target: { value: userInput } });
    expect(generate).toBeEnabled();
    fireEvent.click(generate);
    await screen.findByRole("button", { name: "重新规划" });
    expect(await planningPosts()[0]?.json()).toEqual({ planning_request_id: expect.any(String), user_input: userInput || null, selected_columns: capabilities.columns });
    expect(planningInput()).toHaveValue("");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  it("requires replanning input, clears it on success and resets confirmation", async () => {
    plans = [savedPlan(1, "历史额外输入")];
    renderPlanPage();
    const replan = await screen.findByRole("button", { name: "重新规划" });
    expect(screen.getByText("额外输入（必填）")).toBeInTheDocument();
    expect(screen.getByText(/历史额外输入/)).toBeInTheDocument();
    expect(replan).toBeDisabled();
    fireEvent.change(planningInput(), { target: { value: " \n " } });
    expect(replan).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(startButton()).toBeEnabled();
    fireEvent.change(planningInput(), { target: { value: "细化成功标准" } });
    fireEvent.click(replan);
    await screen.findByText("版本 2 · 最新");
    expect(planningInput()).toHaveValue("");
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(startButton()).toBeDisabled();
    expect(await planningPosts()[0]?.json()).toEqual({ planning_request_id: expect.any(String), user_input: "细化成功标准", selected_columns: capabilities.columns });
  });

  it.each([502, 409])("retains input after HTTP %s and does not automatically replan", async (status) => {
    plans = [savedPlan()];
    generateResponse = async () => {
      if (status === 409) plans = [savedPlan(2, "另一请求的输入"), ...plans];
      return Response.json({ code: status === 409 ? "test_plan_not_latest" : "planning_failed", message: "本次规划未保存" }, { status });
    };
    renderPlanPage();
    const replan = await screen.findByRole("button", { name: "重新规划" });
    fireEvent.change(planningInput(), { target: { value: "保留本轮输入" } });
    fireEvent.click(replan);
    await screen.findByText("本次规划未保存");
    if (status === 409) await screen.findByText("版本 2 · 最新");
    fireEvent.click(await screen.findByRole("button", { name: "关闭" }));
    await waitFor(() => expect(replan).toBeEnabled());
    expect(planningInput()).toHaveValue("保留本轮输入");
    expect(planningPosts()).toHaveLength(1);
  });

  it("allows closing a confirmed planning failure while refreshing the latest plan is still pending", async () => {
    plans = [savedPlan()];
    let finishRefresh!: (response: Response) => void;
    generateResponse = async () => {
      plansResponse = () => new Promise((resolve) => { finishRefresh = resolve; });
      return Response.json({ code: "test_plan_not_latest", message: "版本冲突，规划未保存" }, { status: 409 });
    };
    renderPlanPage();
    const replan = await screen.findByRole("button", { name: "重新规划" });
    fireEvent.change(planningInput(), { target: { value: "保留修改要求" } });
    fireEvent.click(replan);
    const closeButton = await screen.findByRole("button", { name: "关闭" });
    expect(screen.getByRole("log")).toHaveTextContent("版本冲突，规划未保存");
    expect(closeButton).toBeEnabled();
    fireEvent.click(closeButton);
    await act(async () => finishRefresh(Response.json({ items: [savedPlan(2)], total: 1 })));
    expect(planningInput()).toHaveValue("保留修改要求");
  });

  it("requires saving manual edits before replanning and keeps input while saving", async () => {
    plans = [savedPlan()];
    renderPlanPage();
    const replan = await screen.findByRole("button", { name: "重新规划" });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(planningInput(), { target: { value: "补充要求" } });
    fireEvent.click(screen.getByRole("button", { name: /编辑计划/ }));
    fireEvent.change(screen.getByDisplayValue("计划 1"), { target: { value: "人工修改标题" } });
    expect(replan).toBeDisabled();
    expect(startButton()).toBeDisabled();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: /保存新版本/ }));
    await screen.findByText("版本 2 · 最新");
    expect(replan).toBeEnabled();
    expect(planningInput()).toHaveValue("补充要求");
  });

  it("disables edits, saving and execution while a generation request is pending", async () => {
    plans = [savedPlan()];
    let finishGeneration: (response: Response) => void = () => { throw new Error("Request has not started"); };
    generateResponse = () => new Promise<Response>((resolve) => { finishGeneration = resolve; });
    renderPlanPage();
    const replan = await screen.findByRole("button", { name: "重新规划" });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /编辑计划/ }));
    fireEvent.change(planningInput(), { target: { value: "细化计划" } });
    fireEvent.click(replan);
    await waitFor(() => expect(planningPosts()).toHaveLength(1));
    expect(replan).toBeDisabled();
    expect(planningInput()).toBeDisabled();
    expect(screen.getByDisplayValue("计划 1")).toBeDisabled();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新增任务/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /保存新版本/ })).toBeDisabled();
    expect(startButton()).toBeDisabled();
    const next = savedPlan(2, "细化计划");
    plans = [next, ...plans];
    finishGeneration(Response.json(next, { status: 201 }));
    await screen.findByText("版本 2 · 最新");
  });

  it("uses the checkbox only as a UI guard and sends one existing start request", async () => {
    const plan = savedPlan();
    plans = [plan];
    renderPlanPage();
    await screen.findByRole("button", { name: "重新规划" });
    expect(startButton()).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(requests.filter((request) => request.method === "POST")).toHaveLength(0);
    fireEvent.click(startButton());
    await screen.findByText("运行已创建");
    const posts = requests.filter((request) => request.method === "POST");
    expect(posts).toHaveLength(1);
    expect(new URL(posts[0]!.url).pathname).toBe("/api/runs");
    expect(await posts[0]!.json()).toEqual({ test_case_id: plan.test_case_id, test_plan_id: plan.id, assumptions_confirmed: true, selected_columns: capabilities.columns });
  });

  it("does not carry a plan, extra input or confirmation into another test case", async () => {
    plans = [savedPlan()];
    renderPlanPage();
    await screen.findByRole("button", { name: "重新规划" });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(planningInput(), { target: { value: "仅属于原用例的输入" } });
    fireEvent.click(screen.getByRole("link", { name: "切换用例" }));
    await screen.findByRole("button", { name: "生成计划" });
    expect(screen.getByText("另一个原始目标")).toBeInTheDocument();
    expect(planningInput()).toHaveValue("");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("allows historical versions only for reading", async () => {
    plans = [savedPlan(2), savedPlan(1)];
    renderPlanPage();
    await screen.findByText("版本 2 · 最新");
    expect(screen.getByRole("combobox", { name: "计划版本" }).closest(".plan-results")).not.toBeNull();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "计划版本" }));
    fireEvent.click(await screen.findByText("v1 · 只读"));
    await screen.findByText("版本 1 · 历史只读");
    expect(screen.getByRole("button", { name: /编辑计划/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新规划" })).toBeDisabled();
    expect(startButton()).toBeDisabled();
  });

  it("can discard a draft after another client saves a newer plan", async () => {
    plans = [savedPlan()];
    plans[0]!.content.setup_steps = ["准备环境"];
    plans[0]!.content.tasks.push({ test_task_id: "second-task", definition: { ...plans[0]!.content.tasks[0]!.definition, task_no: 2, title: "第二任务" } });
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: /编辑计划/ }));
    fireEvent.change(screen.getByDisplayValue("计划 1"), { target: { value: "未保存草稿" } });
    expect(screen.getByRole("button", { name: "上移任务 2" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "下移任务 1" })).toBeEnabled();
    plans = [savedPlan(2), ...plans];
    fireEvent.click(screen.getByRole("button", { name: /保存新版本/ }));
    await screen.findByText("计划版本已变化");
    await waitFor(() => expect(screen.getByRole("button", { name: "上移任务 2" })).toBeDisabled());
    for (const name of ["下移任务 1", "删除任务 1", "删除任务 2", "新增任务", "将准备步骤 1 转为任务"]) {
      expect(screen.getByRole("button", { name: new RegExp(name) })).toBeDisabled();
    }
    expect(screen.getByDisplayValue("未保存草稿")).toBeDisabled();
    expect(screen.getByDisplayValue("第二任务")).toBeDisabled();
    expect(screen.getByRole("button", { name: /保存新版本/ })).toBeDisabled();
    fireEvent.click(screen.getAllByRole("button", { name: "放弃修改" }).at(-1)!);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "计划版本" })).not.toBeDisabled());
    expect(screen.queryByText("未保存草稿")).not.toBeInTheDocument();
  });

  it("keeps queue editing available on the latest plan and saves a new version", async () => {
    const original = savedPlan();
    original.content.setup_steps = ["准备环境"];
    original.content.tasks.push({ test_task_id: "second-task", definition: { ...original.content.tasks[0]!.definition, task_no: 2, title: "第二任务" } });
    plans = [original];
    renderPlanPage();
    fireEvent.click(await screen.findByRole("button", { name: /编辑计划/ }));
    fireEvent.click(screen.getByRole("button", { name: "上移任务 2" }));
    expect(document.querySelector(".task-editor input[maxlength='200']")).toHaveValue("第二任务");
    fireEvent.click(screen.getByRole("button", { name: "删除任务 2" }));
    fireEvent.click(screen.getByRole("button", { name: /新增任务/ }));
    expect(document.querySelectorAll(".task-editor")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "删除任务 2" }));
    fireEvent.click(screen.getByRole("button", { name: "将准备步骤 1 转为任务" }));
    expect(document.querySelectorAll(".task-editor")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "删除任务 2" }));
    fireEvent.click(screen.getByRole("button", { name: /保存新版本/ }));
    await screen.findByText("版本 2 · 最新");
    expect(plans[0]!.content.tasks.map((task) => task.definition.title)).toEqual(["第二任务"]);
    expect(plans[0]!.content.tasks[0]!.test_task_id).not.toBe("second-task");
    expect(original.content.tasks.map((task) => task.definition.title)).toEqual(["检查页面", "第二任务"]);
  });
});

it.each(["delete", "move"])("confirms and clears an invalidated Goal reference on %s", async (operation) => {
  const original = savedPlan();
  original.content.tasks.push({ test_task_id: "goal-task", definition: {
    task_no: 2, type: "goal", title: "遍历结束", goal: "所有节点已检查", success_criteria: ["没有待检查节点"], jump_back_to_task_no: 1
  } });
  plans = [original];
  renderPlanPage();
  fireEvent.click(await screen.findByRole("button", { name: /编辑计划/ }));
  const action = operation === "delete" ? "删除任务 1" : "下移任务 1";
  fireEvent.click(screen.getByRole("button", { name: action }));
  await screen.findByText("跳转目标失效");
  await waitFor(() => expect(screen.getByText("任务 2 · 遍历结束")).toBeVisible());
  expect(document.querySelectorAll(".task-editor")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
  await waitFor(() => expect(screen.getByText("跳转目标失效")).not.toBeVisible());
  expect(document.querySelector(".task-editor input[maxlength='200']")).toHaveValue("检查页面");
  fireEvent.click(screen.getByRole("button", { name: action }));
  fireEvent.click(await screen.findByRole("button", { name: "继续并清空" }));
  await screen.findByText("请选择前序任务");
  await waitFor(() => expect(document.querySelectorAll(".task-editor")).toHaveLength(operation === "delete" ? 1 : 2));
  expect(screen.getByRole("button", { name: /保存新版本/ })).toBeDisabled();
  expect(original.content.tasks[1]!.definition.jump_back_to_task_no).toBe(1);
});

function renderCasesPage() {
  render(<QueryClientProvider client={queryClient}><TestRouter initialEntries={[`/cases/${testCase.id}/plan?search=设置`]}>
    <Routes><Route path="/" element={<CasesPage />} /><Route path="/cases/:caseId/plan" element={<CasesPage />} /><Route path="/runs/:runId" element={<div>运行已创建</div>} /></Routes>
  </TestRouter></QueryClientProvider>);
}

it("keeps archive guarded after leaving a planning case and allows another case to run", async () => {
  showOtherCase = true;
  otherPlans = [savedPlan(1, null, otherCase)];
  let complete!: (response: Response) => void;
  generateResponse = () => new Promise((resolve) => { complete = resolve; });
  renderCasesPage();
  fireEvent.click(await screen.findByRole("button", { name: "生成计划" }));
  await waitFor(() => expect(complete).toBeDefined());
  const stream = PlanningEventSource.instances[0]!;
  expect(screen.getByRole("button", { name: "归档用例：" + testCase.content.name })).toBeDisabled();
  fireEvent.click(screen.getByRole("link", { name: /另一用例/ }));
  await screen.findByRole("heading", { name: "另一用例", level: 2 });
  expect(screen.getByRole("button", { name: "归档用例：" + testCase.content.name })).toBeDisabled();
  expect(screen.getByRole("button", { name: "归档用例：另一用例" })).toBeEnabled();
  fireEvent.click(await screen.findByRole("checkbox"));
  expect(startButton()).toBeEnabled();
  fireEvent.click(startButton());
  await screen.findByText("运行已创建");
  expect(stream.close).not.toHaveBeenCalled();
  plans = [savedPlan()];
  await act(async () => complete(Response.json(plans[0], { status: 201 })));
});

it("disables only run start globally while another case has an active run", async () => {
  plans = [savedPlan()];
  activeRuns = [{
    id: "active-run", test_plan_id: "other-plan", test_case_id: otherCase.id, test_case_name: otherCase.content.name,
    test_case_archived: false, status: "running", verdict: null, created_at: testCase.created_at,
    started_at: testCase.created_at, finished_at: null, image_count: 0, task_count: 1, task_status_counts: { running: 1 }
  }];
  renderPlanPage();
  fireEvent.click(await screen.findByRole("checkbox"));
  expect(startButton()).toBeDisabled();
  expect(screen.getByRole("button", { name: /编辑用例/ })).toBeEnabled();
  fireEvent.change(planningInput(), { target: { value: "另一个用例仍可规划" } });
  expect(screen.getByRole("button", { name: "重新规划" })).toBeEnabled();
});

it("keeps run start disabled across case switches during preparation and releases it on failure", async () => {
  plans = [savedPlan()]; otherPlans = [savedPlan(1, null, otherCase)];
  let completeStart!: (response: Response) => void;
  startResponse = () => new Promise((resolve) => { completeStart = resolve; });
  renderPlanPage();
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(startButton());
  await waitFor(() => expect(completeStart).toBeDefined());
  fireEvent.click(screen.getByRole("link", { name: "切换用例" }));
  await screen.findByRole("heading", { name: "另一用例" });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(startButton()).toBeDisabled();
  expect(screen.getByRole("button", { name: /编辑用例/ })).toBeEnabled();
  await act(async () => completeStart(Response.json({ code: "mcp_unavailable", message: "准备失败" }, { status: 502 })));
  await waitFor(() => expect(startButton()).toBeEnabled());
});

it("archives from a separate inbox button, retains history wording and clears planning caches", async () => {
  plans = [savedPlan()];
  renderCasesPage();
  const archive = await screen.findByRole("button", { name: "归档用例：" + testCase.content.name });
  expect(archive.closest("a")).toBeNull();
  expect(archive.querySelector("[aria-label='inbox']")).toBeInTheDocument();
  fireEvent.click(archive);
  await screen.findByRole("dialog");
  expect(screen.getByText("归档后从规划页移除，历史运行与记录保留。")).toBeInTheDocument();
  expect(startButton()).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
  expect(requests.some((request) => new URL(request.url).pathname.endsWith("/archive"))).toBe(false);
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(archive);
  fireEvent.click(await screen.findByRole("button", { name: "归档用例" }));
  await screen.findByText("从测试意图开始");
  await waitFor(() => expect(screen.queryByRole("button", { name: "归档用例：" + testCase.content.name })).not.toBeInTheDocument());
  expect(screen.getByRole("searchbox", { name: "搜索用例" })).toHaveValue("设置");
  // React Query may register an empty query during the navigation render;
  // the archived plan content must no longer remain cached.
  expect(queryClient.getQueriesData({ queryKey: ["get", "/api/test-cases/{test_case_id}/plans"] }).every(([, value]) => value === undefined)).toBe(true);
});

it.each([
  { status: 502, code: "mcp_unavailable", message: "MCP 连接失败" },
  { status: 409, code: "active_run_exists", message: "Run preparation is already in progress" }
])("disables archive during start and releases it after HTTP $status", async ({ status, code, message: errorMessage }) => {
  plans = [savedPlan()];
  let completeStart!: (response: Response) => void;
  startResponse = () => new Promise((resolve) => { completeStart = resolve; });
  renderCasesPage();
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(startButton());
  const archive = screen.getByRole("button", { name: "归档用例：" + testCase.content.name });
  await waitFor(() => expect(archive).toBeDisabled());
  await waitFor(() => expect(completeStart).toBeDefined());
  completeStart(Response.json({ code, message: errorMessage }, { status }));
  await screen.findByText(errorMessage);
  await waitFor(() => expect(archive).toBeEnabled());
  expect(startButton()).toBeEnabled();
  expect(screen.queryByText("运行已创建")).not.toBeInTheDocument();
  expect(requests.filter((request) => request.method === "POST" && new URL(request.url).pathname === "/api/runs")).toHaveLength(1);
});

it("keeps the case and dialog when archive is rejected", async () => {
  archiveResponse = async () => Response.json({ code: "test_case_has_active_run", message: "用例正在运行" }, { status: 409 });
  renderCasesPage();
  fireEvent.click(await screen.findByRole("button", { name: "归档用例：" + testCase.content.name }));
  fireEvent.click(await screen.findByRole("button", { name: "归档用例" }));
  await screen.findByText("用例正在运行");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(archived).toBe(false);
});

it("hides cached planning operations when an archived case is refetched", async () => {
  plans = [savedPlan()];
  renderPlanPage();
  await screen.findByRole("button", { name: /编辑计划/ });
  archived = true;
  await act(async () => { await queryClient.invalidateQueries({ queryKey: ["get", "/api/test-cases/{test_case_id}"] }); });
  await screen.findByText("用例不存在或已归档");
  expect(screen.queryByRole("button", { name: /启动运行/ })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "返回用例列表" })).toBeInTheDocument();
});

it("shows the saved planning input in the online report", async () => {
  const plan = savedPlan(1, "报告保留的规划输入");
  const report: TestRunReport = {
    test_case_archived: false,
    detail: { test_plan: plan, run: {
      id: "run", test_plan_id: plan.id, status: "finished", verdict: "PASS",
      created_at: plan.created_at, started_at: plan.created_at, finished_at: plan.created_at
    }, environment: { execution_model: plan.planning_context.planning_model,
      capability_catalog: { tool_catalog: { tools: [] }, resource_catalog: { local_root: null, resources: [], templates: [] }, columns: [] },
      prompt_fingerprints_by_task_type: { act: "act-v1", judge: "judge-v1", goal: "goal-v1" }, app_version: "0.1.0", execution_protocol_version: "12",
      execution: { max_cycles_by_task_type: { act: 30, judge: 10, goal: 10 }, goal_max_failures: 10,
        tool_call_timeout_seconds: 120, image_history_rounds: 3, model_call_max_attempts: 3, model_response_max_attempts: 3 }
    }, task_runs: [] },
    artifacts: [], events: []
  };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(report)));
  render(
    <QueryClientProvider client={queryClient}>
      <TestRouter initialEntries={["/runs/run/report"]}>
        <Routes><Route path="/runs/:runId/report" element={<ReportPage />} /></Routes>
      </TestRouter>
    </QueryClientProvider>
  );
  expect(await screen.findByText("报告保留的规划输入")).toBeInTheDocument();
});
