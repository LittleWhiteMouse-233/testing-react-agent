import type { components } from "./schema";

type Schemas = components["schemas"];

export type ApiErrorBody = Schemas["ApiError"];
export type Artifact = Schemas["Artifact"];
export type DeviceView = Schemas["DeviceView"];
export type DevicePage = Schemas["PageResponse_DeviceView_"];
export type GeneratePlanRequest = Schemas["GeneratePlanRequest"];
export type ReportExportRequest = Schemas["ReportExportRequest"];
export type RunEventPage = Schemas["PageResponse_StoredRunEvent_"];
export type ReviseTestPlanRequest = Schemas["ReviseTestPlanRequest"];
export type StoredRunEvent = Schemas["StoredRunEvent"];
export type TestCase = Schemas["TestCase"];
export type TestCaseCreateRequest = Schemas["TestCaseCreateRequest"];
export type TestCasePage = Schemas["PageResponse_TestCase_"];
export type TestPlan = Schemas["TestPlan"];
export type TestPlanPage = Schemas["PageResponse_TestPlan_"];
export type TestPlanDraft = Schemas["TestPlanContent_TestTaskDefinition_"];
export type TestRun = Schemas["TestRun"];
export type TestRunCreateRequest = Schemas["TestRunCreateRequest"];
export type TestRunDetail = Schemas["TestRunDetail"];
export type TestRunPage = Schemas["PageResponse_TestRun_"];
export type TestRunReport = Schemas["TestRunReport"];
export type TestTaskDefinition = Schemas["TestTaskDefinition"];
