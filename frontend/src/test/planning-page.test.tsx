import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { transferableAbortController } from "node:util";
import { message } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TestCase, TestPlan, TestPlanDraft, TestRunReport } from "../api/contracts";
import PlanPage from "../pages/PlanPage";
import CasesPage from "../pages/CasesPage";
import ReportPage from "../pages/ReportPage";

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

function savedPlan(version = 1, userInput: string | null = null): TestPlan {
  return {
    id: `11111111-1111-4111-8111-${String(version).padStart(12, "0")}`,
    test_case_id: testCase.id,
    version_number: version,
    origin: version === 1 ? "planning" : "replanning",
    derived_from_plan_id: version === 1 ? null : savedPlan(version - 1).id,
    planning_context: {
      test_case_content: testCase.content,
      user_input: userInput,
      planning_prompt_version: "planner-v2",
      planning_model: {
        profile_id: "default", provider: "scripted", model: "deterministic",
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
        definition: { type: "judge", title: "检查页面", goal: "确认设置页面",
          success_criteria: ["设置标题可见"], max_cycles: 2 }
      }]
    },
    created_at: "2026-01-01T00:00:00Z"
  };
}

let plans: TestPlan[];
let requests: Request[];
let generateResponse: ((request: Request) => Promise<Response>) | undefined;
let queryClient: QueryClient;
let currentCase: TestCase;
let archived: boolean;
let updateResponse: ((request: Request) => Promise<Response>) | undefined;
let archiveResponse: (() => Promise<Response>) | undefined;
let startResponse: (() => Promise<Response>) | undefined;

beforeEach(() => {
  plans = [];
  requests = [];
  generateResponse = undefined;
  updateResponse = undefined; archiveResponse = undefined; startResponse = undefined;
  currentCase = { ...testCase, content: { ...testCase.content } }; archived = false;
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
      if (path === "/api/runs") return Response.json({ items: [], total: 0 });
      if (path === "/api/test-cases") return Response.json({ items: archived ? [] : [{ ...currentCase, latest_plan_version: plans[0]?.version_number ?? null }], total: archived ? 0 : 1 });
      if (archived && path.startsWith(`/api/test-cases/${testCase.id}`)) return Response.json({ code: "test_case_not_found", message: "用例不存在或已归档" }, { status: 404 });
      if (path === "/api/test-cases/another-case/plans") return Response.json({ items: [], total: 0 });
      if (path === "/api/test-cases/another-case") return Response.json({ ...testCase, id: "another-case", content: { name: "另一用例", source_text: "另一个原始目标" } });
      if (path.endsWith("/plans")) return Response.json({ items: plans, total: plans.length });
      return Response.json(currentCase);
    }
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
      const body = await request.json() as { user_input: string | null };
      const plan = savedPlan(plans.length + 1, body.user_input);
      plans = [plan, ...plans];
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
  queryClient.clear();
  await act(async () => { message.destroy(); await new Promise((resolve) => setTimeout(resolve, 0)); });
  vi.unstubAllGlobals();
});

