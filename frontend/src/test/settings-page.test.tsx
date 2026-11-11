import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { transferableAbortController } from "node:util";
import { message } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { $api } from "../api/client";
import SettingsPage from "../pages/SettingsPage";

vi.mock("../api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/client")>();
  const { default: createFetchClient } = await import("openapi-fetch");
  const { default: createQueryClient } = await import("openapi-react-query");
  const apiFetch = createFetchClient<import("../api/schema").paths>({ baseUrl: "http://localhost", fetch: (request) => fetch(request) });
  return { ...original, apiFetch, $api: createQueryClient(apiFetch) };
});

const schema = {
  type: "object", additionalProperties: false,
  required: ["app_name", "llm", "execution", "planning", "capability_discovery_timeout_seconds"],
  properties: {
    app_name: { type: "string", title: "应用名称", description: "启动时显示的名称" },
    cors_origins: { type: "array", title: "允许的浏览器来源", items: { type: "string" } },
    capability_discovery_timeout_seconds: { type: "number", exclusiveMinimum: 0, title: "能力目录发现超时（秒）", "x-requires-restart": false },
    planning: { type: "object", title: "规划参数", "x-requires-restart": false,
      required: ["max_cycles", "model_response_max_attempts", "model_call_max_attempts", "tool_call_timeout_seconds"], properties: {
        max_cycles: { type: "integer", minimum: 1, maximum: 100, title: "规划决策轮次上限" },
        model_response_max_attempts: { type: "integer", minimum: 1, maximum: 10, title: "规划连续纠错次数" },
        model_call_max_attempts: { type: "integer", minimum: 1, maximum: 10, title: "规划模型传输尝试次数" },
        tool_call_timeout_seconds: { type: "number", exclusiveMinimum: 0, title: "规划工具交互超时" }
      } },
    execution: { type: "object", title: "执行参数", "x-requires-restart": false,
      required: ["goal_max_failures"], properties: {
        goal_max_failures: { type: "integer", minimum: 1, title: "Goal 累计失败上限", description: "失败次数" }
      } },
    llm: { type: "object", title: "模型设置", "x-requires-restart": false, required: ["profiles", "planning_model_id", "execution_model_id"], properties: {
      profiles: { type: "array", minItems: 1, title: "模型配置列表", items: { type: "object", required: ["id"], properties: {
        id: { type: "string", minLength: 1, title: "模型配置 ID" },
        api_key: { anyOf: [{ type: "string" }, { type: "null" }], format: "password", title: "模型令牌" }
      } } },
      planning_model_id: { type: "string", minLength: 1, title: "规划模型", "x-options-path": ["llm", "profiles"], "x-option-value": "id" },
      execution_model_id: { type: "string", minLength: 1, title: "执行模型", "x-options-path": ["llm", "profiles"], "x-option-value": "id" }
    } }
  }
};

function settingsResponse(version = "version-1", restartRequired = false) {
  return { version, restart_required: restartRequired, settings_schema: schema, settings: {
    app_name: "TestPilot", cors_origins: ["http://localhost:5173"], execution: { goal_max_failures: 10 },
    capability_discovery_timeout_seconds: 120,
    planning: { max_cycles: 20, model_response_max_attempts: 3, model_call_max_attempts: 3, tool_call_timeout_seconds: 120 }, llm: {
      profiles: [{ id: "model-a", api_key_configured: true }], planning_model_id: "model-a", execution_model_id: "model-a"
    }
  } };
}

let requests: { method: string; version: string; body: ReturnType<typeof settingsResponse>["settings"] }[];
let saveStatus = 200;
let currentSettings = settingsResponse();

beforeEach(() => {
  requests = [];
  saveStatus = 200;
  currentSettings = settingsResponse();
  vi.stubGlobal("AbortController", class { constructor() { return transferableAbortController(); } });
  vi.stubGlobal("AbortSignal", transferableAbortController().signal.constructor);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    if (request.method === "GET") return Response.json(currentSettings);
    const command = await request.json();
    requests.push({ method: request.method, version: command.version, body: command.settings });
    if (saveStatus === 409) return Response.json({ code: "settings_version_conflict", message: "配置已更新，请重新加载后保存" }, { status: 409 });
    if (saveStatus === 422) return Response.json({ code: "validation_error", message: "配置校验失败", errors: [{ loc: ["execution", "goal_max_failures"], msg: "当前额度不合法" }] }, { status: 422 });
    currentSettings = settingsResponse("version-2", true);
    currentSettings.settings.app_name = command.settings.app_name;
    return Response.json(currentSettings);
  }));
});

afterEach(() => { cleanup(); message.destroy(); vi.unstubAllGlobals(); });

function mountPage(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })) {
  return render(<QueryClientProvider client={queryClient}><SettingsPage /></QueryClientProvider>);
}

it("renders backend labels, hides existing credentials and clears replacements after save", async () => {
  mountPage();
  expect(await screen.findByLabelText("应用名称")).toHaveValue("TestPilot");
  const token = screen.getByLabelText("模型令牌");
  expect(token).toHaveValue("");
  expect(token).toHaveAttribute("placeholder", "已配置，留空保持不变");
  fireEvent.change(token, { target: { value: "typed-replacement" } });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]?.body.llm.profiles[0]).toEqual({ id: "model-a", api_key: "typed-replacement" });
  expect(await screen.findByText("启动参数已经发生修改，请重启应用后生效")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("模型令牌")).toHaveValue(""));
});

