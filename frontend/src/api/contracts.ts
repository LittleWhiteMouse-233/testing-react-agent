import type { components } from "./schema";

type Schemas = components["schemas"];

export type ApiError = Schemas["ApiError"];
export type StoredRunEvent = Schemas["StoredRunEvent"];
export type TestCase = Schemas["TestCase"];
export type TestCaseContent = Schemas["TestCaseContent"];
export type TestCaseCreateRequest = Schemas["TestCaseCreateRequest"];
export type TestPlan = Schemas["TestPlan"];
export type TestPlanDraft = Schemas["TestPlanContent_TestTaskDefinition_"];
export type TestTaskDefinition = Schemas["TestTaskDefinition"];
export type TestRunReport = Schemas["TestRunReport"];
export type TaskRun = Schemas["TaskRun"];
export type TestRunListResponse = Schemas["TestRunListResponse"];
export type TestRunStatisticsResponse = Schemas["TestRunStatisticsResponse"];
export type TestRunVerdict = Schemas["TestRunVerdict"];