function renderPlanPage() {
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/cases/${testCase.id}/plan`]}>
        <Link to="/cases/another-case/plan">切换用例</Link>
        <Link to={`/cases/${testCase.id}/plan`}>返回原用例</Link>
        <Routes>
          <Route path="/cases/:caseId/plan" element={<PlanPage />} />
          <Route path="/runs/:runId" element={<div>运行已创建</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const planningInput = () => screen.getByRole("textbox", { name: "规划额外输入" });
const startButton = () => screen.getByRole("button", { name: /启动运行/ });
const planningPosts = () => requests.filter((request) => request.method === "POST" && new URL(request.url).pathname.endsWith("/plans"));

describe("planning page", () => {
  it("keeps conflicting actions disabled after returning to a case with pending planning", async () => {
    plans = [savedPlan()];
    let completePlanning!: (response: Response) => void;
    generateResponse = () => new Promise((resolve) => { completePlanning = resolve; });
    renderPlanPage();
    fireEvent.change(await screen.findByRole("textbox", { name: "规划额外输入" }), { target: { value: "调整计划" } });
    fireEvent.click(screen.getByRole("button", { name: "重新规划" }));
    await waitFor(() => expect(completePlanning).toBeDefined());

    fireEvent.click(screen.getByRole("link", { name: "切换用例" }));
    expect(await screen.findByRole("button", { name: "生成计划" })).toBeEnabled();
    fireEvent.click(screen.getByRole("link", { name: "返回原用例" }));
    expect(await screen.findByRole("button", { name: /编辑用例/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /编辑计划/ })).toBeDisabled();
    expect(planningInput()).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(startButton()).toBeDisabled();
    expect(planningPosts()).toHaveLength(1);

    plans = [savedPlan(2, "调整计划"), ...plans];
    await act(async () => { completePlanning(Response.json(plans[0], { status: 201 })); });
    await waitFor(() => expect(screen.getByRole("button", { name: /编辑用例/ })).toBeEnabled());
    expect(screen.getByRole("combobox", { name: "计划版本" })).toBeEnabled();
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
    expect(await planningPosts()[0]?.json()).toEqual({ user_input: userInput || null });
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
    expect(await planningPosts()[0]?.json()).toEqual({ user_input: "细化成功标准" });
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
    await waitFor(() => expect(replan).toBeEnabled());
    expect(planningInput()).toHaveValue("保留本轮输入");
    expect(planningPosts()).toHaveLength(1);
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
    expect(screen.getByRole("spinbutton")).toBeDisabled();
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
    expect(await posts[0]!.json()).toEqual({ test_plan_id: plan.id, assumptions_confirmed: true });
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
    plans[0]!.content.tasks.push({ test_task_id: "second-task", definition: { ...plans[0]!.content.tasks[0]!.definition, title: "第二任务" } });
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
    original.content.tasks.push({ test_task_id: "second-task", definition: { ...original.content.tasks[0]!.definition, title: "第二任务" } });
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

function renderCasesPage() {
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[`/cases/${testCase.id}/plan?search=设置`]}>
    <Routes><Route path="/" element={<CasesPage />} /><Route path="/cases/:caseId/plan" element={<CasesPage />} /><Route path="/runs/:runId" element={<div>运行已创建</div>} /></Routes>
  </MemoryRouter></QueryClientProvider>);
}

it("archives from a separate inbox button, retains history wording and clears planning caches", async () => {
  plans = [savedPlan()];
  renderCasesPage();
  const archive = await screen.findByRole("button", { name: "归档用例：" + testCase.content.name });
  expect(archive.closest("a")).toBeNull();
  expect(archive.querySelector("[aria-label='inbox']")).toBeInTheDocument();
  fireEvent.click(archive);
  await screen.findByRole("dialog");
  expect(screen.getByText("归档后从规划页移除，历史运行与证据保留。")).toBeInTheDocument();
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

it("disables archive as soon as start is submitted and releases it on startup failure", async () => {
  plans = [savedPlan()];
  let completeStart!: (response: Response) => void;
  startResponse = () => new Promise((resolve) => { completeStart = resolve; });
  renderCasesPage();
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(startButton());
  const archive = screen.getByRole("button", { name: "归档用例：" + testCase.content.name });
  await waitFor(() => expect(archive).toBeDisabled());
  await waitFor(() => expect(completeStart).toBeDefined());
  completeStart(Response.json({ code: "mcp_unavailable", message: "MCP 连接失败" }, { status: 502 }));
  await screen.findByText("MCP 连接失败");
  await waitFor(() => expect(archive).toBeEnabled());
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
    }, environment: { execution_model: plan.planning_context.planning_model, tool_catalog: { tools: [] },
      act_prompt_version: "act-v1", judge_prompt_version: "judge-v1", app_version: "0.1.0", execution_protocol_version: "3", screenshot_history_rounds: 3
    }, task_runs: [] },
    artifacts: [], events: []
  };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(report)));
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/runs/run/report"]}>
        <Routes><Route path="/runs/:runId/report" element={<ReportPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  expect(await screen.findByText("报告保留的规划输入")).toBeInTheDocument();
});
