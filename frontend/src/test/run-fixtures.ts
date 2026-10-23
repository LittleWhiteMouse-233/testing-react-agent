import type { StoredRunEvent, TestRunReport, TestRunVerdict } from "../api/contracts";

export const runId = "11111111-1111-4111-8111-111111111111";
export const caseId = "22222222-2222-4222-8222-222222222222";
export const taskRunId = "33333333-3333-4333-8333-333333333333";
export const imageId = "44444444-4444-4444-8444-444444444444";
const createdAt = "2026-09-14T02:23:11Z";

export function runEvent(sequence: number, event: StoredRunEvent["event"]): StoredRunEvent {
  return { event_id: runId, sequence, occurred_at: createdAt, event };
}

export function runReport(verdict: TestRunVerdict | null = "PASS"): TestRunReport {
  const profile = { profile_id: "default", provider: "scripted", model: "deterministic", base_url: null,
    temperature: 0, timeout_seconds: 60, context_window_tokens: 32768, max_output_tokens: 2048,
    characters_per_token: 1.5, tokens_per_image: 1024, context_safety_margin_tokens: 1024 };
  const status = verdict === "PASS" ? "passed" : verdict === "FAIL" ? "failed" : verdict === "BLOCKED" ? "blocked" : verdict === "CANCELLED" ? "cancelled" : "running";
  return {
    test_case_archived: false,
    detail: {
      run: { id: runId, test_plan_id: runId, status: verdict ? "finished" : "running", verdict,
        created_at: createdAt, started_at: createdAt, finished_at: verdict ? "2026-09-14T02:23:47Z" : null },
      test_plan: { id: runId, test_case_id: caseId, version_number: 1, origin: "planning", derived_from_plan_id: null, created_at: createdAt,
        planning_context: { test_case_content: { name: "设置页面验证", source_text: "进入设置并检查固件版本完整显示" },
          planning_model: profile, planning_prompt_fingerprint: "planning-fingerprint", user_input: "检查版本号完整显示" },
        content: { title: "设置页面计划", setup_steps: ["准备测试环境"], assumptions: ["电视已开机"],
          tasks: [{ test_task_id: runId, definition: { title: "检查固件版本", type: "judge", goal: "确认版本信息", success_criteria: ["版本号完整可见"] } }] } },
      environment: { tool_catalog: { tools: [{ name: "fixture_inspect", source: "fixture", description: "读取观察", input_schema: { type: "object" }, annotations: {} }] },
        resource_catalog: { local_root: null, resources: [], templates: [] },
        execution_model: profile, prompt_fingerprints_by_task_type: { act: "act-v1", judge: "judge-v1" },
        max_cycles_by_task_type: { act: 30, judge: 10 }, app_version: "0.1.0", execution_protocol_version: "7" },
      task_runs: [{ id: taskRunId, test_run_id: runId, test_task_id: runId, status, cycle_count: 2,
        started_at: createdAt, finished_at: verdict ? "2026-09-14T02:23:47Z" : null,
        result: verdict ? { summary: "已根据截图检查版本号", reason_code: verdict === "CANCELLED" ? "user_cancelled" : verdict === "BLOCKED" ? "tool_failed" : verdict === "FAIL" ? "assertion_failed" : "completed" } : null }]
    },
    artifacts: [{ id: imageId, type: "image", test_run_id: null, task_run_id: taskRunId, created_at: createdAt, mime_type: "image/png", size_bytes: 68, sha256: "a".repeat(64) }],
    events: [runEvent(1, { type: "message.appended", test_run_id: runId, task_run_id: taskRunId, message: {
      role: "tool", message_id: "message-1", tool_call_id: "call-1", name: "fixture_inspect", status: "success",
      content: [{ type: "text", text: "截图前观察" }, { type: "image_artifact", artifact_id: imageId }, { type: "text", text: "截图后观察" }]
    } })]
  };
}
