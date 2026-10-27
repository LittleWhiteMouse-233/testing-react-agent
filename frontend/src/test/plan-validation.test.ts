import { describe, expect, it } from "vitest";
import type { TestPlan } from "../api/contracts";
import { isPlanDraftValid, toPlanDraft, updateTaskOrder } from "../planDraft";
import type { TestTaskDefinition } from "../api/contracts";

const plan: TestPlan = {
  id: "11111111-1111-4111-8111-111111111111",
  test_case_id: "22222222-2222-4222-8222-222222222222",
  version_number: 1,
  origin: "planning",
  derived_from_plan_id: null,
  planning_context: {
    test_case_content: { name: "用例", source_text: "检查页面" },
    planning_model: {
      profile_id: "default",
      provider: "scripted",
      model: "deterministic",
      base_url: null,
      temperature: 0,
      timeout_seconds: 60,
      context_window_tokens: 32768,
      max_output_tokens: 2048,
      characters_per_token: 1.5,
      tokens_per_image: 1024,
      context_safety_margin_tokens: 1024
    },
    planning_prompt_fingerprint: "planning-fingerprint"
  },
  content: {
    title: "计划",
    setup_steps: [],
    assumptions: [],
    tasks: [{
      test_task_id: "33333333-3333-4333-8333-333333333333",
      definition: {
        task_no: 1,
        type: "judge",
        title: "判断",
        goal: "确认页面",
        success_criteria: ["标题可见"]
      }
    }]
  },
  created_at: "2026-01-01T00:00:00Z"
};

describe("plan UI draft mapper", () => {
  it("removes persisted task identity at the one revision boundary", () => {
    const draft = toPlanDraft(plan);
    const draftTask = draft.tasks[0];
    const planTask = plan.content.tasks[0];
    expect(draftTask).toBeDefined();
    expect(planTask).toBeDefined();
    if (!draftTask || !planTask) throw new Error("Expected one plan task");
    expect(draftTask).not.toHaveProperty("test_task_id");
    expect(draftTask).toEqual(planTask.definition);
    expect(isPlanDraftValid(draft)).toBe(true);
  });

  it("rejects blank observable criteria", () => {
    const draft = toPlanDraft(plan);
    const draftTask = draft.tasks[0];
    expect(draftTask).toBeDefined();
    if (!draftTask) throw new Error("Expected one draft task");
    draftTask.success_criteria = [""];
    expect(isPlanDraftValid(draft)).toBe(false);
  });
});

function traversalTasks(): TestTaskDefinition[] {
  const base = plan.content.tasks[0]!.definition;
  return [
    { ...base, task_no: 1, title: "Root", type: "act" },
    { ...base, task_no: 2, title: "Next node", type: "act" },
    { ...base, task_no: 3, title: "Complete?", type: "goal", jump_back_to_task_no: 2 }
  ];
}

describe("goal references in editable drafts", () => {
  it("preserves the referenced task when reordering and renumbering", () => {
    const tasks = traversalTasks();
    const reordered = updateTaskOrder([tasks[1]!, tasks[0]!, tasks[2]!]);
    expect(reordered.map((task) => [task.task_no, task.title])).toEqual([[1, "Next node"], [2, "Root"], [3, "Complete?"]]);
    expect(reordered[2]!.jump_back_to_task_no).toBe(1);
    expect(tasks[2]!.jump_back_to_task_no).toBe(2);
  });

  it("clears a deleted target and prevents saving until a new target is selected", () => {
    const tasks = traversalTasks();
    const remaining = updateTaskOrder([tasks[0]!, tasks[2]!]);
    expect(remaining[1]!.jump_back_to_task_no).toBeNull();
    const draft = { ...toPlanDraft(plan), tasks: remaining };
    expect(isPlanDraftValid(draft)).toBe(false);
    draft.tasks[1]!.jump_back_to_task_no = 1;
    expect(isPlanDraftValid(draft)).toBe(true);
  });

  it("clears a target moved after its Goal", () => {
    const tasks = traversalTasks();
    const reordered = updateTaskOrder([tasks[0]!, tasks[2]!, tasks[1]!]);
    expect(reordered[1]!.jump_back_to_task_no).toBeNull();
  });

  it("adjusts the target when an earlier unrelated task is removed", () => {
    const tasks = traversalTasks();
    const remaining = updateTaskOrder([tasks[1]!, tasks[2]!]);
    expect(remaining.map((task) => task.task_no)).toEqual([1, 2]);
    expect(remaining[1]!.jump_back_to_task_no).toBe(1);
  });

  it("rejects invalid numbering and non-Goal jump values", () => {
    const draft = { ...toPlanDraft(plan), tasks: traversalTasks() };
    draft.tasks[1]!.task_no = 1;
    expect(isPlanDraftValid(draft)).toBe(false);
    draft.tasks[1]!.task_no = 2;
    draft.tasks[1]!.jump_back_to_task_no = 1;
    expect(isPlanDraftValid(draft)).toBe(false);
  });
});