it("blocks invalid numeric input using the backend schema", async () => {
  mountPage();
  const budget = await screen.findByLabelText("Goal 累计失败上限");
  fireEvent.change(budget, { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  expect(await screen.findByText(/必须大于等于 1/)).toBeInTheDocument();
  expect(requests).toHaveLength(0);
});

it("saves planning limits and directory discovery timeout independently", async () => {
  mountPage();
  expect(await screen.findByLabelText("规划决策轮次上限")).toHaveValue("20");
  fireEvent.change(screen.getByLabelText("规划决策轮次上限"), { target: { value: "12" } });
  fireEvent.change(screen.getByLabelText("规划连续纠错次数"), { target: { value: "2" } });
  fireEvent.change(screen.getByLabelText("规划模型传输尝试次数"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("规划工具交互超时"), { target: { value: "80" } });
  fireEvent.change(screen.getByLabelText("能力目录发现超时（秒）"), { target: { value: "45" } });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]?.body.planning).toEqual({ max_cycles: 12, model_response_max_attempts: 2, model_call_max_attempts: 4, tool_call_timeout_seconds: 80 });
  expect(requests[0]?.body.capability_discovery_timeout_seconds).toBe(45);
  expect(requests[0]?.body.execution).toEqual({ goal_max_failures: 10 });
});

it("keeps the draft on version conflict and maps backend field errors", async () => {
  mountPage();
  const appName = await screen.findByLabelText("应用名称");
  fireEvent.change(appName, { target: { value: "Unsaved draft" } });
  saveStatus = 409;
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  expect(await screen.findByText("配置已更新，请重新加载后保存")).toBeInTheDocument();
  expect(appName).toHaveValue("Unsaved draft");
  await waitFor(() => expect(screen.getByRole("button", { name: "保存设置" })).not.toBeDisabled());
  saveStatus = 422;
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  expect(await screen.findByText("当前额度不合法")).toBeInTheDocument();
  expect(appName).toHaveValue("Unsaved draft");
});

it("adds and removes model groups without a sorting control", async () => {
  mountPage();
  await screen.findByLabelText("应用名称");
  fireEvent.click(screen.getByRole("button", { name: "新增模型配置列表" }));
  expect(screen.getAllByLabelText("模型配置 ID")).toHaveLength(2);
  fireEvent.change(screen.getAllByLabelText("模型配置 ID")[1]!, { target: { value: "model-b" } });
  fireEvent.click(screen.getAllByRole("button", { name: "删除模型配置列表" })[0]!);
  expect(screen.getAllByLabelText("模型配置 ID")).toHaveLength(1);
  expect(screen.getByLabelText("模型配置 ID")).toHaveValue("model-b");
});

it("loads the latest saved settings and version when returning to the page", async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const page = mountPage(queryClient);
  fireEvent.change(await screen.findByLabelText("应用名称"), { target: { value: "Saved name" } });
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await screen.findByText("启动参数已经发生修改，请重启应用后生效");
  page.unmount();
  currentSettings = { ...currentSettings, version: "version-3", settings: { ...currentSettings.settings, app_name: "Updated in another window" } };
  mountPage(queryClient);
  await waitFor(() => expect(screen.getByLabelText("应用名称")).toHaveValue("Updated in another window"));
  expect(screen.getByText("启动参数已经发生修改，请重启应用后生效")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]?.version).toBe("version-3");
  await waitFor(() => expect(screen.getByRole("button", { name: "保存设置" })).not.toBeDisabled());
});

it("keeps unsaved edits when a background query refreshes", async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  mountPage(queryClient);
  fireEvent.change(await screen.findByLabelText("应用名称"), { target: { value: "Unsaved name" } });
  currentSettings = { ...currentSettings, settings: { ...currentSettings.settings, app_name: "Server name" } };
  await act(() => queryClient.refetchQueries({ queryKey: $api.queryOptions("get", "/api/settings", {}).queryKey }));
  expect(screen.getByLabelText("应用名称")).toHaveValue("Unsaved name");
});

it("keeps unsaved edits when reloading fails", async () => {
  mountPage();
  fireEvent.change(await screen.findByLabelText("应用名称"), { target: { value: "Unsaved name" } });
  vi.mocked(fetch).mockResolvedValueOnce(Response.json({ code: "internal_error", message: "加载暂时失败" }, { status: 500 }));
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByText("加载暂时失败");
  expect(screen.getByLabelText("应用名称")).toHaveValue("Unsaved name");
});

it("explains that a timed-out save may still succeed and preserves the draft", async () => {
  mountPage();
  fireEvent.change(await screen.findByLabelText("应用名称"), { target: { value: "Unsaved name" } });
  vi.mocked(fetch).mockRejectedValueOnce(new DOMException("The operation timed out.", "TimeoutError"));
  fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
  await screen.findAllByText("保存结果尚未确认，请重新加载设置检查是否生效");
  expect(screen.getByLabelText("应用名称")).toHaveValue("Unsaved name");
});

it("marks startup list settings as requiring restart", async () => {
  mountPage();
  expect(await screen.findByRole("heading", { name: "允许的浏览器来源 重启生效" })).toBeInTheDocument();
});
