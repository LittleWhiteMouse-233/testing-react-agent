export type TaskType = "act" | "judge";

export interface Task {
  task_id: string;
  type: TaskType;
  title: string;
  goal: string;
  success_criteria: string[];
  max_cycles: number;
}

export interface PlanOutput {
  title: string;
  setup_steps: string[];
  assumptions: string[];
  tasks: Task[];
}

export interface TestCase {
  id: string;
  name: string;
  source_text: string;
  created_at: string;
  updated_at: string;
}

export interface PlanRevision {
  id: string;
  test_case_id: string;
  revision: number;
  source: string;
  parent_revision_id?: string;
  plan: PlanOutput;
  model_info: Record<string, unknown>;
  created_at: string;
}

export interface Run {
  id: string;
  test_case_id: string;
  plan_revision_id: string;
  device_id: string;
  status: "pending" | "running" | "finished" | "cancelled";
  overall_result?: "PASS" | "FAIL" | "BLOCKED" | "CANCELLED";
  snapshot: Record<string, any>;
  task_runs?: Array<{
    id: string;
    task_id: string;
    task_index: number;
    status: string;
    cycle_count: number;
    summary?: string;
  }>;
  created_at: string;
}

export interface StepEvent {
  id: string;
  sequence: number;
  run_id: string;
  task_run_id?: string;
  type: string;
  timestamp: string;
  payload: Record<string, any>;
}

export interface Artifact {
  id: string;
  type: string;
  mime_type: string;
  url: string;
}

export interface Report {
  run: Run;
  snapshot: Record<string, any>;
  summary: Record<string, number>;
  tasks: Array<{
    id: string;
    task_id: string;
    task_index: number;
    definition: Task;
    status: string;
    cycle_count: number;
    summary?: string;
    artifacts: Artifact[];
  }>;
  events: StepEvent[];
  artifacts: Artifact[];
}

