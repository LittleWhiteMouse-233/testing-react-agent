import type { components } from "./schema";

type Schemas = components["schemas"];

export type ApiError = Schemas["ApiError"];
export type Artifact = Schemas["Artifact"];
export type StoredRunEvent = Schemas["StoredRunEvent"];
export type TestCase = Schemas["TestCase"];
export type TestCaseCreateRequest = Schemas["TestCaseCreateRequest"];
export type TestPlan = Schemas["TestPlan"];
export type TestPlanDraft = Schemas["TestPlanContent_TestTaskDefinition_"];
export type TestTaskDefinition = Schemas["TestTaskDefinition"];
