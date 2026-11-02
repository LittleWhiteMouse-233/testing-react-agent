import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { message } from "antd";
import { $api, apiFetch } from "./client";
import type { PlanningEvent } from "./contracts";
import { openPlanningEventStream } from "./planningEvents";

export interface PlanningProgressState {
  requestId: string;
  userInput: string;
  status: "connecting" | "running" | "failed";
  startedAt: number;
  finishedAt?: number;
  events: PlanningEvent[];
  connectionNotice: string;
  error: string;
}

interface PlanningProgressSession {
  requests: ReadonlyMap<string, PlanningProgressState>;
  start: (testCaseId: string, userInput: string) => void;
  close: (testCaseId: string) => string | undefined;
}

const PlanningProgressContext = createContext<PlanningProgressSession | null>(null);

export function PlanningProgressProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [requests, setRequests] = useState<ReadonlyMap<string, PlanningProgressState>>(() => new Map());
  // Both references point to the same snapshot; commands register synchronously before SSE setup.
  const requestRecords = useRef(requests);
  const connections = useRef(new Map<string, () => void>());

  const publish = useCallback((testCaseId: string, progress: PlanningProgressState | null) => {
    const next = new Map(requestRecords.current);
    if (progress) next.set(testCaseId, progress);
    else next.delete(testCaseId);
    requestRecords.current = next;
    setRequests(next);
  }, []);

  useEffect(() => () => {
    connections.current.forEach((close) => close());
    connections.current.clear();
    requestRecords.current = new Map();
  }, []);

  const close = useCallback((testCaseId: string) => {
    const progress = requestRecords.current.get(testCaseId);
    if (progress?.status !== "failed") return;
    publish(testCaseId, null);
    return progress.userInput;
  }, [publish]);

  const start = useCallback((testCaseId: string, userInput: string) => {
    if (requestRecords.current.has(testCaseId)) return;
    const requestId = crypto.randomUUID();
    publish(testCaseId, {
      requestId, userInput, status: "connecting", startedAt: Date.now(), events: [], connectionNotice: "", error: ""
    });
    let submitted = false;
    let stopped = false;
    let stream: { close: () => void } | undefined;
    const update = (change: (progress: PlanningProgressState) => PlanningProgressState) => {
      const progress = requestRecords.current.get(testCaseId);
      if (!stopped && progress?.requestId === requestId) publish(testCaseId, change(progress));
    };
    const finish = (error: string | null) => {
      const progress = requestRecords.current.get(testCaseId);
      if (stopped || progress?.requestId !== requestId) return;
      connections.current.get(testCaseId)?.();
      connections.current.delete(testCaseId);
      publish(testCaseId, error === null ? null : { ...progress, status: "failed", finishedAt: Date.now(), error });
    };
    const testCaseQueryKey = $api.queryOptions("get", "/api/test-cases/{test_case_id}", {
      params: { path: { test_case_id: testCaseId } }
    }).queryKey;
    const testPlansQueryKey = $api.queryOptions("get", "/api/test-cases/{test_case_id}/plans", {
      params: { path: { test_case_id: testCaseId } }
    }).queryKey;
    const timeout = window.setTimeout(() => finish("日志连接超时，尚未提交规划，请关闭后重试"), 10_000);
    connections.current.set(testCaseId, () => {
      stopped = true;
      window.clearTimeout(timeout);
      stream?.close();
    });
    try {
      stream = openPlanningEventStream(testCaseId, {
        onReady: () => {
          if (submitted || stopped) return;
          submitted = true;
          window.clearTimeout(timeout);
          update((progress) => ({ ...progress, status: "running" }));
          void apiFetch.POST("/api/test-cases/{test_case_id}/plans", {
            params: { path: { test_case_id: testCaseId } },
            body: { planning_request_id: requestId, user_input: userInput.trim() || null }
          }).then(async ({ error }) => {
            if (stopped) return;
            if (error) {
              if (error.code === "planning_input_not_found") void queryClient.invalidateQueries({ queryKey: testCaseQueryKey });
              if (error.code === "test_plan_not_latest") void queryClient.invalidateQueries({ queryKey: testPlansQueryKey });
              finish(error.message);
            } else {
              // Invalidation alone can reuse an initial read that started before the plan was saved.
              await Promise.all([testCaseQueryKey, testPlansQueryKey].map((queryKey) => queryClient.cancelQueries({ queryKey })));
              if (stopped) return;
              for (const queryKey of [testCaseQueryKey, testPlansQueryKey, ["get", "/api/test-cases"]]) {
                void queryClient.invalidateQueries({ queryKey });
              }
              finish(null);
              message.success("计划已生成");
            }
          }, () => finish("规划结果未确认：请求连接异常，请检查最新计划后再操作"));
        },
        onEvent: (event) => {
          if (event.test_case_id !== testCaseId || event.planning_request_id !== requestId) return;
          update((progress) => ({ ...progress, events: [...progress.events, event] }));
        },
        onContractError: () => {
          if (!submitted) finish("日志契约异常，尚未提交规划，请关闭后重试");
          else update((progress) => ({ ...progress, connectionNotice: "日志不可用：事件契约异常，正在等待规划结果" }));
        },
        onConnection: (state) => {
          if (state === "reconnecting" && submitted) update((progress) => ({
            ...progress, connectionNotice: "日志连接中断，正在重连；期间日志可能缺失"
          }));
          else if (state === "connected") update((progress) => ({
            ...progress, connectionNotice: progress.connectionNotice ? "日志连接已恢复；断线期间日志可能缺失" : ""
          }));
        }
      });
    } catch {
      finish("日志连接失败，尚未提交规划，请关闭后重试");
    }
  }, [publish, queryClient]);

  return <PlanningProgressContext value={{ requests, start, close }}>{children}</PlanningProgressContext>;
}

export function usePlanningProgress() {
  const session = useContext(PlanningProgressContext);
  if (!session) throw new Error("PlanningProgressProvider is required");
  return session;
}
