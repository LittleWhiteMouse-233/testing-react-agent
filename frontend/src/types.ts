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
  snapshot: RunSnapshot;
  task_runs?: Array<{
    id: string;
    task: Task;
    task_index: number;
    status: TaskStatus;
    cycle_count: number;
    outcome?: TaskOutcome;
  }>;
  created_at: string;
}

export type TaskStatus = "pending" | "running" | "passed" | "failed" | "blocked" | "skipped";

export interface TaskOutcome {
  status: TaskStatus;
  reason_code: ReasonCode;
  summary: string;
  cycle_count: number;
  evidence_artifact_ids: string[];
}

export type ReasonCode =
  | "completed"
  | "assertion_failed"
  | "goal_unreachable"
  | "cycle_limit"
  | "device_unavailable"
  | "capture_failed"
  | "model_unavailable"
  | "invalid_model_response"
  | "agent_blocked"
  | "tool_unavailable"
  | "tool_failed"
  | "process_restarted"
  | "global_fail_fast"
  | "user_cancelled"
  | "unexpected_error";

export interface RunSnapshot {
  test_case: { id: string; name: string; source_text: string };
  plan_revision: { id: string; revision: number; source: string };
  plan: PlanOutput;
  confirmed_assumptions: string[];
  device: {
    id: string;
    health_message: string;
    capabilities?: Record<string, unknown>;
  };
  model: Record<string, unknown>;
  enabled_tools: Array<Record<string, unknown>>;
  prompt_versions: Record<string, string>;
  app_version: string;
  execution_protocol_version: string;
}

export interface ToolInvocation {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  decision_summary: string;
}

export interface ToolExecutionResult {
  status: "succeeded" | "timed_out" | "invalid" | "blocked" | "cancelled";
  summary: string;
  data: Record<string, unknown>;
}

interface StepEventBase<TType extends string, TPayload> {
  id: string;
  sequence: number;
  run_id: string;
  task_run_id: string | null;
  type: TType;
  timestamp: string;
  payload: TPayload;
}

export type StepEvent =
  | StepEventBase<"run.started", { device_id: string }>
  | StepEventBase<"task.started", { task_index: number; task: Task }>
  | StepEventBase<"cycle.started", { cycle_count: number }>
  | StepEventBase<"observation.captured", {
      observation: {
        artifact_id: string;
        mime_type: string;
        activity: string | null;
        cycle_count: number;
      };
    }>
  | StepEventBase<"agent.action_selected", {
      cycle_count: number;
      invocation: ToolInvocation;
    }>
  | StepEventBase<"agent.terminal_selected", {
      cycle_count: number;
      status: "passed" | "failed" | "blocked";
      summary: string;
      evidence_artifact_ids: string[];
    }>
  | StepEventBase<"tool.started", {
      cycle_count: number;
      invocation: ToolInvocation;
    }>
  | StepEventBase<"tool.finished", {
      cycle_count: number;
      invocation: ToolInvocation;
      result: ToolExecutionResult;
    }>
  | StepEventBase<"agent.response_invalid", {
      cycle_count: number;
      attempt: number;
      message: string;
    }>
  | StepEventBase<"execution.error", {
      reason_code: ReasonCode;
      message: string;
    }>
  | StepEventBase<"task.finished", { outcome: TaskOutcome }>
  | StepEventBase<"tasks.skipped", {
      task_ids: string[];
      reason_code: ReasonCode;
    }>
  | StepEventBase<"run.finished", {
      result: "PASS" | "FAIL" | "BLOCKED" | "CANCELLED";
    }>
  | StepEventBase<"run.cancelled", { result: "CANCELLED" }>;

export type ExecutionEventType = StepEvent["type"];

export interface Artifact {
  id: string;
  run_id: string;
  task_run_id: string | null;
  type: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
  url: string;
  metadata: Record<string, unknown>;
}

export interface Report {
  run: {
    id: string;
    status: Run["status"];
    overall_result?: Run["overall_result"];
    device_id: string;
    started_at?: string;
    finished_at?: string;
    created_at: string;
  };
  snapshot: RunSnapshot;
  summary: {
    task_count: number;
    passed: number;
    failed: number;
    blocked: number;
    skipped: number;
    event_count: number;
    artifact_count: number;
  };
  tasks: Array<{
    id: string;
    task_index: number;
    definition: Task;
    status: TaskStatus;
    cycle_count: number;
    outcome?: TaskOutcome;
    started_at?: string;
    finished_at?: string;
    artifacts: Artifact[];
    evidence: Artifact[];
  }>;
  events: StepEvent[];
  artifacts: Artifact[];
}
