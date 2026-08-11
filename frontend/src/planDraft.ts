import type {
  TestPlan,
  TestPlanDraft,
  TestTaskDefinition
} from "./api/contracts";

export function toPlanDraft(plan: TestPlan): TestPlanDraft {
  return {
    title: plan.content.title,
    setup_steps: [...plan.content.setup_steps],
    assumptions: [...plan.content.assumptions],
    tasks: plan.content.tasks.map((task) => ({ ...task.definition }))
  };
}

export function emptyTask(setup = ""): TestTaskDefinition {
  return {
    type: "act",
    title: setup || "新任务",
    goal: setup,
    success_criteria: [""],
    max_cycles: 10
  };
}

export function isPlanDraftValid(plan: TestPlanDraft | null): boolean {
  return !!plan &&
    plan.title.trim().length > 0 &&
    plan.tasks.length > 0 &&
    plan.tasks.every(
      (task) =>
        task.title.trim().length > 0 &&
        task.goal.trim().length > 0 &&
        task.success_criteria.length > 0 &&
        task.success_criteria.every((criterion) => criterion.trim().length > 0) &&
        task.max_cycles >= 1
    );
}
