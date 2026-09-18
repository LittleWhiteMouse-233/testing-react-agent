import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import type { PlanningEvent } from "./contracts";
import { openPlanningEventStream } from "./planningEvents";

export interface PlanningProgressState {
  status: "connecting" | "running" | "failed";
  startedAt: number;
  finishedAt?: number;
  events: PlanningEvent[];
  connectionNotice: string;
  error: string;
}

export function usePlanningProgress(testCaseId: string) {
  const [progress, setProgress] = useState<PlanningProgressState | null>(null);
  const activeRequest = useRef<string | null>(null);
  const cleanup = useRef<(() => void) | null>(null);
  useEffect(() => () => { activeRequest.current = null; cleanup.current?.(); }, []);

  const close = useCallback(() => {
    activeRequest.current = null;
    cleanup.current?.();
    cleanup.current = null;
    setProgress(null);
  }, []);

  const operation = useMutation({
    meta: { testCaseId },
    mutationFn: (submit: (requestId: string) => Promise<unknown>) => new Promise<void>((resolve) => {
      if (activeRequest.current) { resolve(); return; }
      const requestId = crypto.randomUUID();
      activeRequest.current = requestId;
      setProgress({ status: "connecting", startedAt: Date.now(), events: [], connectionNotice: "", error: "" });
      let submitted = false;
      let stopped = false;
      let stream: { close: () => void } | undefined;
      const finish = (error: string | null) => {
        if (activeRequest.current !== requestId) return;
        cleanup.current?.();
        cleanup.current = null;
        activeRequest.current = null;
        if (error === null) setProgress(null);
        else setProgress((current) => current && ({ ...current, status: "failed", finishedAt: Date.now(), error }));
      };
      const timeout = window.setTimeout(() => finish("日志连接超时，尚未提交规划，请关闭后重试"), 10_000);
      cleanup.current = () => { stopped = true; window.clearTimeout(timeout); stream?.close(); resolve(); };
      try {
        stream = openPlanningEventStream(testCaseId, {
          onReady: () => {
            if (submitted || stopped) return;
            submitted = true;
            window.clearTimeout(timeout);
            setProgress((current) => current && ({ ...current, status: "running" }));
            void submit(requestId).then(() => finish(null), (error: unknown) => {
              const reason = typeof error === "object" && error !== null && "code" in error && "message" in error
                ? String(error.message) : "规划结果未确认：请求连接异常，请检查最新计划后再操作";
              finish(reason);
            });
          },
          onEvent: (event) => {
            if (stopped || event.test_case_id !== testCaseId || event.planning_request_id !== requestId) return;
            setProgress((current) => current && ({ ...current, events: [...current.events, event] }));
          },
          onContractError: () => {
            if (!submitted) finish("日志契约异常，尚未提交规划，请关闭后重试");
            else setProgress((current) => current && ({ ...current, connectionNotice: "日志不可用：事件契约异常，正在等待规划结果" }));
          },
          onConnection: (state) => {
            if (stopped) return;
            if (state === "reconnecting" && submitted) setProgress((current) => current && ({
              ...current, connectionNotice: "日志连接中断，正在重连；期间日志可能缺失"
            }));
            else if (state === "connected") setProgress((current) => current && ({
              ...current, connectionNotice: current.connectionNotice ? "日志连接已恢复；断线期间日志可能缺失" : ""
            }));
          }
        });
      } catch {
        finish("日志连接失败，尚未提交规划，请关闭后重试");
      }
    })
  });

  return { progress, pending: progress !== null && progress.status !== "failed", start: operation.mutate, close };
}
