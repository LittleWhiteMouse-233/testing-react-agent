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

export function emptyTask(taskNo: number, setup = ""): TestTaskDefinition {
  return {
    task_no: taskNo,
    type: "act",
    title: setup || "新任务",
    goal: setup,
    success_criteria: [""]
  };
}

export function updateTaskOrder(tasks: TestTaskDefinition[]): TestTaskDefinition[] {
  const taskNumbers = new Map(tasks.map((task, index) => [task.task_no, index + 1]));
  return tasks.map((task, index) => {
    const taskNo = index + 1;
    const target = task.jump_back_to_task_no == null ? undefined : taskNumbers.get(task.jump_back_to_task_no);
    return { ...task, task_no: taskNo,
      jump_back_to_task_no: task.type === "goal" && target !== undefined && target < taskNo ? target : null };
  });
}

export function getGoalsWithInvalidatedJumpTargets(
  tasksBeforeRenumbering: TestTaskDefinition[],
  tasksAfterRenumbering: TestTaskDefinition[]
): TestTaskDefinition[] {
  return tasksBeforeRenumbering.filter((task, index) =>
    task.type === "goal" && task.jump_back_to_task_no != null &&
    tasksAfterRenumbering[index]!.jump_back_to_task_no == null
  );
}

export function isPlanDraftValid(plan: TestPlanDraft | null): boolean {
  return !!plan &&
    plan.title.trim().length > 0 &&
    plan.title.length <= 200 &&
    [...plan.assumptions, ...plan.setup_steps].every((text) => text.trim().length > 0) &&
    plan.tasks.length > 0 &&
    plan.tasks.every(
      (task, index) =>
        task.task_no === index + 1 &&
        (task.type === "goal"
          ? task.jump_back_to_task_no != null && Number.isInteger(task.jump_back_to_task_no) &&
            task.jump_back_to_task_no >= 1 && task.jump_back_to_task_no < task.task_no
          : task.jump_back_to_task_no == null) &&
        task.title.trim().length > 0 &&
        task.title.length <= 200 &&
        task.goal.trim().length > 0 &&
        task.success_criteria.length > 0 &&
        task.success_criteria.every((criterion) => criterion.trim().length > 0)
    );
}
