import { Alert, Button, Spin } from "antd";
import { useEffect, useRef, useState } from "react";
import type { PlanningEvent } from "../api/contracts";
import type { PlanningProgressState } from "../api/usePlanningProgress";

function planningEventText(event: PlanningEvent): string {
  switch (event.type) {
    case "planning.started": return "开始规划，正在读取用例和最新计划";
    case "planning.input_ready": return "输入准备完成";
    case "planning.attempt_started": return `第 ${event.attempt}/3 次生成，等待模型返回`;
    case "planning.attempt_failed": return `第 ${event.attempt}/3 次生成失败：${event.reason}`;
    case "planning.validated": return "计划校验通过";
    case "planning.saving": return "正在保存计划";
    case "planning.succeeded": return "计划保存成功";
    case "planning.failed": return event.reason;
  }
}

export default function PlanningProgressDialog({ progress, onClose }: {
  progress: PlanningProgressState;
  onClose: () => void;
}) {
  const [now, setNow] = useState(Date.now());
  const panel = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const pending = progress.status !== "failed";
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [pending]);
  useEffect(() => {
    if (panel.current && follow.current) panel.current.scrollTop = panel.current.scrollHeight;
  }, [progress.events, progress.error]);
  const elapsed = Math.max(0, Math.floor(((progress.finishedAt ?? now) - progress.startedAt) / 1000));
  const errorAlreadyShown = progress.events.some((event) => event.type === "planning.failed" && event.reason === progress.error);

  return <div className="planning-progress-overlay">
    <section className="planning-progress-dialog" role="dialog" aria-label="规划进度" aria-busy={pending}>
      <header><h3>规划进度</h3><span>{pending && <Spin size="small" />} {progress.status === "connecting" ? "正在连接日志" : pending ? "规划中" : "规划未完成"} · {elapsed} 秒</span></header>
      {progress.connectionNotice && <Alert type="warning" title={progress.connectionNotice} />}
      <div className="planning-progress-log" role="log" aria-label="规划日志" ref={panel} onScroll={() => {
        const element = panel.current;
        if (element) follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
      }}>
        {progress.events.map((event, index) => <div key={index} className={"reason" in event ? "planning-log-error" : ""}>
          <time>{new Date(event.occurred_at).toLocaleTimeString("zh-CN", { hour12: false })}</time> {planningEventText(event)}
        </div>)}
        {progress.error && !errorAlreadyShown && <div className="planning-log-error">{progress.error}</div>}
      </div>
      {!pending && <footer><Button aria-label="关闭" onClick={onClose}>关闭</Button></footer>}
    </section>
  </div>;
}
