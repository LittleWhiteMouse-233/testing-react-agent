import { describe, expect, it } from "vitest";
import type { TestPlan } from "../api/contracts";
import { isPlanDraftValid, toPlanDraft } from "../planDraft";

const plan: TestPlan = {
  id: "11111111-1111-4111-8111-111111111111",
  test_case_id: "22222222-2222-4222-8222-222222222222",
  version_number: 1,
  origin: "planning",
  derived_from_plan_id: null,
  planning_context: {
    test_case_content: { name: "用例", source_text: "检查页面" },
    device_info: null,
    planning_model: {
      profile_id: "default",
      provider: "scripted",
      model: "deterministic",
      base_url: null,
      temperature: 0,
      timeout_seconds: 60
    },
    planning_prompt_version: "v1"
  },
  content: {
    title: "计划",
    setup_steps: [],
    assumptions: [],
    tasks: [{
      test_task_id: "33333333-3333-4333-8333-333333333333",
      definition: {
        type: "judge",
        title: "判断",
        goal: "确认页面",
        success_criteria: ["标题可见"],
        max_cycles: 2
      }
    }]
  },
  created_at: "2026-01-01T00:00:00Z"
};

describe("plan UI draft mapper", () => {
  it("removes persisted task identity at the one revision boundary", () => {
    const draft = toPlanDraft(plan);
    expect(draft.tasks[0]).not.toHaveProperty("test_task_id");
    expect(draft.tasks[0]).toEqual(plan.content.tasks[0].definition);
    expect(isPlanDraftValid(draft)).toBe(true);
  });

  it("rejects blank observable criteria", () => {
    const draft = toPlanDraft(plan);
    draft.tasks[0].success_criteria = [""];
    expect(isPlanDraftValid(draft)).toBe(false);
  });
});
